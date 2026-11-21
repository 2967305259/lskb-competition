from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from sqlalchemy import inspect

import app as a
from backup import create_backup, verify_backup
from conftest import login, png_bytes
from migrations import assert_schema_current, migrate_database
from test_p0 import complete, transition_data


def upload(client, endpoint, field, content, filename, **data):
    return client.post(endpoint, data={**data, field: (BytesIO(content), filename)})


def test_image_validation_metadata_size_and_quota(application, client, monkeypatch):
    ids = application.test_ids
    login(client, ids['users'][0])
    root = Path(application.config['UPLOAD_ROOT']) / 'avatars'
    path = '/captain/upload-avatar'
    assert upload(client, path, 'avatar', b'invalid image', 'misleading.png').status_code == 400
    assert upload(client, path, 'avatar', png_bytes(), 'mismatch.jpg').status_code == 400
    assert upload(client, path, 'avatar', b'a' * (4 * 1024**2 + 1), 'large.png').status_code == 413
    with monkeypatch.context() as change:
        change.setitem(application.config, 'UPLOAD_USER_MAX_MB', 0)
        assert upload(client, path, 'avatar', png_bytes(), 'avatar.png').status_code == 413
    with monkeypatch.context() as change:
        change.setitem(application.config, 'UPLOAD_TOTAL_MAX_MB', 0)
        assert upload(client, path, 'avatar', png_bytes(), 'avatar.png').status_code == 413
    assert list(root.iterdir()) == []
    assert upload(client, path, 'avatar', png_bytes(), '我的头像.png').status_code == 302
    with application.app_context():
        saved = a.UploadAsset.query.one()
        user = a.db.session.get(a.User, ids['users'][0])
        assert re.fullmatch(r'[a-f0-9]{32}\.png', user.avatar)
        assert saved.relative_path == f'avatars/{user.avatar}'
        assert (saved.uploader_id, saved.original_name, saved.mime_type, saved.size_bytes, saved.sha256) == (
            user.id, '我的头像.png', 'image/png', len(png_bytes()), sha256(png_bytes()).hexdigest())
        avatar = user.avatar
    assert application.test_client().get(f'/static/uploads/avatars/{avatar}').data == png_bytes()


def test_screenshot_batch_is_atomic_if_any_file_fails(application, client):
    ids = application.test_ids
    login(client, ids['users'][0])
    path = f"/tournament/match/{ids['match']}/submit-result"
    base = {'score': '100', 'difficulty': '14'}
    files = [(BytesIO(png_bytes()), 'first.png'), (BytesIO(b'invalid'), 'second.png')]
    assert client.post(path, data={**base, 'screenshots': files}).status_code == 400
    assert client.post(path, data={**base, 'screenshots': [(BytesIO(png_bytes()), f'{i}.png') for i in range(4)]}).status_code == 400
    with application.app_context():
        assert not a.UploadAsset.query.count()
        match = a.db.session.get(a.Match, ids['match'])
        assert match.player_a_score is None and not match.match_note
    assert list((Path(application.config['UPLOAD_ROOT']) / 'screenshots').iterdir()) == []
    assert upload(client, path, 'screenshots', png_bytes(), 'evidence.png', **base).status_code == 302
    with application.app_context():
        file = a.UploadAsset.query.one().relative_path
    assert application.test_client().get('/static/uploads/' + file).status_code == 404
    login(client, ids['users'][1])
    assert client.get('/static/uploads/' + file).data == png_bytes()


@pytest.fixture(scope='session')
def webm_sample(tmp_path_factory):
    if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
        pytest.skip('FFmpeg is required to validate the video upload path')
    path = tmp_path_factory.mktemp('video') / 'sample.webm'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=32x32:r=10:d=0.3',
                    '-c:v', 'libvpx-vp9', str(path)], check=True, capture_output=True, timeout=15)
    return path.read_bytes()


def test_carousel_upload_validation_and_range_policy(application, client, webm_sample):
    ids = application.test_ids
    login(client, ids['admin'])
    endpoint = '/admin/carousel/add'
    assert upload(client, endpoint, 'media', webm_sample, 'wrong.mp4', media_type='video').status_code == 400
    assert upload(client, endpoint, 'media', webm_sample, '赛事轮播.webm', media_type='video', title='现场视频').status_code == 302
    with application.app_context():
        item = a.CarouselItem.query.one()
        asset = a.UploadAsset.query.one()
        assert (asset.mime_type, asset.original_name, item.file_path) == (
            'video/webm', '赛事轮播.webm', '/static/uploads/' + asset.relative_path)
        url, item_id = item.file_path, item.id
    anon = application.test_client()
    assert url in anon.get('/').get_data(as_text=True)
    video = anon.get(url, headers={'Range': 'bytes=0-7'})
    assert video.status_code == 206 and video.data == webm_sample[:8] and video.mimetype == 'video/webm'
    assert client.post(f'/admin/carousel/{item_id}/edit', data={'file_path': url, 'media_type': 'video'}).status_code == 302
    assert anon.get(url).status_code == 404
    assert client.get(url).data == webm_sample


