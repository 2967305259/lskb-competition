from io import BytesIO

from openpyxl import load_workbook

import app as a
from conftest import login
from test_p0 import transition_data


def workbook(client):
    response = client.get('/admin/export')
    assert response.status_code == 200
    return load_workbook(BytesIO(response.data))


def test_advancement_uses_actual_count_and_archived_quota(application, client):
    ids = application.test_ids
    with application.app_context():
        a.Setting.get_instance().advance_count = 4
        for team_id, points in zip(ids['teams'], (100, 90)):
            a.db.session.get(a.Team, team_id).team_score = points
        for i, points in enumerate((80, 70, 70, 70), start=3):
            user = a.User(username=f'tie{i}', nickname=f'tie{i}', password_hash='fixture', role='captain')
            a.db.session.add(user)
            a.db.session.flush()
            a.db.session.add(a.Team(team_name=f'晋级样例{i}', captain_id=user.id,
                                    tournament_id=ids['season'], team_score=points))
        a.db.session.commit()

    login(client, ids['admin'])
    for path in ('/admin/advance', '/tournament/'):
        html = client.get(path).get_data(as_text=True)
        assert '配置晋级名额 4 · 实际晋级 6 队' in html
        assert all(f'晋级样例{i}' in html for i in range(3, 7))
    admin = client.get('/admin/advance').get_data(as_text=True)
    assert admin.count('已晋级</span>') == 6
    assert '未晋级' not in admin
    standings = workbook(client)['队伍积分榜']
    assert [row[0] for row in standings.iter_rows(min_row=2, max_col=5, values_only=True)] == [1, 2, 3, 4, 4, 4]
    assert [standings[f'E{row}'].value for row in range(2, 8)] == ['是'] * 6
    assert (standings['H1'].value, standings['H2'].value) == (4, 6)

    assert client.post('/admin/new-season', data=transition_data(application, client, force=True)).status_code == 302
    assert client.post('/admin/score-config', data={'advance_count': '1', 'bonus_score': '500'}).status_code == 302
    archive = client.get(f"/history/{ids['season']}").get_data(as_text=True)
    assert '配置晋级名额 4 · 实际晋级 6 队' in archive
    assert archive.count('已晋级</td>') == 6


def test_manual_segments_forfeit_and_review_keep_independent_match_counts(application, client):
    ids = application.test_ids
    login(client, ids['admin'])
    assert client.post('/admin/schedule/manual-match', data={
        'player_a_id': ids['users'][0], 'player_b_id': ids['users'][1],
        'roguelike': 'water', 'match_date': '2026-12-01',
    }).status_code == 302
    with application.app_context():
        whole = a.Match.query.filter(a.Match.parent_id.is_(None), a.Match.id != ids['match']).one()
        whole_id = whole.id
        segments = list(whole.children)
        assert len(segments) == 2
        segment_id = segments[-1].id
    assert client.post('/admin/schedule/confirm-match', data={'match_id': whole_id}).status_code == 302

    login(client, ids['users'][0])
    assert client.post(f'/tournament/match/{segment_id}/forfeit', data={'reason': '验收弃权'}).status_code == 302
    stats = client.get(f"/player/api/players/{ids['users'][0]}").get_json()['stats']
    assert (stats['formal_matches'], stats['decided_matches'], stats['settled_matches'], stats['losses']) == (2, 1, 0, 1)
    for path in ('/player/profile', '/captain/profile'):
        profile = client.get(path)
        assert profile.status_code == 200
        html = profile.get_data(as_text=True)
        assert html.count('<strong>负</strong>') == 1
        assert html.count('>待结算</span>') == 2
    login(client, ids['admin'])
    exported = workbook(client)
    assert exported['比赛结果'].max_row == 3
    assert [exported['队伍积分榜'][f'H{row}'].value for row in (3, 4, 5)] == [2, 1, 0]
    assert exported['比赛结果']['F3'].value == 0
    assert exported['比赛结果']['I3'].value is None
    assert '待结算' in client.get('/tournament/').get_data(as_text=True)

    login(client, ids['users'][1])
    assert client.post(f'/tournament/match/{whole_id}/submit-result',
                       data={'score': '100', 'difficulty': '12'}).status_code == 302
    login(client, ids['admin'])
    edit_path = f'/admin/match/{whole_id}/edit-result'
    assert client.post(edit_path, data={'action': 'win_b'}).status_code == 302
    assert client.post(edit_path, data={'action': 'win_b'}).status_code == 400
    assert client.post(f'/admin/match/{whole_id}/review', data={'action': 'reject', 'reason': '验收'}).status_code == 302
    stats = client.get(f"/player/api/players/{ids['users'][0]}").get_json()['stats']
    assert (stats['formal_matches'], stats['decided_matches'], stats['settled_matches'], stats['losses']) == (2, 1, 1, 1)
    assert [workbook(client)['队伍积分榜'][f'H{row}'].value for row in (3, 4, 5)] == [2, 1, 1]

    assert client.post('/admin/new-season', data=transition_data(application, client, force=True)).status_code == 302
    archive = client.get(f"/history/{ids['season']}").get_data(as_text=True)
    assert '正式场次 2 · 已判胜负 1 · 已结算 1' in archive
    assert '归档冻结，未结算' in archive


