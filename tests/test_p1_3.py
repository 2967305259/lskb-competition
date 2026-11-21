from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, text

import app as a
from backup import restore_backup, verify_backup
from conftest import login
from test_p0 import complete, settle, submit


def test_export_includes_current_formal_results_adjustments_and_ties(application, client):
    ids = application.test_ids
    assert submit(application, client, 0, 0).status_code == 302
    assert submit(application, client, 1, 0).status_code == 302
    assert settle(application, client).status_code == 302
    login(client, ids['admin'])
    for team, member in zip(ids['teams'], ids['members']):
        assert client.post(f'/admin/team/{team}/update-scores', data={
            f'score_{member}': '10', 'reason': 'test',
        }).status_code == 302

    with application.app_context():
        a.Setting.get_instance().advance_count = 1
        a.db.session.get(a.Team, ids['teams'][0]).team_name = '=2+2'
        archived = a.Tournament(name='旧届', season_number=99, is_active=False)
        a.db.session.add(archived)
        a.db.session.flush()
        a.db.session.add(a.Team(team_name='只在旧届', captain_id=ids['users'][0], tournament_id=archived.id))
        a.db.session.add(a.Match(
            tournament_id=ids['season'], parent_id=ids['match'], player_a_id=ids['users'][0],
            player_b_id=ids['users'][1], team_a_id=ids['teams'][0], team_b_id=ids['teams'][1], roguelike='water',
        ))
        a.db.session.commit()

    response = client.get('/admin/export')
    assert response.status_code == 200
    workbook = load_workbook(BytesIO(response.data))
    assert workbook.sheetnames == ['报名信息', '比赛结果', '队伍积分榜', '队员积分榜']
    assert list(workbook['报名信息'].values)[1][0] == '=2+2'
    assert workbook['报名信息']['A2'].data_type == 's'
    assert workbook['比赛结果'].max_row == 2
    assert workbook['比赛结果']['F2'].value == 0
    assert workbook['比赛结果']['I2'].value == 0
    assert [row[3:] for row in list(workbook['队员积分榜'].values)[1:]] == [(10, 10, '一致')] * 2
    standings = workbook['队伍积分榜']
    rows = list(standings.iter_rows(min_row=2, max_row=3, max_col=5, values_only=True))
    assert [row[0] for row in rows] == [1, 1]
    assert [row[-2:] for row in rows] == [(5000, '是')] * 2
    assert standings.auto_filter.ref == 'A1:E3'

    with application.app_context():
        a.Tournament.get_active().is_active = False
        a.db.session.commit()
    assert client.get('/admin/export').status_code == 409


