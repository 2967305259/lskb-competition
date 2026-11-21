from sqlalchemy import text

import app as a
from conftest import login
from management import data_report
from test_p0 import complete, transition_data


def test_team_links_and_api_keep_current_and_archived_seasons_distinct(application, client):
    ids = application.test_ids
    team_id = ids['teams'][0]
    public = f'/team/{team_id}'
    private = f'/player/team/{team_id}'
    admin = f'/admin/team/{team_id}'
    api = f'/api/teams/{team_id}'
    match_id = ids['match']
    history = f"/history/{ids['season']}"
    public_page = client.get(public)
    assert public_page.status_code == 200
    assert '只读' not in public_page.get_data(as_text=True)
    item = client.get(api).get_json()['team']
    assert item['season_scope'] == 'current' and not item['archived'] and not item['read_only']
    old_number = item['season_number']
    login(client, ids['users'][0])
    assert client.get(private).status_code == 200
    login(client, ids['admin'])
    assert 'update-member' in client.get(admin).get_data(as_text=True)

    complete(application, client)
    assert client.post('/admin/new-season', data=transition_data(application, client)).status_code == 302
    response = client.get(public)
    assert response.status_code == 302 and response.location == history
    archived_api = client.get(api)
    assert archived_api.status_code == 200
    item = archived_api.get_json()['team']
    assert item['season_number'] == old_number and item['archived'] and item['read_only']
    assert item['season_scope'] == 'archived'

    page = client.get(history).get_data(as_text=True)
    assert f'href="#team-{team_id}"' in page and f'id="team-{team_id}"' in page
    assert f'href="{public}"' not in page
    assert all(f'href="/team/{tid}"' not in page for tid in ids['teams'])

    response = client.get(admin)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    notice = f'往届数据（第 {old_number} 届）· 只读'
    assert notice in html
    assert f'href="{history}#team-{team_id}"' in html
    assert '/update-member/' not in html and '/update-scores"' not in html and '/delete"' not in html
    assert '往届专项纠错' not in html
    assert notice in client.get(f'/admin/match/{match_id}/score-logs').get_data(as_text=True)
    assert client.post(admin + '/update-scores').status_code == 403

    login(client, ids['super'])
    assert '往届专项纠错' in client.get(admin).get_data(as_text=True)
    login(client, ids['users'][0])
    response = client.get(private)
    assert response.status_code == 302 and response.location == history
    unrelated = f"/api/teams/{ids['teams'][1]}"
    assert client.get(unrelated).get_json()['team']['season_number'] == old_number


def test_unassigned_team_shows_read_only_and_remains_in_reconciliation_report(application, client):
    ids = application.test_ids
    with application.app_context():
        team = a.Team(team_name='未归属队伍', captain_id=ids['users'][0])
        a.db.session.add(team)
        a.db.session.flush()
        member = a.TeamMember(team_id=team.id, user_id=ids['users'][0], roguelike='water')
        a.db.session.add(member)
        a.db.session.commit()
        team_id = team.id
        assert team_id in {row['id'] for row in data_report()['orphan_teams']}

    response = client.get(f'/team/{team_id}')
    html = response.get_data(as_text=True)
    assert response.status_code == 200 and '赛季归属待核对 · 只读' in html
    assert 'water（历史名称待核对）' in html
    assert '1/4 人' not in html
    api = client.get(f'/api/teams/{team_id}')
    assert api.status_code == 200
    record = api.get_json()['team']
    assert record['season_scope'] == 'orphan' and not record['archived'] and record['read_only']
    assert record['season_number'] is None

    login(client, ids['users'][0])
    response = client.get(f'/player/team/{team_id}')
    assert response.status_code == 302 and response.location == f'/team/{team_id}'
    login(client, ids['super'])
    admin = f'/admin/team/{team_id}'
    page = client.get(admin).get_data(as_text=True)
    assert '赛季归属待核对 · 只读' in page
    assert '往届专项纠错' not in page and '/update-member/' not in page
    assert client.post(admin + '/delete').status_code == 403
    with application.app_context():
        assert a.db.session.get(a.Team, team_id).tournament_id is None


def test_archived_forfeit_hides_player_links_while_reusing_frozen_rule_context(application, client):
    ids = application.test_ids
    path = f"/tournament/match/{ids['match']}"
    login(client, ids['users'][0])
    assert client.post(path + '/forfeit').status_code == 302
    login(client, ids['users'][1])
    before = client.get(path).get_data(as_text=True)
    assert before.count(f'href="{path}/submit-result"') == 1

    assert client.post('/admin/new-season', data=transition_data(application, client, force=True)).status_code == 302
    with application.app_context():
        old = a.db.session.get(a.Tournament, ids['season'])
        old_number = old.season_number
        a.Roguelike.query.filter_by(key='water').one().name = '当届改名赛道'
        a.db.session.commit()
    login(client, ids['users'][1])
    response = client.get(path)
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and f'往届数据（第 {old_number} 届）· 只读' in page
    assert '归档冻结，未结算' in page
    assert '水月与深蓝之树' in page and '当届改名赛道' not in page
    assert f'href="{path}/submit-result"' not in page and f'{path}/forfeit' not in page
    assert client.get(path + '/submit-result').status_code == 403
    assert client.post(path + '/submit-result', data={'score': 1, 'difficulty': 12}).status_code == 403
    login(client, ids['admin'])
    assert f'往届数据（第 {old_number} 届）· 只读' in client.get(path).get_data(as_text=True)


def test_match_with_conflicting_team_ownership_disables_current_actions(application, client):
    ids = application.test_ids
    with application.app_context():
        team = a.Team(team_name='待核对队伍', captain_id=ids['users'][0])
        a.db.session.add(team)
        a.db.session.commit()
        team_id = team.id
        with a.db.engine.begin() as conn:
            conn.execute(text('UPDATE matches SET team_a_id=:team WHERE id=:match'),
                         {'team': team_id, 'match': ids['match']})

    login(client, ids['users'][0])
    path = f"/tournament/match/{ids['match']}"
    response = client.get(path)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert '赛季归属待核对 · 只读' in page and f'{path}/submit-result' not in page
    assert client.get(path + '/submit-result').status_code == 409
    assert client.post(path + '/submit-result', data={'score': 99, 'difficulty': 12}).status_code == 409
    with application.app_context():
        assert a.db.session.get(a.Match, ids['match']).player_a_score is None
