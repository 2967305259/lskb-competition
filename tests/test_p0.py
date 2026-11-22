from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import inspect, text, create_engine
import app as a
from app import db
from conftest import login, png_bytes
from backup import create_backup, verify_backup, restore_backup
from migrations import migrate_database, assert_schema_current
from scoring import recalculate_all_scores
from season_safety import SeasonError, set_score, capture_match_rule, save_context


def submit(application, client, side, score):
    ids = application.test_ids
    login(client, ids['users'][side])
    return client.post(f"/tournament/match/{ids['match']}/submit-result", data={
        'score': str(score), 'difficulty': '14', 'screenshots': (BytesIO(png_bytes()), 'score.png')})


def settle(application, client):
    login(client, application.test_ids['admin'])
    return client.post(f"/admin/match/{application.test_ids['match']}/edit-result", data={'action': 'win_a'})


def transition_data(application, client, force=False):
    login(client, application.test_ids['admin'])
    response = client.get('/admin/tournament-settings')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    token = re.search('name="idempotency_key" value="([^"]+)"', html).group(1)
    return {'idempotency_key': token, 'expected_current_season_id': str(application.test_ids['season']),
            'force': '1' if force else '', 'force_reason': '隔离测试强制换季' if force else '',
            'score_rules_mode': 'initial'}


def complete(application, client):
    assert submit(application, client, 0, 100).status_code == 302
    assert submit(application, client, 1, 90).status_code == 302
    assert settle(application, client).status_code == 302


def test_import_and_factory_have_no_side_effects(tmp_path):
    database = tmp_path / 'source.db'
    connection = sqlite3.connect(database)
    try:
        connection.execute('CREATE TABLE existing_data (value TEXT)')
        connection.execute("INSERT INTO existing_data VALUES ('sentinel')")
        connection.commit()
    finally:
        connection.close()
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    (uploads / 'original.png').write_bytes(b'existing upload')
    def snapshot():
        return {path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
                for path in tmp_path.rglob('*')}

    before = snapshot()
    code = """
import app
instance = app.create_app('testing')
assert not hasattr(app, 'app')
assert instance.config['SQLALCHEMY_DATABASE_URI'] == 'sqlite:///:memory:'
"""
    env = dict(os.environ, DATABASE_URL=f'sqlite:///{database}',
               DB_FILE=str(tmp_path / 'alternate.db'), STATE_DIR=str(tmp_path / 'state'),
               UPLOAD_ROOT=str(uploads), IMAGE_DIR=str(tmp_path / 'images'), BACKUP_DIR=str(tmp_path / 'backups'))
    subprocess.run([sys.executable, '-c', code], env=env, check=True)
    assert snapshot() == before


def test_zero_submission_and_explicit_settlement(application, client):
    assert submit(application, client, 0, 0).status_code == 302
    assert submit(application, client, 1, 100).status_code == 302
    with application.app_context():
        match = db.session.get(a.Match, application.test_ids['match'])
        assert match.player_a_score == 0
        assert match.status == 'submitted'
        recalculate_all_scores()
        db.session.commit()
        assert db.session.get(a.Team, application.test_ids['teams'][1]).team_score == 0
    assert settle(application, client).status_code == 302
    with application.app_context():
        match = db.session.get(a.Match, application.test_ids['match'])
        assert match.settlement_state == 'settled'
        assert match.player_b_multiplier == Decimal('1.2000')
        assert db.session.get(a.TeamMember, application.test_ids['members'][1]).final_score == 120
        assert a.ScoreLog.query.first().new_result and len(a.ScoreLog.query.first().new_result) > 20


def test_snapshot_survives_global_rule_change_and_raw_correction(application, client):
    complete(application, client)
    login(client, application.test_ids['admin'])
    with application.app_context():
        rid = a.Roguelike.query.filter_by(key='water').one().id
    assert client.post(f'/admin/roguelike/{rid}/edit', data={'difficulty_step': '.2', 'is_active': '1'}).status_code == 302
    assert client.post('/admin/reset-scores').status_code == 302
    with application.app_context():
        member = db.session.get(a.TeamMember, application.test_ids['members'][0])
        assert member.final_score == 120
    assert client.post(f"/admin/match/{application.test_ids['match']}/edit-result",
        data={'action': 'win_a', 'score_a': '110', 'reason': '纠正原始成绩'}).status_code == 302
    with application.app_context():
        assert db.session.get(a.Match, application.test_ids['match']).player_a_multiplier == Decimal('1.2000')
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 132


