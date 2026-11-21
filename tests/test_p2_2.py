import pytest

import app as a
from conftest import login
from test_p0 import transition_data


PLAYER_LISTS = ('/player/invitations', '/player/my-join-requests')
CAPTAIN_LISTS = ('/captain/invitations', '/captain/join-requests')


@pytest.fixture()
def records(application):
    ids = application.test_ids
    with application.app_context():
        player = a.User(username='p22-player', nickname='申请选手甲', role='player', password_hash='fixture')
        other = a.User(username='p22-other', nickname='申请选手乙', role='player', password_hash='fixture')
        past = a.Tournament(name='已归档赛事', season_number=1, is_active=False)
        a.db.session.add_all([player, other, past])
        a.db.session.flush()
        current = a.db.session.get(a.Team, ids['teams'][0])
        current.team_name = '本届甲队'
        archived = a.Team(team_name='往届甲队', captain_id=ids['users'][0], tournament_id=past.id)
        archived_pending = a.Team(team_name='往届待处理队', captain_id=ids['users'][1], tournament_id=past.id)
        unknown = a.Team(team_name='归属不明甲队', captain_id=ids['users'][0])
        a.db.session.add_all([archived, archived_pending, unknown])
        a.db.session.flush()

        cases = dict(current=(current, player, 'pending'), history=(archived, player, 'rejected'),
                     past_pending=(archived_pending, player, 'pending'), unassigned=(unknown, player, 'pending'),
                     other=(current, other, 'accepted'))
        pairs = {}
        for key, (team, actor, status) in cases.items():
            invite = a.Invitation(team_id=team.id, user_id=actor.id, status=status)
            request = a.JoinRequest(team_id=team.id, user_id=actor.id, status=status)
            a.db.session.add_all([invite, request])
            a.db.session.flush()
            pairs[key] = (invite.id, request.id, team.id)
        a.db.session.commit()
        return dict(pairs=pairs, player=player.id, other=other.id, captain=ids['users'][0])


def check_read_only(response, name):
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert name in html and '只读' in html
    for action in ('/accept', '/reject', '/cancel'):
        assert action + '"' not in html


def test_owned_list_scopes_and_current_dashboard(application, client, records):
    invite_id, request_id, _ = records['pairs']['current']
    for user, paths in ((records['player'], PLAYER_LISTS), (records['captain'], CAPTAIN_LISTS)):
        login(client, user)
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert '本届甲队' in html
            assert '往届甲队' not in html and '归属不明甲队' not in html and '往届待处理队' not in html
            check_read_only(client.get(path + '?season=history'), '第 1 届 · 已归档赛事')
            check_read_only(client.get(path + '?season=unassigned'), '归属不明甲队')
            if user == records['player']:
                assert '已接受' not in html and '已通过' not in html
            assert client.get(path + '?season=all').status_code == 400
        dashboard_path = '/player/dashboard' if user == records['player'] else '/captain/dashboard'
        html = client.get(dashboard_path).get_data(as_text=True)
        assert '归属不明甲队' not in html and '往届待处理队' not in html
        assert all(f'href="{path}"' in html for path in paths)

    assert f'/captain/invitation/{invite_id}/cancel' in client.get(CAPTAIN_LISTS[0]).get_data(as_text=True)
    assert f'/captain/join-request/{request_id}/accept' in client.get(CAPTAIN_LISTS[1]).get_data(as_text=True)
    login(client, records['player'])
    assert f'/player/invitation/{invite_id}/accept' in client.get(PLAYER_LISTS[0]).get_data(as_text=True)
    assert '往届待处理队' not in client.get(PLAYER_LISTS[0] + '?season=history').get_data(as_text=True)


def test_uncertain_archived_and_other_owners_cannot_write(application, client, records):
    pairs = records['pairs']
    login(client, records['player'])
    for key in ('history', 'past_pending', 'unassigned', 'other'):
        invite_id, _, team_id = pairs[key]
        for action in ('accept', 'reject'):
            assert client.post(f'/player/invitation/{invite_id}/{action}').status_code == 403
        if key != 'other':
            assert client.post(f'/player/join-request/{team_id}').status_code == 403
    login(client, records['captain'])
    for key in ('history', 'past_pending', 'unassigned'):
        invite_id, request_id, _ = pairs[key]
        assert client.post(f'/captain/invitation/{invite_id}/cancel').status_code == 403
        for action in ('accept', 'reject'):
            assert client.post(f'/captain/join-request/{request_id}/{action}').status_code == 403
    other_captain = application.test_ids['users'][1]
    login(client, other_captain)
    invite_id, request_id, _ = pairs['current']
    assert client.post(f'/captain/invitation/{invite_id}/cancel').status_code == 403
    assert client.post(f'/captain/join-request/{request_id}/accept').status_code == 403
    with application.app_context():
        assert a.Invitation.query.count() == a.JoinRequest.query.count() == 5
        assert a.Invitation.query.filter_by(status='pending').count() == 3
        assert a.JoinRequest.query.filter_by(status='pending').count() == 3
    login(client, records['captain'])
    assert client.post(f'/captain/join-request/{request_id}/accept').status_code == 302
    with application.app_context():
        assert a.db.session.get(a.JoinRequest, request_id).status == 'accepted'
        assert a.TeamMember.query.filter_by(team_id=pairs['current'][2], user_id=records['player']).one()


def test_no_active_season_has_empty_current_reads(application, client, records):
    with application.app_context():
        a.Tournament.get_active().is_active = False
        a.db.session.commit()
    for user, paths in ((records['player'], PLAYER_LISTS), (records['captain'], CAPTAIN_LISTS)):
        login(client, user)
        for path in paths:
            html = client.get(path).get_data(as_text=True)
            assert '当前没有活跃赛季' in html and '本届暂无记录' in html
            assert '本届甲队' not in html and '往届甲队' not in html and '归属不明甲队' not in html
            check_read_only(client.get(path + '?season=history'), '往届甲队')
            check_read_only(client.get(path + '?season=unassigned'), '归属不明甲队')
    login(client, records['player'])
    search = client.post('/player/join-request/search', data={'search_keyword': '甲队'})
    assert search.status_code == 200 and '本届甲队' not in search.get_data(as_text=True)
    assert '/player/invitation/' not in client.get('/player/dashboard').get_data(as_text=True)
    invite_id, _, _ = records['pairs']['current']
    assert client.post(f'/player/invitation/{invite_id}/accept').status_code == 409


def test_switch_preserves_processed_history_for_both_roles(application, client, records):
    pairs = records['pairs']
    player = records['player']
    login(client, player)
    assert client.post(f"/player/invitation/{pairs['current'][0]}/reject").status_code == 302
    transition = transition_data(application, client, force=True)
    assert client.post('/admin/new-season', data=transition).status_code == 302
    with application.app_context():
        assert a.db.session.get(a.Invitation, pairs['current'][0]).status == 'rejected'
        assert a.db.session.get(a.JoinRequest, pairs['current'][1]) is None
    for user, paths in ((player, PLAYER_LISTS), (records['captain'], CAPTAIN_LISTS)):
        login(client, user)
        for path in paths:
            assert '本届暂无记录' in client.get(path).get_data(as_text=True)
        check_read_only(client.get(paths[0] + '?season=history'), '本届甲队')
