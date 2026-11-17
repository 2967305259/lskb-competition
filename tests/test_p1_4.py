from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

import app as a
from backup import verify_backup
from conftest import login
from migrations import assert_schema_current, migrate_database, SCHEMA_VERSION
from season_phase import NO_TRACKS, parse_event_time, window_state
from test_p0 import settle, submit, transition_data


def test_manual_switch_controls_team_actions_even_after_planned_deadline(application, client):
    ids = application.test_ids
    with application.app_context():
        season = a.Tournament.get_active()
        season.registration_start = datetime(2020, 1, 1)
        season.registration_end = datetime(2020, 1, 2)
        season.window_times_utc = True
        season.status = 'running'
        spare = a.User(username='spare', nickname='spare', password_hash='fixture', role='player')
        recruit = a.User(username='recruit', nickname='recruit', password_hash='fixture', role='captain')
        a.db.session.add_all([spare, recruit])
        a.db.session.flush()
        invitation = a.Invitation(team_id=ids['teams'][0], user_id=spare.id)
        application_request = a.JoinRequest(team_id=ids['teams'][0], user_id=spare.id)
        a.db.session.add_all([invitation, application_request])
        a.db.session.commit()
        invite_id, request_id, spare_id, recruit_id = invitation.id, application_request.id, spare.id, recruit.id

    login(client, ids['admin'])
    assert '报名截止时间已过但报名仍开放' in client.get('/admin/dashboard').get_data(as_text=True)
    assert client.post('/admin/registration', data={'registration_open': 'closed'}).status_code == 302
    actions = (
        (recruit_id, '/captain/team/create', {}),
        (spare_id, f"/player/join-request/{ids['teams'][0]}", {}),
        (spare_id, f'/player/invitation/{invite_id}/accept', {}),
        (spare_id, f'/player/invitation/{invite_id}/reject', {}),
        (ids['users'][0], f'/captain/invite/{spare_id}', {}),
        (ids['users'][0], f'/captain/invitation/{invite_id}/cancel', {}),
        (ids['users'][0], f'/captain/join-request/{request_id}/accept', {}),
        (ids['users'][0], f'/captain/join-request/{request_id}/reject', {}),
        (ids['users'][0], '/player/team/roguelike', {'roguelike': 'water'}),
        (ids['users'][0], '/captain/team/assign-position', {'member_id': ids['members'][0], 'position': 'cold'}),
    )
    for user, path, data in actions:
        login(client, user)
        response = client.post(path, data=data)
        assert response.status_code == 403, path
        assert '报名已由管理员关闭' in response.get_data(as_text=True)
    login(client, spare_id)
    assert f'/player/invitation/{invite_id}/accept' not in client.get('/player/invitations').get_data(as_text=True)

    login(client, ids['admin'])
    assert client.post('/admin/registration', data={'registration_open': 'open'}).status_code == 302
    login(client, ids['users'][0])
    _, path, data = actions[-1]
    assert client.post(path, data=data).status_code == 302
    assert client.post('/player/team/roguelike', data={'roguelike': 'water', 'squad': '新分队'}).status_code == 302
    login(client, spare_id)
    assert client.post(f'/player/invitation/{invite_id}/accept').status_code == 302
    with application.app_context():
        assert a.db.session.get(a.TeamMember, ids['members'][0]).position == 'cold'
        assert a.db.session.get(a.TeamMember, ids['members'][0]).squad == '新分队'
        assert a.db.session.get(a.Invitation, invite_id).status == 'accepted'


def test_disabled_tracks_block_opening_and_start_but_allow_blank_new_season(application, client):
    ids = application.test_ids
    with application.app_context():
        a.Roguelike.query.update({'is_active': False})
        a.Setting.get_instance().registration_open = False
        a.db.session.commit()
    login(client, ids['admin'])
    assert '开放报名（需要启用赛道）' in client.get('/admin/registration').get_data(as_text=True)
    for path, data in (('/admin/registration', {'registration_open': 'open'}), ('/admin/start-tournament', {})):
        response = client.post(path, data=data)
        assert response.status_code == 409
        assert NO_TRACKS in response.get_data(as_text=True)
    response = client.post('/admin/new-season', data=transition_data(application, client, force=True), follow_redirects=True)
    assert response.status_code == 200
    assert '本届报名开关保持关闭' in response.get_data(as_text=True)
    with application.app_context():
        assert a.Tournament.get_active().id != ids['season']
        assert not a.Setting.get_instance().registration_open