@pytest.mark.maintenance
def test_web_backup_download_and_isolated_restore(application, client, tmp_path):
    complete(application, client)
    root = Path(application.config['BACKUP_DIR'])
    ids = application.test_ids
    media_samples = {'avatars/restored.png': b'avatar', 'teams/restored.png': b'team-photo'}
    for name, content in media_samples.items():
        (Path(application.config['UPLOAD_ROOT']) / name).write_bytes(content)
    with application.app_context():
        a.db.session.get(a.User, ids['users'][0]).avatar = 'restored.png'
        a.db.session.get(a.Team, ids['teams'][0]).team_photo = 'restored.png'
        a.db.session.commit()
    login(client, ids['admin'])
    assert client.post('/admin/data-tools/backup').status_code == 403
    login(client, ids['super'])
    assert client.post('/admin/data-tools/backup').status_code == 302
    batch, = root.iterdir()
    manifest = verify_backup(batch)
    with application.app_context():
        assert manifest['season_number'] == a.Tournament.get_active().season_number
    base = f'/admin/data-tools/backup/{batch.name}'
    login(client, ids['admin'])
    assert client.get(f'{base}/manifest.json').status_code == 403
    login(client, ids['super'])
    assert batch.name.encode() in client.get('/admin/data-tools').data
    settings = client.get('/admin/tournament-settings')
    assert manifest['created_at'].encode() in settings.data
    assert b'/admin/data-tools' in settings.data
    downloaded = tmp_path / 'downloaded-batch'
    downloaded.mkdir()
    for name in ('manifest.json', *manifest['files']):
        response = client.get(f'{base}/{name}')
        assert response.status_code == 200
        assert response.cache_control.no_store
        assert f"season-{manifest['season_number']}-{batch.name}-{name}" in response.headers['Content-Disposition']
        assert response.data == (batch / name).read_bytes()
        (downloaded / name).write_bytes(response.data)
        response.close()
    assert client.get(f'{base}/%2e%2e/manifest.json').status_code == 404

    with application.app_context():
        url = a.db.engine.url
        admin = None
        if url.get_backend_name() == 'mysql':
            admin = create_engine(url.set(database=None))
            database_name = url.database + '_p13'
            with admin.begin() as connection:
                connection.execute(text('CREATE DATABASE ' + database_name))
            destination = create_engine(url.set(database=database_name))
        else:
            destination = create_engine(f"sqlite:///{tmp_path / 'restored-web.db'}")
        try:
            media = tmp_path / 'restored-web-media'
            restore_backup(downloaded, destination, media, manifest['code_version'])
            with destination.connect() as connection:
                restored = connection.execute(text(
                    'SELECT player_a_score, player_a_multiplier, context_version, settlement_state FROM matches'
                )).one()
                assert restored[0] == 100 and restored[1] > 0 and restored[2] > 0 and restored[3] == 'settled'
                assert connection.execute(text('SELECT COUNT(*) FROM score_logs')).scalar() >= 1
            restored_config = dict(application.config, SQLALCHEMY_DATABASE_URI=destination.url,
                                   UPLOAD_ROOT=str(media / 'uploads'), IMAGE_DIR=str(media / 'image'),
                                   STATE_DIR=str(tmp_path / 'restored-state'), BACKUP_DIR=str(tmp_path / 'restored-backups'))
            restored_app = a.create_app('testing', restored_config)
            try:
                restored_client = restored_app.test_client()
                for path in ('/', '/tournament/', '/history'):
                    assert restored_client.get(path).status_code == 200
                screenshot = next((media / 'uploads' / 'screenshots').iterdir())
                assert restored_client.get(f'/static/uploads/screenshots/{screenshot.name}').status_code == 404
                login(restored_client, ids['users'][0])
                media_samples[f'screenshots/{screenshot.name}'] = b'image'
                for name, content in media_samples.items():
                    assert restored_client.get(f'/static/uploads/{name}').data == content
                    assert (Path(application.config['UPLOAD_ROOT']) / name).read_bytes() == content
            finally:
                with restored_app.app_context():
                    a.db.engine.dispose()
            with a.db.engine.connect() as source:
                assert source.execute(text('SELECT player_a_score FROM matches')).scalar() == 100
        finally:
            destination.dispose()
            if admin:
                with admin.begin() as connection:
                    connection.execute(text('DROP DATABASE ' + database_name))
                admin.dispose()


@pytest.mark.maintenance
def test_web_backup_retention_and_missing_media_failure(application, client):
    ids = application.test_ids
    login(client, ids['super'])
    root = Path(application.config['BACKUP_DIR'])
    for _ in range(6):
        assert client.post('/admin/data-tools/backup').status_code == 302
    batches = sorted(root.iterdir())
    assert len(batches) == 5
    for batch in batches:
        verify_backup(batch)
    batch = batches[-1]
    asset = batch / 'media.tar.gz'
    data = asset.read_bytes()
    asset.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    assert client.get(f'/admin/data-tools/backup/{batch.name}/manifest.json').status_code == 404

    with application.app_context():
        a.db.session.get(a.User, ids['users'][0]).avatar = 'missing.png'
        a.db.session.commit()
    response = client.post('/admin/data-tools/backup', follow_redirects=True)
    assert '备份失败'.encode() in response.data
    assert sorted(root.iterdir()) == batches
