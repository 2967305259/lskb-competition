import json
from pathlib import Path

import app as a
from conftest import login
from test_p0 import complete, transition_data
from test_p1_acceptance import workbook


def prepare_records(application):
    ids = application.test_ids
    with application.app_context():
        player = a.db.session.get(a.User, ids['users'][0])
        player.username = 'private-login'
        player.avatar = 'public-portrait.png'
        member = a.db.session.get(a.TeamMember, ids['members'][0])
        member.declaration = 'PRIVATE_DECLARATION'
        other = a.User(username='unaffiliated', nickname='未参赛账号', role='player', password_hash='fixture',
                       avatar='private-portrait.png')
        a.db.session.add(other)
        a.db.session.commit()
        outsider = other.id
    root = Path(application.config['UPLOAD_ROOT'])
    for name in ('public-portrait.png', 'private-portrait.png', 'unreferenced.png'):
        (root / 'avatars' / name).write_bytes(name.encode())
    return outsider


def test_role_visibility_and_direct_media(application, client):
    ids = application.test_ids
    outsider = prepare_records(application)
    complete(application, client)
    root = Path(application.config['UPLOAD_ROOT'])
    evidence = next((root / 'screenshots').glob(f"screenshot_{ids['match']}_{ids['users'][0]}_*")).name
    evidence_url = f'/static/uploads/screenshots/{evidence}'
    portrait_url = '/static/uploads/avatars/public-portrait.png'
    role_api = f"/player/api/players/{ids['users'][0]}"
    anon = application.test_client()
    for path in ('/api/teams', f"/api/teams/{ids['teams'][0]}", f"/team/{ids['teams'][0]}"):
        data = anon.get(path).get_data(as_text=True)
        assert 'PRIVATE_DECLARATION' not in data and 'private-login' not in data
    assert anon.get(role_api).status_code == 302
    assert anon.get(portrait_url).status_code == 200
    assert anon.get('/static/uploads/avatars/private-portrait.png').status_code == 404
    assert anon.get('/static/uploads/avatars/unreferenced.png').status_code == 404
    assert anon.get(evidence_url).status_code == 404
    assert anon.get('/static/files/../uploads/screenshots/' + evidence).status_code == 404
    login(client, outsider)
    assert client.get(evidence_url).status_code == 404
    assert client.get('/static/uploads/avatars/private-portrait.png').status_code == 200
    assert 'username' not in client.get(role_api).get_json()['player']
    assert 'declaration' not in client.get(role_api).get_json()['player']['team']
    assert client.get('/player/api/players/private-login').status_code == 404
    assert client.get('/admin/export').status_code == 403
    login(client, ids['users'][0])
    assert client.get(role_api).get_json()['player']['username'] == 'private-login'
    response = client.get(evidence_url)
    assert response.status_code == 200 and response.cache_control.no_store
    assert evidence_url in client.get(f"/tournament/match/{ids['match']}").get_data(as_text=True)
    login(client, ids['admin'])
    assert client.get(evidence_url).status_code == 200
    assert client.get(role_api).get_json()['player']['team']['declaration'] == 'PRIVATE_DECLARATION'
    assert workbook(client)['报名信息']['B2'].value == 'alpha'
    with application.app_context():
        audit = a.SystemLog.query.filter_by(action='download_season_excel').one()
        assert audit.user_id == ids['admin'] and json.loads(audit.detail)['season_id'] == ids['season']


def test_personal_copy_and_anonymization_preserve_archive(application, client):
    ids = application.test_ids
    prepare_records(application)
    complete(application, client)
    assert client.post('/admin/new-season', data=transition_data(application, client)).status_code == 302
    uid = ids['users'][0]
    owner = application.test_client()
    login(owner, uid)
    exported = owner.post('/auth/privacy/export')
    record = exported.get_json()
    assert exported.status_code == 200 and exported.cache_control.no_store
    assert record['account']['username'] == 'private-login'
    assert record['memberships'][0]['declaration'] == 'PRIVATE_DECLARATION'
    assert record['matches'][0]['score'] == 100 and len(record['matches'][0]['evidence']) == 1
    assert 'password_hash' not in exported.get_data(as_text=True) and 'beta' not in exported.get_data(as_text=True)
    login(client, ids['admin'])
    assert client.post(f'/admin/user/{uid}/delete').status_code == 409
    assert client.post(f'/admin/user/{uid}/anonymize', data={'reference': 'req-001'}).status_code == 403
    login(client, ids['super'])
    assert client.post(f'/admin/user/{uid}/privacy/export', data={'reference': 'copy-001'}).status_code == 200
    assert client.post(f'/admin/user/{uid}/anonymize').status_code == 400
    assert client.post(f'/admin/user/{uid}/anonymize', data={'reference': 'req-001'}).status_code == 302
    assert owner.get('/auth/privacy').status_code == 302
    assert owner.get('/static/uploads/avatars/public-portrait.png').status_code == 404
    assert client.get(record['matches'][0]['evidence'][0]).status_code == 200
    with application.app_context():
        closed = a.db.session.get(a.User, uid)
        assert not closed.check_password('test-password') and closed.avatar is None and closed.anonymized_at
        assert closed.nickname.startswith('注销选手-')
        alias = closed.nickname
        old_match = a.db.session.get(a.Match, ids['match'])
        assert (old_match.player_a_id, old_match.winner_id, old_match.settlement_state) == (uid, uid, 'settled')
        assert a.ScoreLog.query.filter_by(match_id=old_match.id).count() > 0
        records = [json.loads(row.detail) for row in a.SystemLog.query.filter_by(action='download_personal_data')]
        assert {record['request_reference'] for record in records} == {None, 'copy-001'}
    assert alias in application.test_client().get(f"/history/{ids['season']}").get_data(as_text=True)
    assert (Path(application.config['UPLOAD_ROOT']) / 'avatars/public-portrait.png').is_file()


def test_admin_can_delete_account_without_event_or_audit_references(application, client):
    uid = prepare_records(application)
    login(client, application.test_ids['admin'])
    assert client.post(f'/admin/user/{uid}/delete').status_code == 302
    with application.app_context():
        assert a.db.session.get(a.User, uid) is None
        row = a.SystemLog.query.filter_by(action='delete_user').one()
        assert json.loads(row.detail) == {'subject_id': uid}