def test_forfeit_keeps_score_allows_opponent_and_defers_settlement(application, client):
    assert submit(application, client, 0, 100).status_code == 302
    response = client.post(f"/tournament/match/{application.test_ids['match']}/forfeit", data={'reason': '主动弃权'})
    assert response.status_code == 302
    with application.app_context():
        match = db.session.get(a.Match, application.test_ids['match'])
        assert match.player_a_score == 100 and match.player_b_score is None
        assert match.winner_id == application.test_ids['users'][1]
        assert match.settlement_state == 'pending'
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 0
    assert submit(application, client, 1, 80).status_code == 302
    assert settle(application, client).status_code == 302
    with application.app_context():
        match = db.session.get(a.Match, application.test_ids['match'])
        assert match.forfeit_side == 'a' and match.player_a_score == 100
        assert match.winner_id == application.test_ids['users'][1]
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 120


def test_forfeit_without_submission_records_explicit_zero(application, client):
    login(client, application.test_ids['users'][0])
    assert client.post(f"/tournament/match/{application.test_ids['match']}/forfeit").status_code == 302
    with application.app_context():
        match = db.session.get(a.Match, application.test_ids['match'])
        assert match.player_a_score == 0 and match.score_origin_a == 'forfeit_zero'
        assert match.player_b_score is None
    login(client, application.test_ids['admin'])
    assert settle(application, client).status_code == 409


def test_split_submissions_target_whole_match_once(application, client):
    login(client, application.test_ids['admin'])
    with application.app_context():
        match = db.session.get(a.Match, application.test_ids['match'])
        from datetime import datetime
        match.scheduled_time = datetime(2026, 11, 1, 12)
        db.session.commit()
    assert client.post('/admin/schedule/split-match', data={'match_id': application.test_ids['match'], 'parts': 3}).status_code == 200
    with application.app_context():
        child_id = a.Match.query.filter_by(parent_id=application.test_ids['match']).first().id
    login(client, application.test_ids['users'][0])
    assert client.post(f'/tournament/match/{child_id}/submit-result', data={'score': '100', 'difficulty': '14'}).status_code == 302
    assert submit(application, client, 0, 100).status_code == 409
    assert submit(application, client, 1, 90).status_code == 302
    assert settle(application, client).status_code == 302
    with application.app_context():
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 120
        assert a.Match.query.filter_by(parent_id=application.test_ids['match'], player_a_score=None).count() == 2
    assert client.post('/admin/schedule/split-match', data={'match_id': application.test_ids['match'], 'parts': 2}).status_code == 409


def test_season_replay_idempotent_and_old_page_conflict(application, client):
    complete(application, client)
    data = transition_data(application, client)
    other = transition_data(application, client)
    assert client.post('/admin/new-season', data=data).status_code == 302
    assert client.post('/admin/new-season', data=data).status_code == 302
    assert client.post('/admin/new-season', data=other).status_code == 409
    with application.app_context():
        assert a.Tournament.query.count() == 2
        assert a.Tournament.query.filter_by(is_active=True).count() == 1
        assert a.SeasonTransition.query.count() == 1
        assert a.SystemLog.query.filter_by(action='new_season').count() == 1
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 120


def test_concurrent_transition_only_once(application, client):
    data = transition_data(application, client, True)
    def worker(_):
        with application.test_client() as c:
            login(c, application.test_ids['admin'])
            return c.post('/admin/new-season', data=data).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(worker, range(2))) == [302, 302]
    with application.app_context():
        assert a.Tournament.query.count() == 2
        assert a.SeasonTransition.query.count() == 1


