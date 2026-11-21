import re
from time import perf_counter

from sqlalchemy import event

import app as a
from conftest import login
from scoring import get_team_rankings


def archive_fixture(application, *, season_count=10, teams_per_season=20):
    with application.app_context():
        captains = [a.User(username=f'p24-{i}', nickname=f'P24 队长{i}', role='captain', password_hash='fixture')
                    for i in range(teams_per_season)]
        a.db.session.add_all(captains)
        a.db.session.flush()
        archives = []
        for number in range(100 + season_count - 1, 99, -1):
            season = a.Tournament(name=f'归档样本{number}', season_number=number, is_active=False,
                                  status='finished', bonus_score=500)
            a.db.session.add(season)
            a.db.session.flush()
            archives.append(season.id)
            teams = [
                a.Team(tournament_id=season.id, captain_id=captain.id, team_name=f'第{number}届队伍{i:02}',
                       team_score=(45, a.db.null(), 0)[i] if i < 3 else 10)
                for i, captain in enumerate(captains)
            ]
            a.db.session.add_all(teams)
            a.db.session.flush()
            for team, value in zip(teams, (1, 45, 1000)):
                a.db.session.add(a.TeamMember(team_id=team.id, user_id=team.captain_id, final_score=value))
            if teams:
                fields = dict(tournament_id=season.id, team_a_id=teams[0].id, team_b_id=teams[1].id,
                              player_a_id=captains[0].id, player_b_id=captains[1].id, roguelike='water')
                settled = a.Match(**fields, status='finished', settlement_state='settled',
                                  player_a_score=0, player_b_score=1, winner_id=captains[1].id)
                a.db.session.add_all([
                    settled,
                    a.Match(**fields, status='forfeit_a', forfeit_side='a', player_a_score=0,
                            winner_id=captains[1].id),
                    a.Match(**fields, status='confirmed'),
                    a.Match(**fields, match_type='exhibition', status='finished', winner_id=captains[1].id),
                ])
                a.db.session.flush()
                a.db.session.add(a.Match(**fields, parent_id=settled.id, status='finished', winner_id=captains[1].id))
        a.db.session.commit()
        return archives


def measure_history(application, client):
    with application.app_context():
        engine = a.db.engine
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        start = perf_counter()
        response = client.get('/history')
        elapsed_ms = (perf_counter() - start) * 1000
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    return response, len(statements), elapsed_ms


def test_archive_batch_matches_detail_and_meets_query_budget(application, client):
    archives = archive_fixture(application)
    response, anonymous_queries, elapsed_ms = measure_history(application, client)
    assert response.status_code == 200
    assert anonymous_queries <= 5
    with application.app_context():
        if a.db.engine.dialect.name == 'sqlite':
            assert elapsed_ms < 500
    print(f'archive 10×20, anonymous: {anonymous_queries} SQL statements, {elapsed_ms:.1f} ms')

    page = response.get_data(as_text=True)
    assert re.findall(r'<span class="badge bg-secondary">第 (\d+) 届</span>', page) == [
        str(number) for number in range(109, 99, -1)
    ]
    for label, value in (('参赛队伍', 20), ('正式场次', 3), ('已判胜负', 2), ('已结算', 1)):
        assert len(re.findall(rf'<h4 class="mb-0">{value}</h4>\s*<small class="text-muted">{label}</small>', page)) == 10
    assert '第109届队伍00、第109届队伍01' in page
    assert '第109届队伍02' not in page  # A saved zero takes precedence over the member sum.
    detail = client.get(f'/history/{archives[0]}').get_data(as_text=True)
    assert '正式场次 3 · 已判胜负 2 · 已结算 1' in detail and '归档冻结，未结算' in detail
    with application.app_context():
        ranking = get_team_rankings(archives[0])
        assert [(row['rank'], row['points']) for row in ranking[:3]] == [(1, 45), (1, 45), (3, 10)]
        assert [row['team'].team_name for row in ranking if row['rank'] == 1] == [
            '第109届队伍00', '第109届队伍01'
        ]

    login(client, application.test_ids['users'][0])
    response, player_queries, _ = measure_history(application, client)
    assert response.status_code == 200 and player_queries <= 5
    print(f'archive 10×20, signed-in player: {player_queries} SQL statements')


def test_archive_pagination_is_stable_and_handles_empty_seasons(application, client):
    empty = client.get('/history')
    assert empty.status_code == 200 and '暂无往届赛事记录' in empty.get_data(as_text=True)
    archive_fixture(application, season_count=21, teams_per_season=0)

    first = client.get('/history').get_data(as_text=True)
    second = client.get('/history?page=2').get_data(as_text=True)
    assert 'href="/history?page=2"' in first
    assert 'href="/history?page=1"' in second
    assert re.findall(r'<span class="badge bg-secondary">第 (\d+) 届</span>', first) == [
        str(number) for number in range(120, 100, -1)
    ]
    assert re.findall(r'<span class="badge bg-secondary">第 (\d+) 届</span>', second) == ['100']
    assert '该届暂无积分数据' in second
    assert second.count('<h4 class="mb-0">0</h4>') == 4
    assert client.get('/history?page=3').status_code == 404
