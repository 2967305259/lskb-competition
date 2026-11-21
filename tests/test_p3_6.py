from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import re

import pytest

import app as a
from conftest import login, png_bytes
from retention import CLEANUP_ACTION
from test_p0 import transition_data

TOOLS = '/admin/data-tools'
PREVIEW = f'{TOOLS}/cleanup/preview'
CONFIRM = f'{TOOLS}/cleanup/confirm'


def upload(application, name):
    path = Path(application.config['UPLOAD_ROOT']) / name
    path.write_bytes(name.encode())
    return path


def preview(client, **options):
    response = client.post(PREVIEW, data=options)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def token(page):
    return re.search(r'name="cleanup_token" value="([^"]+)"', page)[1]


def confirm(client, preview_token, **kwargs):
    return client.post(CONFIRM, data={'cleanup_token': preview_token, 'confirm': '1'}, **kwargs)


def business_rows():
    return {model.__tablename__: a.db.session.execute(a.db.select(model.__table__).order_by(model.id)).all()
            for model in (a.Match, a.Team, a.TeamMember, a.ScoreLog)}


def setup_archive(application):
    ids = application.test_ids
    with application.app_context():
        old = a.Tournament(name='往届清理样本', season_number=1, is_active=False, status='finished')
        a.db.session.add(old)
        a.db.session.flush()
        teams = [a.Team(team_name=name, captain_id=user_id, tournament_id=old.id)
                 for name, user_id in zip(('old-a', 'old-b'), ids['users'])]
        a.db.session.add_all(teams)
        a.db.session.flush()
        for team in teams:
            a.db.session.add(a.TeamMember(team_id=team.id, user_id=team.captain_id, roguelike='water'))
        old_match = a.Match(tournament_id=old.id, team_a_id=teams[0].id, team_b_id=teams[1].id,
                            player_a_id=ids['users'][0], player_b_id=ids['users'][1], roguelike='water',
                            status='finished', review_status='approved')
        a.db.session.add(old_match)
        a.db.session.commit()
        return old.id, old_match.id, teams[0].id


def test_preview_cancel_and_cleanup_protect_all_business_references(application, client, tmp_path):
    ids = application.test_ids
    old, old_match, old_team = setup_archive(application)
    linked_screenshot = f'screenshots/screenshot_{old_match}_{ids["users"][0]}_linked.png'
    orphan_screenshot = f'screenshots/screenshot_{old_match}_{ids["users"][0]}_unused.png'
    kept = ('avatars/current.png', 'avatars/old-carousel.png', 'avatars/encoded-ref.png', 'teams/old-team.png',
            linked_screenshot, f'screenshots/screenshot_{ids["match"]}_42_current.png',
            'screenshots/screenshot_99999_42_unknown.png')
    removed = ('avatars/unused.png', 'teams/unused.png', orphan_screenshot)
    paths = {name: upload(application, name) for name in (*kept, *removed)}
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'outside')
    link = paths['avatars/unused.png'].parent / 'link.png'
    link.symlink_to(outside)
    with application.app_context():
        a.db.session.get(a.User, ids['users'][0]).avatar = 'current.png'
        a.db.session.get(a.Team, old_team).team_photo = 'old-team.png'
        a.db.session.get(a.Match, old_match).match_note = f'[截图] /static/uploads/{linked_screenshot}'
        a.db.session.add(a.CarouselItem(tournament_id=old, file_path='/static/uploads/avatars/old-carousel.png',
                                        is_active=False))
        a.db.session.add(a.Rule(title='素材链接', content='<img src="static/uploads/avatars/encoded%2Dref.png">'))
        a.db.session.add(a.SystemLog(action='old_entry', created_at=datetime.utcnow() - timedelta(days=100)))
        a.db.session.add(a.SystemLog(action='recent_entry'))
        a.db.session.add(a.ScoreLog(match_id=old_match, modified_by=ids['admin'], old_result='a' * 33000,
                                     created_at=datetime.utcnow() - timedelta(days=100)))
        a.db.session.commit()
        before = business_rows()

    login(client, ids['admin'])
    assert '预览清理范围' not in client.get(TOOLS).get_data(as_text=True)
    assert client.post(PREVIEW, data={'avatars': '1'}).status_code == 403
    assert confirm(client, 'invalid').status_code == 403
    assert client.get(f'{TOOLS}/score-logs').status_code == 403

    login(client, ids['super'])
    page = preview(client, avatars='1', teams='1', screenshots='older_seasons', recent_seasons='1',
                   logs='1', log_days='30')
    for label in ('头像', '队伍照片', '比赛截图', '系统日志'):
        assert f'{label} <strong>1</strong>' in page
    assert all(name in page for name in removed)
    assert all(path.is_file() for path in paths.values())
    assert client.get(TOOLS).status_code == 200
    assert client.post(CONFIRM, data={'cleanup_token': token(page)}).status_code == 400
    with application.app_context():
        assert business_rows() == before
        assert a.SystemLog.query.filter_by(action=CLEANUP_ACTION).count() == 0
        assert a.SystemLog.query.filter_by(action='old_entry').count() == 1

    response = confirm(client, token(page), follow_redirects=True)
    assert '已删除 3 个素材文件和 1 条系统日志' in response.get_data(as_text=True)
    assert all(paths[name].is_file() for name in kept)
    assert all(not paths[name].exists() for name in removed)
    assert link.is_symlink() and outside.read_bytes() == b'outside'
    assert confirm(client, token(page)).status_code == 409
    with application.app_context():
        assert business_rows() == before
        assert a.SystemLog.query.filter_by(action='old_entry').count() == 0
        assert a.SystemLog.query.filter_by(action='recent_entry').count() == 1
        audit = a.SystemLog.query.filter_by(action=CLEANUP_ACTION).one()
        assert audit.user_id == ids['super']
        detail = json.loads(audit.detail)
        assert detail['state'] == 'completed'
        assert detail['deleted_files'] == {'avatars': 1, 'teams': 1, 'screenshots': 1}
        assert detail['deleted_system_logs'] == 1

    export = client.get(f'{TOOLS}/score-logs')
    assert export.status_code == 200 and export.cache_control.no_store
    assert export.get_json()[0]['old_result'] == 'a' * 33000