def test_force_switch_requires_reason_and_freezes_old_submissions(application, client):
    data = transition_data(application, client)
    assert client.post('/admin/new-season', data=data).status_code == 409
    data.update(force='1', force_reason='明确保留未完成场次')
    assert client.post('/admin/new-season', data=data).status_code == 302
    with application.app_context():
        match = db.session.get(a.Match, application.test_ids['match'])
        assert match.status == 'confirmed' and match.settlement_state == 'pending'
        transition = a.SeasonTransition.query.one()
        assert json.loads(transition.unfinished_ids) == [match.id]
    assert submit(application, client, 0, 100).status_code == 403


@pytest.mark.parametrize('failure', ['backup', 'audit'])
def test_switch_failure_rolls_back(application, client, monkeypatch, failure):
    import season_safety
    data = transition_data(application, client, True)
    def fail(*args, **kwargs):
        raise RuntimeError('injected failure')
    monkeypatch.setattr(season_safety, 'create_backup' if failure == 'backup' else '_log', fail)
    with pytest.raises(RuntimeError):
        client.post('/admin/new-season', data=data)
    with application.app_context():
        assert a.Tournament.query.count() == 1
        assert a.Tournament.get_active().id == application.test_ids['season']
        assert a.SeasonTransition.query.count() == 0
        assert a.SystemLog.query.filter_by(action='new_season').count() == 0


@pytest.mark.parametrize('path', [
    '/admin/team/{team}/delete', '/admin/team/{team}/update-scores', '/admin/team/{team}/update',
    '/admin/team/{team}/update-member/{member}', '/admin/match/{match}/edit-result',
    '/admin/match/{match}/review', '/admin/schedule/update-time', '/admin/schedule/split-match',
    '/admin/schedule/delete-match', '/admin/schedule/confirm-match'])
def test_archived_direct_writes_rejected(application, client, path):
    complete(application, client)
    data = transition_data(application, client)
    assert client.post('/admin/new-season', data=data).status_code == 302
    ids = application.test_ids
    path = path.format(team=ids['teams'][0], member=ids['members'][0], match=ids['match'])
    assert client.post(path, data={'match_id': ids['match'], 'action': 'win_b', 'score_a': 999}).status_code == 403
    with application.app_context():
        assert db.session.get(a.Match, ids['match']).player_a_score == 100
        assert db.session.get(a.TeamMember, ids['members'][0]).final_score == 120


def test_archived_correction_super_only_historical_rule_and_audit(application, client):
    complete(application, client)
    data = transition_data(application, client)
    assert client.post('/admin/new-season', data=data).status_code == 302
    path = f"/admin/history-corrections/{application.test_ids['season']}"
    payload = {'match_id': application.test_ids['match'], 'score_a': '110', 'diff_a': '15', 'reason': '核对截图'}
    assert client.post(path, data=payload).status_code == 403
    login(client, application.test_ids['super'])
    assert client.post(path, data=dict(payload, reason='')).status_code == 400
    assert client.post(path, data=payload).status_code == 200
    with application.app_context():
        member = db.session.get(a.TeamMember, application.test_ids['members'][0])
        assert member.final_score == 143
        log = a.SystemLog.query.filter_by(action='historical_correction').one()
        detail = json.loads(log.detail)
        assert detail['before']['match']['player_a_score'] == '100'
        assert detail['after']['match']['player_a_score'] == '110'
        assert detail['reason'] == '核对截图'
        assert a.Tournament.get_active().season_number == 4