def test_inventory_reports_drift_and_cleanup_preserves_archived_media(application, client, tmp_path):
    ids = application.test_ids
    root = Path(application.config['UPLOAD_ROOT'])
    login(client, ids['users'][0])
    for filename in ('first.png', 'second.png'):
        assert upload(client, '/captain/upload-avatar', 'avatar', png_bytes(), filename).status_code == 302
    login(client, ids['admin'])
    assert upload(client, '/admin/carousel/add', 'media', png_bytes(), 'old-cover.png', media_type='image', title='旧届封面').status_code == 302
    assert upload(client, '/admin/carousel/add', 'media', png_bytes(), 'unused-cover.png', media_type='image', title='备用封面').status_code == 302
    with application.app_context():
        unused = a.UploadAsset.query.filter_by(original_name='unused-cover.png').one().relative_path
        old_cover = a.UploadAsset.query.filter_by(original_name='old-cover.png').one().relative_path
        unused_id = a.CarouselItem.query.filter_by(file_path='/static/uploads/' + unused).one().id
    assert client.post(f'/admin/carousel/{unused_id}/delete').status_code == 302
    complete(application, client)
    assert client.post('/admin/new-season', data=transition_data(application, client)).status_code == 302
    assert client.get('/admin/data-tools/media?format=json').status_code == 403
    login(client, ids['super'])
    assert client.get('/admin/data-tools/media').status_code == 200
    first = client.get('/admin/data-tools/media?format=json')
    baseline = first.get_json()
    assert first.cache_control.no_store and baseline['duplicates'] and not baseline['missing']
    assert {'root': 'uploads', 'path': unused} in baseline['orphans']
    assert {'root': 'uploads', 'path': old_cover} not in baseline['orphans']
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(baseline), encoding='utf-8')
    (root / old_cover).write_bytes(b'changed')
    result = application.test_cli_runner().invoke(args=['media-report', '--baseline', str(path)])
    assert result.exit_code == 0, result.output
    checked = json.loads(result.output)
    assert {'root': 'uploads', 'path': old_cover} in checked['changed']
    assert checked['comparison']['changed'] == [{'root': 'uploads', 'path': old_cover}]
    with application.app_context(), pytest.raises(RuntimeError, match='素材内容与上传记录不一致'):
        create_backup(a.db.engine, application.config)
    (root / old_cover).unlink()
    assert {'root': 'uploads', 'path': old_cover} in client.get('/admin/data-tools/media?format=json').get_json()['missing']
    (root / old_cover).write_bytes(png_bytes())
    preview = client.post('/admin/data-tools/cleanup/preview', data={'carousel': '1'}).get_data(as_text=True)
    token = re.search(r'name="cleanup_token" value="([^\"]+)"', preview).group(1)
    assert client.post('/admin/data-tools/cleanup/confirm', data={'cleanup_token': token, 'confirm': '1'}).status_code == 302
    assert (root / old_cover).is_file() and not (root / unused).is_file()
    with application.app_context():
        assert a.Match.query.filter_by(tournament_id=ids['season'], settlement_state='settled').count() == 1
        batch = create_backup(a.db.engine, application.config)
        assert verify_backup(batch)['rows']['upload_assets'] >= 4


@pytest.mark.maintenance
def test_schema_six_upgrade_preserves_legacy_files(application):
    ids = application.test_ids
    root = Path(application.config['UPLOAD_ROOT'])
    with application.app_context():
        a.db.session.get(a.User, ids['users'][0]).avatar = 'legacy.png'
        (root / 'avatars/legacy.png').write_bytes(b'legacy')
        a.db.session.commit()
        engine = a.db.engine
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP TABLE upload_assets')
            connection.exec_driver_sql('UPDATE schema_version SET version=6')
        batch = migrate_database(engine, application.config)
        assert verify_backup(batch)['schema_version'] == 6
        assert_schema_current(engine)
        assert inspect(engine).has_table('upload_assets')
        assert a.UploadAsset.query.count() == 0
        assert migrate_database(engine, application.config) is None
    assert application.test_client().get('/static/uploads/avatars/legacy.png').data == b'legacy'