def test_adjustment_ranking_and_export_follow_season_multiplier(application, client):
    ids = application.test_ids
    result_path = f"/tournament/match/{ids['match']}/submit-result"
    for user, score in zip(ids['users'], (1000, 1)):
        login(client, user)
        assert client.post(result_path, data={'score': str(score), 'difficulty': '12'}).status_code == 302
    login(client, ids['admin'])
    assert client.post(f"/admin/match/{ids['match']}/edit-result", data={'action': 'win_a'}).status_code == 302
    team_id, member_id = ids['teams'][0], ids['members'][0]
    assert client.post(f'/admin/team/{team_id}/update-scores',
                       data={f'score_{member_id}': '1010', 'reason': '验收加分'}).status_code == 302
    for _ in range(2):
        assert client.post('/admin/reset-scores').status_code == 302
    data = workbook(client)
    assert data['队伍积分榜']['D2'].value == 505000
    assert [data['队员积分榜'][f'{col}2'].value for col in 'CDEF'] == [1000, 10, 1010, '一致']
    detail = client.get(f'/admin/team/{team_id}').get_data(as_text=True)
    assert '账本合计：<strong>1010</strong>' in detail
    assert '505000' in client.get('/tournament/rankings').get_data(as_text=True)

    with application.app_context():
        adjustment_id = a.ScoreAdjustment.query.one().id
    assert client.post(f'/admin/adjustments/{adjustment_id}/revoke', data={'reason': '验收撤销'}).status_code == 200
    assert workbook(client)['队伍积分榜']['D2'].value == 500000
    assert client.post('/admin/new-season', data=transition_data(application, client)).status_code == 302

    with application.app_context():
        new = a.Tournament.get_active()
        team = a.Team(team_name='新赛季队伍', captain_id=ids['users'][0], tournament_id=new.id)
        a.db.session.add(team)
        a.db.session.flush()
        member = a.TeamMember(team_id=team.id, user_id=ids['users'][0])
        a.db.session.add(member)
        a.db.session.commit()
        new_team, new_member = team.id, member.id
    assert client.post(f'/admin/team/{new_team}/update-scores',
                       data={f'score_{new_member}': '10', 'reason': '新届加分'}).status_code == 302
    assert workbook(client)['队伍积分榜']['D2'].value == 5000
    assert client.post('/admin/score-config', data={'advance_count': '0', 'bonus_score': '100'}).status_code == 302
    assert workbook(client)['队伍积分榜']['D2'].value == 1000
    assert '500000' in client.get(f"/history/{ids['season']}").get_data(as_text=True)
    with application.app_context():
        assert a.db.session.get(a.Team, team_id).team_score == 500000
        assert a.db.session.get(a.Tournament, ids['season']).bonus_score == 500
        assert a.Tournament.get_active().bonus_score == 100