def test_current_rebuild_does_not_touch_archive(application, client):
    complete(application, client)
    data = transition_data(application, client)
    assert client.post('/admin/new-season', data=data).status_code == 302

    ids = application.test_ids
    with application.app_context():
        season = a.Tournament.get_active()
        season.bonus_score = 200
        track = a.Roguelike.query.filter_by(key='water').one()
        track.difficulty_min, track.difficulty_max, track.difficulty_step = 10, 13, 0.25
        teams = [a.Team(team_name=name, captain_id=user_id, tournament_id=season.id)
                 for name, user_id in zip(('alpha', 'beta'), ids['users'])]
        db.session.add_all(teams)
        db.session.flush()
        members = [a.TeamMember(team_id=team.id, user_id=team.captain_id, roguelike='water') for team in teams]
        match = a.Match(tournament_id=season.id, player_a_id=ids['users'][0], player_b_id=ids['users'][1],
                        team_a_id=teams[0].id, team_b_id=teams[1].id, roguelike='water', status='confirmed')
        db.session.add_all([*members, match])
        db.session.commit()
        new_match = match.id
        new_members = [member.id for member in members]
        new_teams = [team.id for team in teams]

    for user_id, score in zip(ids['users'], (101, 80)):
        login(client, user_id)
        assert client.post(f'/tournament/match/{new_match}/submit-result', data={
            'score': str(score), 'difficulty': '12', 'screenshots': (BytesIO(png_bytes()), 'score.png'),
        }).status_code == 302
    login(client, ids['admin'])
    assert client.post(f'/admin/match/{new_match}/edit-result', data={'action': 'win_a'}).status_code == 302
    with application.app_context():
        for player_id, team_id in zip(new_members, new_teams):
            db.session.get(a.TeamMember, player_id).final_score = 3
            db.session.get(a.Team, team_id).team_score = 5
        db.session.commit()

    assert client.post('/admin/reset-scores').status_code == 302
    with application.app_context():
        assert [db.session.get(a.TeamMember, mid).final_score for mid in ids['members']] == [120, 108]
        assert [db.session.get(a.Team, tid).team_score for tid in ids['teams']] == [60000, 54000]
        assert [db.session.get(a.TeamMember, mid).final_score for mid in new_members] == [151, 120]
        assert [db.session.get(a.Team, tid).team_score for tid in new_teams] == [30200, 24000]
        assert db.session.get(a.Match, ids['match']).player_a_multiplier == Decimal('1.2000')
        assert db.session.get(a.Match, new_match).player_a_multiplier == Decimal('1.5000')
        with pytest.raises(SeasonError):
            recalculate_all_scores(ids['season'])


@pytest.mark.maintenance
def test_backup_and_isolated_restore(application, client, tmp_path):
    complete(application, client)
    with application.app_context():
        db.session.remove()
        batch = create_backup(db.engine, application.config)
        manifest = verify_backup(batch)
        assert manifest['complete'] and manifest['rows']['matches'] == 1
        if db.engine.dialect.name == 'sqlite':
            target = create_engine('sqlite:///' + str(tmp_path / 'restored.db'))
        else:
            source_url = db.engine.url
            name = source_url.database + '_restore'
            admin = create_engine(source_url.set(database=None))
            with admin.begin() as conn:
                conn.execute(text('CREATE DATABASE ' + name))
            target = create_engine(source_url.set(database=name))
        try:
            with pytest.raises(RuntimeError, match='代码版本不匹配'):
                restore_backup(batch, target, tmp_path / 'wrong-version', 'other-version')
            assert not inspect(target).get_table_names()
            assert not (tmp_path / 'wrong-version').exists()
            restore_backup(batch, target, tmp_path / 'restored-media', application.config['APP_VERSION'])
            with target.connect() as conn:
                assert conn.execute(text('SELECT player_a_score FROM matches')).scalar() == 100
                assert conn.execute(text('SELECT COUNT(*) FROM score_logs')).scalar() == 1
            assert (tmp_path / 'restored-media' / 'image' / '1.png').read_bytes() == b'fixture-media'
            from app import create_app
            restored_config = dict(application.config)
            restored_config.update(SQLALCHEMY_DATABASE_URI=target.url,
                UPLOAD_ROOT=str(tmp_path / 'restored-media' / 'uploads'), IMAGE_DIR=str(tmp_path / 'restored-media' / 'image'),
                STATE_DIR=str(tmp_path / 'restored-state'), BACKUP_DIR=str(tmp_path / 'restored-backups'))
            restored = create_app('testing', restored_config)
            with restored.test_client() as restored_client:
                for path in ('/', '/tournament/', '/history'):
                    assert restored_client.get(path).status_code == 200
                login(restored_client, application.test_ids['admin'])
                assert restored_client.get(f"/tournament/match/{application.test_ids['match']}").status_code == 200
                screenshot = next((tmp_path / 'restored-media' / 'uploads' / 'screenshots').iterdir())
                assert restored_client.get('/static/uploads/screenshots/' + screenshot.name).data == png_bytes()
            with restored.app_context():
                db.session.remove()
                db.engine.dispose()

            with pytest.raises(RuntimeError):
                restore_backup(batch, db.engine, tmp_path / 'forbidden', application.config['APP_VERSION'])
        finally:
            target.dispose()
            if db.engine.dialect.name == 'mysql':
                with admin.begin() as conn:
                    conn.execute(text('DROP DATABASE ' + name))
                admin.dispose()