def test_finished_season_allows_existing_results_but_not_scheduling(application, client):
    ids = application.test_ids
    match_path = f"/tournament/match/{ids['match']}"
    with application.app_context():
        season = a.Tournament.get_active()
        season.status = 'running'
        season.tournament_end = datetime(2030, 6, 1, 12)
        a.db.session.commit()
    login(client, ids['admin'])
    assert b'/admin/finish-tournament' in client.get('/admin/dashboard').data
    assert client.post('/admin/finish-tournament').status_code == 302
    assert client.post('/admin/schedule/update-time', data={'match_id': ids['match'], 'duration_minutes': 50}).status_code == 409
    schedule_page = client.get('/admin/schedule')
    assert schedule_page.status_code == 200 and b'/admin/schedule/confirm-match' not in schedule_page.data
    login(client, ids['users'][0])
    assert client.get(match_path + '/schedule').status_code == 409
    assert client.post(match_path + '/forfeit').status_code == 302
    login(client, ids['users'][1])
    assert '补交成绩' in client.get(match_path).get_data(as_text=True)
    assert submit(application, client, 1, 0).status_code == 302
    assert settle(application, client).status_code == 302
    assert client.post(f"/admin/match/{ids['match']}/review", data={'action': 'reject'}).status_code == 302
    with application.app_context():
        season = a.Tournament.get_active()
        assert season.tournament_end == datetime(2030, 6, 1, 12)
        assert season.status == 'finished'
        assert a.db.session.get(a.Match, ids['match']).review_status == 'rejected'


def test_window_settings_preserve_legacy_until_confirmed_and_round_trip_local_time(application, client):
    path = '/admin/tournament-settings'
    login(client, application.test_ids['admin'])
    with application.app_context():
        season = a.Tournament.get_active()
        season.registration_start = datetime(2025, 6, 1, 23, 30)
        season.registration_end = datetime(2025, 6, 2, 0, 30)
        a.db.session.commit()
    assert '旧日期时区待核对' in client.get(path).get_data(as_text=True)
    assert client.post(path, data={'tournament_name': '更新赛事名称'}).status_code == 302
    with application.app_context():
        season = a.Tournament.get_active()
        assert season.registration_start == datetime(2025, 6, 1, 23, 30)
        assert not season.window_times_utc
    dates = {'registration_start': '2025-06-01T23:30', 'registration_end': '2025-06-02T00:30',
             'tournament_start': '', 'tournament_end': '', 'confirm_legacy_windows': '1'}
    assert client.post(path, data=dates).status_code == 302
    for invalid_end in ('2025-06-01T23:30', '2025-06-01T23:00'):
        invalid = client.post(path, data=dict(dates, registration_end=invalid_end))
        assert '结束时间须晚于开始时间' in invalid.get_data(as_text=True)
    with application.app_context():
        season = a.Tournament.get_active()
        assert season.window_times_utc
        assert season.registration_start == datetime(2025, 6, 1, 15, 30)
        assert season.registration_end == datetime(2025, 6, 1, 16, 30)
        assert season.tournament_start is None
    html = client.get(path).get_data(as_text=True)
    assert 'value="2025-06-02T00:30"' in html and 'Asia/Shanghai' in html
    assert window_state(datetime(2025, 6, 1, 15), datetime(2025, 6, 1, 16), True,
                        now=datetime(2025, 6, 1, 16)) == 'expired'

    response = client.post('/admin/new-season', data=dict(transition_data(application, client, force=True),
                           inherit_registration_window='1'), follow_redirects=True)
    assert '报名日期已过期' in response.get_data(as_text=True)
    with application.app_context():
        season = a.Tournament.get_active()
        assert season.window_times_utc and season.registration_end == datetime(2025, 6, 1, 16, 30)
        assert season.tournament_start is None


