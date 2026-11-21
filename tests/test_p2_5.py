from sqlalchemy import event, inspect

import app as a
from conftest import login
from test_p0 import settle, submit


def test_match_and_team_workflows_do_not_query_legacy_notifications(application, client):
    ids = application.test_ids
    match_path = f"/tournament/match/{ids['match']}"
    with application.app_context():
        engine = a.db.engine
        assert not inspect(engine).has_table('notifications')
        a.db.session.get(a.Match, ids['match']).status = 'pending'
        applicant = a.User(username='p25-applicant', nickname='申请人', role='player', password_hash='fixture')
        a.db.session.add(applicant)
        a.db.session.flush()
        applicant_id = applicant.id
        a.db.session.add(a.TimePreference(player_id=applicant_id, day_of_week=0,
                                          start_time='10:00', end_time='12:00'))
        a.db.session.commit()

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert client.get('/').status_code == 200
        login(client, ids['users'][0])
        assert client.post(match_path + '/schedule', data={'scheduled_time': '2026-12-01T13:45'}).status_code == 302
        assert client.post(match_path + '/note', data={'match_note': '确认比赛时段'}).status_code == 302
        login(client, ids['users'][1])
        confirmation = client.post(match_path + '/confirm-schedule', follow_redirects=True)
        assert confirmation.status_code == 200
        assert '双方达成一致' in confirmation.get_data(as_text=True)
        assert '确认比赛时段' in confirmation.get_data(as_text=True)
        assert client.post(match_path + '/forfeit').status_code == 302
        assert submit(application, client, 0, 100).status_code == 302
        assert settle(application, client).status_code == 302

        login(client, applicant_id)
        assert client.post(f"/player/join-request/{ids['teams'][0]}").status_code == 302
        with application.app_context():
            request_id = a.JoinRequest.query.filter_by(user_id=applicant_id).one().id
        login(client, ids['users'][0])
        rejection = client.post(f'/captain/join-request/{request_id}/reject', follow_redirects=True)
        assert rejection.status_code == 200 and '已拒绝' in rejection.get_data(as_text=True)
        with application.app_context():
            assert a.db.session.get(a.JoinRequest, request_id).status == 'rejected'

        login(client, ids['super'])
        deletion = client.post(f'/admin/user/{applicant_id}/anonymize', data={'reference': '已核验申请'}, follow_redirects=True)
        assert deletion.status_code == 200 and '账号已匿名化' in deletion.get_data(as_text=True)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)

    assert statements and all('notifications' not in statement for statement in statements)
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        assert match.settlement_state == 'settled' and match.winner_id == ids['users'][0]
        assert match.confirmed_a and match.confirmed_b and '确认比赛时段' in match.match_note
        assert a.db.session.get(a.User, applicant_id).anonymized_at is not None
        assert a.db.session.get(a.JoinRequest, request_id).status == 'rejected'
        assert not a.TimePreference.query.filter_by(player_id=applicant_id).count()