def test_referenced_track_cannot_change_key_or_delete(application, client):
    login(client, application.test_ids['admin'])
    with application.app_context():
        rid = a.Roguelike.query.filter_by(key='water').one().id
    assert client.post(f'/admin/roguelike/{rid}/edit', data={'key': 'other'}).status_code == 409
    assert client.post(f'/admin/roguelike/{rid}/delete').status_code == 409
    assert client.post(f'/admin/roguelike/{rid}/edit', data={'is_active': '0', 'name': '当前新名'}).status_code == 302


def test_no_active_rebuild_has_no_effect_and_multiple_active_rejected(application):
    with application.app_context():
        member = db.session.get(a.TeamMember, application.test_ids['members'][0])
        member.final_score = 77
        db.session.commit()
        with db.engine.begin() as conn:
            conn.execute(text('UPDATE tournaments SET is_active=0'))
        assert recalculate_all_scores()['tournament_id'] is None
        db.session.commit()
        assert db.session.get(a.TeamMember, member.id).final_score == 77
        with db.engine.begin() as conn:
            conn.execute(text('UPDATE tournaments SET is_active=1'))
            conn.execute(text("INSERT INTO tournaments (name, season_number, is_active) VALUES ('duplicate-active', 99, 1)"))
        db.session.expire_all()
        with pytest.raises(SeasonError):
            recalculate_all_scores()


def test_historical_rule_text_and_track_name_stay_frozen(application, client):
    with application.app_context():
        db.session.add(a.Rule(title='当届规则', content='old-season-rule', is_published=True))
        db.session.commit()
    complete(application, client)
    data = transition_data(application, client)
    assert client.post('/admin/new-season', data=data).status_code == 302
    with application.app_context():
        rid = a.Roguelike.query.filter_by(key='water').one().id
        rule_id = a.Rule.query.one().id
    assert client.post(f'/admin/roguelike/{rid}/edit', data={'name': '新赛季赛道名', 'is_active': '1'}).status_code == 302
    assert client.post(f'/admin/rules/{rule_id}/edit', data={'title': '新规则', 'content': 'new-season-rule', 'is_published': '1'}).status_code == 302
    response = client.get(f"/history/{application.test_ids['season']}")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'old-season-rule' in html and 'new-season-rule' not in html
    assert '水月与深蓝之树' in html


def test_adjustments_survive_rebuild_and_revoke(application, client):
    complete(application, client)
    ids = application.test_ids
    login(client, ids['admin'])
    assert client.post(f"/admin/team/{ids['teams'][0]}/update-scores", data={f"score_{ids['members'][0]}": '130', 'reason': '调整 +10'}).status_code == 302
    assert client.post('/admin/reset-scores').status_code == 302
    with application.app_context():
        assert db.session.get(a.Team, ids['teams'][0]).team_score == 65000
        adjustment_id = a.ScoreAdjustment.query.one().id
    assert client.post(f'/admin/adjustments/{adjustment_id}/revoke', data={'reason': '撤销误调'}).status_code == 200
    assert client.post(f'/admin/adjustments/{adjustment_id}/revoke', data={'reason': '重复请求'}).status_code == 200
    with application.app_context():
        assert db.session.get(a.TeamMember, ids['members'][0]).final_score == 120
        assert a.ScoreAdjustment.query.one().revoked
        assert a.SystemLog.query.filter_by(action='revoke_adjustment').count() == 1


def test_correction_audit_failure_rolls_back_scores(application, client, monkeypatch):
    complete(application, client)
    data = transition_data(application, client)
    assert client.post('/admin/new-season', data=data).status_code == 302
    login(client, application.test_ids['super'])
    def fail(*args, **kwargs):
        raise RuntimeError('audit failed')
    monkeypatch.setattr(a, 'log_system_action', fail)
    with pytest.raises(RuntimeError):
        client.post(f"/admin/history-corrections/{application.test_ids['season']}",
            data={'match_id': application.test_ids['match'], 'score_a': '999', 'reason': '必须回滚'})
    with application.app_context():
        assert db.session.get(a.Match, application.test_ids['match']).player_a_score == 100
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 120
        assert a.SystemLog.query.filter_by(action='historical_correction').count() == 0