def test_match_schedule_uses_event_clock_and_reconfirmation(application, client):
    ids = application.test_ids
    match_id = ids['match']
    path = f'/tournament/match/{match_id}'
    with application.app_context():
        a.db.session.get(a.Match, match_id).status = 'pending'
        a.db.session.commit()
    login(client, ids['users'][0])
    assert client.post(path + '/schedule', data={'scheduled_time': '2026-12-01T23:45'}).status_code == 302
    login(client, ids['users'][1])
    assert client.post(path + '/schedule', data={'scheduled_time': '2026-12-02T00:15'}).status_code == 302
    with application.app_context():
        match = a.db.session.get(a.Match, match_id)
        assert match.scheduled_time_utc and match.scheduled_time == datetime(2026, 12, 1, 16, 15)
        assert not match.confirmed_a and match.confirmed_b and match.status == 'scheduled'
    login(client, ids['admin'])
    schedule = client.get('/admin/schedule/api/matches').get_json()
    assert schedule['timezone'] == 'Asia/Shanghai'
    assert schedule['matches'][0]['scheduled_time'] == '2026-12-02T00:15:00'
    assert client.post('/admin/schedule/update-time', data={'match_id': match_id, 'duration_minutes': 40}).status_code == 200
    with application.app_context():
        assert a.db.session.get(a.Match, match_id).scheduled_time == datetime(2026, 12, 1, 16, 15)


def test_event_time_rejects_ambiguous_or_nonexistent_wall_times(application):
    application.config['EVENT_TIMEZONE'] = 'America/New_York'
    with application.app_context():
        assert parse_event_time('2026-03-08T03:10') == datetime(2026, 3, 8, 7, 10)
        for time in ('2026-03-08T02:10', '2026-11-01T01:10'):
            with pytest.raises(ValueError, match='不存在或有歧义'):
                parse_event_time(time)


def test_version_one_upgrade_keeps_unverified_clock_values(application):
    with application.app_context():
        season = a.Tournament.get_active()
        season.registration_start = datetime(2020, 6, 3, 17)
        a.db.session.get(a.Match, application.test_ids['match']).scheduled_time = datetime(2020, 6, 3, 19)
        a.db.session.commit()
        a.db.session.remove()
        with a.db.engine.begin() as conn:
            conn.execute(text('UPDATE schema_version SET version=1'))
            conn.execute(text('ALTER TABLE tournaments DROP COLUMN window_times_utc'))
            conn.execute(text('ALTER TABLE matches DROP COLUMN scheduled_time_utc'))

        batch = migrate_database(a.db.engine, application.config)
        assert Path(batch).is_dir() and verify_backup(batch)['schema_version'] == 1
        assert_schema_current(a.db.engine)
        assert migrate_database(a.db.engine, application.config) is None
        season = a.Tournament.get_active()
        match = a.db.session.get(a.Match, application.test_ids['match'])
        assert season.registration_start == datetime(2020, 6, 3, 17) and not season.window_times_utc
        assert match.scheduled_time == datetime(2020, 6, 3, 19) and not match.scheduled_time_utc
        assert a.db.session.execute(text('SELECT MAX(version) FROM schema_version')).scalar() == SCHEMA_VERSION


def test_start_and_matching_use_enabled_tracks_without_changing_manual_switch(application, client):
    ids = application.test_ids
    with application.app_context():
        a.Roguelike.query.filter_by(key='water').one().is_active = False
        if a.Roguelike.query.filter_by(is_active=True).first() is None:
            a.db.session.add(a.Roguelike(key='p14-alt', name='替代赛道', is_active=True))
        setting = a.Setting.get_instance()
        setting.allow_incomplete_teams = True
        setting.registration_open = False
        a.db.session.commit()
    login(client, ids['admin'])
    start = client.post('/admin/start-tournament', follow_redirects=True)
    assert '须选择已启用的赛道' in start.get_data(as_text=True)
    with application.app_context():
        season = a.Tournament.get_active()
        assert season.status == 'registration'
        season.status = 'matching'
        a.db.session.commit()
    assert client.post('/admin/run-matching').status_code == 302
    with application.app_context():
        season = a.Tournament.get_active()
        assert season.status == 'matching'
        assert a.Match.query.count() == 1
        active_key = a.Roguelike.query.filter_by(is_active=True).first().key
        a.TeamMember.query.update({'roguelike': active_key})
        season.status = 'registration'
        a.db.session.commit()
    assert client.post('/admin/start-tournament').status_code == 302
    with application.app_context():
        assert a.Tournament.get_active().status == 'matching'
        assert not a.Setting.get_instance().registration_open
    assert client.post('/admin/run-matching').status_code == 302
    with application.app_context():
        assert a.Tournament.get_active().status == 'running'
        assert a.Match.query.filter_by(roguelike=active_key).count() == 1
        assert not a.Setting.get_instance().registration_open