def test_screenshot_review_requires_approval_and_a_verified_match(application, client):
    ids = application.test_ids
    _, old_match, _ = setup_archive(application)
    player_id = ids['users'][0]
    approved = upload(application, f'screenshots/screenshot_{old_match}_{player_id}_approved.webp')
    ambiguous = upload(application, f'screenshots/screenshot_{old_match}_999_wrong-player.webp')
    rejected = upload(application, f'screenshots/screenshot_{ids["match"]}_{player_id}_rejected.webp')
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        match.review_status = 'rejected'
        pending_match = a.Match(tournament_id=ids['season'], team_a_id=ids['teams'][0], team_b_id=ids['teams'][1],
                                player_a_id=player_id, player_b_id=ids['users'][1], roguelike='water')
        a.db.session.add(pending_match)
        a.db.session.commit()
        pending = upload(application, f'screenshots/screenshot_{pending_match.id}_{player_id}_pending.webp')
    login(client, ids['super'])
    page = preview(client, screenshots='approved')
    assert '比赛截图 <strong>1</strong>' in page
    assert confirm(client, token(page)).status_code == 302
    assert not approved.exists()
    assert pending.exists() and rejected.exists() and ambiguous.exists()


def test_stale_or_forged_preview_cannot_delete_replaced_or_referenced_file(application, client):
    ids = application.test_ids
    path = upload(application, 'avatars/unused.png')
    login(client, ids['super'])
    first = token(preview(client, avatars='1'))
    assert confirm(client, first + 'invalid').status_code == 400
    path.write_bytes(b'replaced')
    assert confirm(client, first).status_code == 409
    second = token(preview(client, avatars='1'))
    with application.app_context():
        a.db.session.get(a.User, ids['users'][0]).avatar = path.name
        a.db.session.commit()
    assert confirm(client, second).status_code == 409
    assert path.read_bytes() == b'replaced'
    assert '当前范围内没有可清理' in preview(client, avatars='1')