@pytest.mark.maintenance
def test_missing_media_and_tampered_backup_are_rejected(application, client):
    with application.app_context():
        db.session.remove()
        batch = create_backup(db.engine, application.config)
        (batch / 'media.tar.gz').write_bytes(b'incomplete')
        with pytest.raises(RuntimeError):
            verify_backup(batch)
    data = transition_data(application, client, True)
    application.config['IMAGE_DIR'] += '-does-not-exist'
    with pytest.raises(RuntimeError, match='素材目录缺失'):
        client.post('/admin/new-season', data=data)
    with application.app_context():
        assert a.Tournament.query.count() == 1


@pytest.mark.maintenance
def test_wal_backup_restore_and_custom_media_route(application, tmp_path, client):
    with application.app_context():
        if db.engine.dialect.name != 'sqlite':
            pytest.skip('SQLite WAL-specific probe')
        db.session.remove()
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql('PRAGMA journal_mode=WAL').scalar() == 'wal'
        avatar = Path(application.config['UPLOAD_ROOT']) / 'avatars' / 'still-used.png'
        avatar.write_bytes(b'avatar-content')
        db.session.get(a.User, application.test_ids['users'][0]).avatar = avatar.name
        db.session.commit()
        batch = create_backup(db.engine, application.config)
        engine = create_engine('sqlite:///' + str(tmp_path / 'wal-restored.db'))
        restore_backup(batch, engine, tmp_path / 'wal-media', 'p0-v1')
        engine.dispose()
        assert (tmp_path / 'wal-media' / 'uploads' / 'avatars' / avatar.name).read_bytes() == b'avatar-content'
    response = client.get('/static/uploads/avatars/still-used.png')
    assert response.status_code == 200 and response.data == b'avatar-content'


@pytest.mark.parametrize('action', ['submit-result','forfeit','note','schedule','confirm-schedule'])
def test_archived_player_posts_never_create_files(application, client, action):
    data = transition_data(application, client, True)
    assert client.post('/admin/new-season', data=data).status_code == 302
    screenshot_dir = Path(application.config['UPLOAD_ROOT']) / 'screenshots'
    before = set(screenshot_dir.iterdir())
    login(client, application.test_ids['users'][0])
    assert client.post(f"/tournament/match/{application.test_ids['match']}/{action}", data={
        'score': '999', 'difficulty': '14', 'screenshots': (BytesIO(b'invalid'), 'new.png')}).status_code == 403
    assert set(screenshot_dir.iterdir()) == before


def test_archived_membership_without_matches_prevents_account_deletion(application, client):
    with application.app_context():
        user = a.User(username='history-only', nickname='history-only', role='player', password_hash='fixture')
        db.session.add(user)
        db.session.flush()
        user_id = user.id
        db.session.add(a.TeamMember(team_id=application.test_ids['teams'][0], user_id=user_id))
        db.session.commit()
    data = transition_data(application, client, True)
    assert client.post('/admin/new-season', data=data).status_code == 302
    assert client.post(f'/admin/user/{user_id}/delete').status_code == 409
    with application.app_context():
        assert db.session.get(a.User, user_id)
        assert a.TeamMember.query.filter_by(user_id=user_id).count() == 1


def test_multiple_active_returns_controlled_http_error(application, client):
    with application.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("INSERT INTO tournaments (name,season_number,is_active) VALUES ('bad-active',99,1)"))
    login(client, application.test_ids['admin'])
    response = client.post('/admin/reset-scores')
    assert response.status_code == 409
    assert '多个活跃赛季' in response.get_data(as_text=True)


def test_published_context_versions_are_preserved_and_bound_to_matches(application, client):
    assert submit(application, client, 0, 100).status_code == 302
    with application.app_context():
        match = db.session.get(a.Match, application.test_ids['match'])
        old_version = match.context_version
        original = a.SeasonContextVersion.query.filter_by(tournament_id=match.tournament_id, version=old_version).one().context
        rid = a.Roguelike.query.filter_by(key='water').one().id
    login(client, application.test_ids['admin'])
    assert client.post(f'/admin/roguelike/{rid}/edit', data={'name': '改名后的当前赛道', 'is_active': '1'}).status_code == 302
    with application.app_context():
        assert a.SeasonContextVersion.query.filter_by(tournament_id=application.test_ids['season'], version=old_version).one().context == original
        assert a.Tournament.get_active().snapshot_version > old_version
        assert db.session.get(a.Match, application.test_ids['match']).context_version == old_version


def test_orphan_match_cannot_silently_clear_existing_scores(application):
    with application.app_context():
        with db.engine.begin() as conn:
            conn.execute(text('UPDATE matches SET tournament_id=NULL,context_version=NULL'))
            conn.execute(text('UPDATE team_members SET final_score=123'))
        with pytest.raises(SeasonError, match='归属与本届队伍冲突'):
            recalculate_all_scores()
        assert all(m.final_score == 123 for m in a.TeamMember.query.all())


@pytest.mark.maintenance
def test_backup_rejects_missing_database_media_references(application, client):
    with application.app_context():
        user = db.session.get(a.User, application.test_ids['users'][0])
        user.avatar = 'missing-avatar.png'
        db.session.commit()
        db.session.remove()
        with pytest.raises(RuntimeError, match='素材引用缺失'):
            create_backup(db.engine, application.config)


def test_player_stats_are_current_by_default_and_count_formal_outcomes_once(application, client):
    complete(application, client)
    ids = application.test_ids
    with application.app_context():
        root = db.session.get(a.Match, ids['match'])
        # These rows must never become a second formal result for the player.
        db.session.add(a.Match(tournament_id=root.tournament_id, player_a_id=root.player_a_id,
                               player_b_id=root.player_b_id, team_a_id=root.team_a_id,
                               team_b_id=root.team_b_id, roguelike=root.roguelike,
                               parent_id=root.id, status='finished', winner_id=root.player_a_id,
                               match_type='match'))
        db.session.add(a.Match(tournament_id=root.tournament_id, player_a_id=root.player_a_id,
                               player_b_id=root.player_b_id, team_a_id=None, team_b_id=None,
                               roguelike=root.roguelike, status='finished', winner_id=root.player_a_id,
                               match_type='exhibition'))
        db.session.commit()
    login(client, ids['users'][0])
    response = client.get(f"/player/api/players/{ids['users'][0]}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['stats']['season_scope'] == 'current'
    assert payload['stats']['wins'] == 1
    assert payload['stats']['total_matches'] == 1
    assert len(payload['recent_matches']) == 1

    transition = transition_data(application, client)
    assert client.post('/admin/new-season', data=transition).status_code == 302
    response = client.get(f"/player/api/players/{ids['users'][0]}")
    assert response.get_json()['stats']['total_matches'] == 0
    response = client.get(f"/player/api/players/{ids['users'][0]}?season=all")
    assert response.get_json()['stats']['season_scope'] == 'all'
    assert response.get_json()['stats']['wins'] == 1
    named = client.get(f"/player/api/players/alpha?season=all")
    assert named.get_json()['stats'] == response.get_json()['stats']


def test_tie_rank_and_qualification_share_competition_ranking(application, client):
    from scoring import get_team_rankings
    ids = application.test_ids
    with application.app_context():
        season = a.Tournament.get_active()
        captain = a.User(username='gamma', nickname='gamma', role='captain')
        captain.set_password('test-password')
        db.session.add(captain)
        db.session.flush()
        team = a.Team(team_name='gamma', captain_id=captain.id, tournament_id=season.id, team_score=90)
        db.session.add(team)
        db.session.flush()
        db.session.get(a.Team, ids['teams'][0]).team_score = 100
        db.session.get(a.Team, ids['teams'][1]).team_score = 100
        setting = a.Setting.get_instance()
        setting.advance_count = 1
        db.session.commit()
        rankings = get_team_rankings(season.id)
        assert [row['rank'] for row in rankings] == [1, 1, 3]
    with application.app_context():
        assert len(a.get_advanced_teams()) == 2