def test_preview_is_bound_to_super_admin_and_expires(application, client, monkeypatch):
    import itsdangerous

    path = upload(application, 'teams/unused.png')
    with application.app_context():
        other = a.User(username='other', nickname='other', role='super_admin', password_hash='fixture')
        a.db.session.add(other)
        a.db.session.commit()
        other_id = other.id
    super_id = application.test_ids['super']
    login(client, super_id)
    original = token(preview(client, teams='1'))
    login(client, other_id)
    assert confirm(client, original).status_code == 403
    login(client, super_id)
    get_timestamp = itsdangerous.TimestampSigner.get_timestamp
    monkeypatch.setattr(itsdangerous.TimestampSigner, 'get_timestamp', lambda self: get_timestamp(self) + 901)
    response = confirm(client, original)
    assert response.status_code == 400 and '已过期' in response.get_data(as_text=True)
    assert path.is_file()


def test_file_failure_is_audited_and_the_preview_cannot_be_replayed(application, client, monkeypatch):
    path = upload(application, 'avatars/unused.png')
    login(client, application.test_ids['super'])
    preview_token = token(preview(client, avatars='1'))

    def fail(*args):
        raise OSError('isolated test failure')

    monkeypatch.setattr('retention._unlink_candidate', fail)
    response = confirm(client, preview_token, follow_redirects=True)
    assert '1 个文件未删除' in response.get_data(as_text=True)
    assert path.is_file()
    assert confirm(client, preview_token).status_code == 409
    with application.app_context():
        detail = json.loads(a.SystemLog.query.filter_by(action=CLEANUP_ACTION).one().detail)
        assert detail['state'] == 'partial'
        assert detail['deleted_files'] == {}
        assert detail['failed_files'] == ['avatars/unused.png']


def test_audit_write_failure_keeps_files_and_rolls_back_log_deletion(application, client, monkeypatch):
    path = upload(application, 'avatars/unused.png')
    with application.app_context():
        a.db.session.add(a.SystemLog(action='old_entry', created_at=datetime.utcnow() - timedelta(days=10)))
        a.db.session.commit()
    login(client, application.test_ids['super'])
    preview_token = token(preview(client, avatars='1', logs='1', log_days='1'))

    def fail(*args):
        raise RuntimeError('isolated audit failure')

    monkeypatch.setattr(a, 'log_system_action', fail)
    with pytest.raises(RuntimeError, match='isolated audit failure'):
        confirm(client, preview_token)
    assert path.is_file()
    with application.app_context():
        assert a.SystemLog.query.filter_by(action='old_entry').count() == 1


def test_concurrent_confirmation_runs_once(application, client):
    super_id = application.test_ids['super']
    path = upload(application, 'avatars/unused.png')
    login(client, super_id)
    preview_token = token(preview(client, avatars='1'))

    def run(_):
        with application.test_client() as worker:
            login(worker, super_id)
            return confirm(worker, preview_token).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == [302, 409]
    assert not path.exists()
    with application.app_context():
        assert a.SystemLog.query.filter_by(action=CLEANUP_ACTION).count() == 1


def test_replacing_photos_and_switching_season_preserve_files_by_default(application, client):
    ids = application.test_ids
    avatar = upload(application, 'avatars/previous.png')
    photo = upload(application, 'teams/previous.png')
    with application.app_context():
        a.db.session.get(a.User, ids['users'][0]).avatar = avatar.name
        a.db.session.get(a.Team, ids['teams'][0]).team_photo = photo.name
        a.db.session.add(a.SystemLog(action='old_entry', created_at=datetime.utcnow() - timedelta(days=100)))
        a.db.session.commit()
    login(client, ids['users'][0])
    for path, field in (('/captain/upload-avatar', 'avatar'), ('/captain/upload-team-avatar', 'team_avatar')):
        assert client.post(path, data={field: (BytesIO(png_bytes()), 'new.png')}).status_code == 302
    assert avatar.is_file() and photo.is_file()
    assert client.post('/admin/new-season', data=transition_data(application, client, force=True)).status_code == 302
    assert avatar.is_file() and photo.is_file()
    with application.app_context():
        assert a.SystemLog.query.filter_by(action='old_entry').count() == 1
        assert a.SystemLog.query.filter_by(action=CLEANUP_ACTION).count() == 0
