from datetime import datetime

import app as a
from conftest import login
from scoring import get_player_stats
from test_p0 import complete, transition_data


def player_data(application, client, scope=None):
    player_id = application.test_ids['users'][0]
    suffix = f'?season={scope}' if scope else ''
    urls = (f'/player/api/players/{player_id}{suffix}', f'/player/api/players/alpha{suffix}')
    first, second = (client.get(url) for url in urls)
    assert first.status_code == second.status_code == 200
    assert first.get_json() == second.get_json()
    return first.get_json()


def test_full_record_exceeds_recent_limits_and_paginates_formal_matches(application, client):
    ids = application.test_ids
    complete(application, client)
    with application.app_context():
        root = a.db.session.get(a.Match, ids['match'])
        # A login name can match somebody else's nickname.
        root.player_a.username = 'alpha-account'
        a.db.session.flush()
        root.player_b.username = 'alpha'
        common = dict(tournament_id=root.tournament_id, player_a_id=root.player_a_id,
                      player_b_id=root.player_b_id, team_a_id=root.team_a_id, team_b_id=root.team_b_id,
                      roguelike=root.roguelike, created_at=datetime(2040, 1, 2))
        winners = [root.player_a_id] * 30 + [root.player_b_id] * 15 + [None] * 7
        a.db.session.add_all(a.Match(**common, winner_id=winner, status='finished', settlement_state='settled',
                                    scoring_rule=root.scoring_rule, context_version=root.context_version,
                                    player_a_score=0, player_b_score=0,
                                    player_a_difficulty=root.player_a_difficulty,
                                    player_b_difficulty=root.player_b_difficulty,
                                    player_a_multiplier=root.player_a_multiplier,
                                    player_b_multiplier=root.player_b_multiplier)
                             for winner in winners)
        pending = a.Match(**common, status='confirmed')
        segment = a.Match(**common, parent_id=root.id, status='finished', winner_id=root.player_a_id)
        exhibition = a.Match(**common, match_type='exhibition', status='finished', winner_id=root.player_a_id)
        a.db.session.add_all([pending, segment, exhibition])
        a.db.session.commit()
        pending_id, segment_id, exhibition_id = pending.id, segment.id, exhibition.id
        service_record = get_player_stats(root.player_a_id)
        assert len(service_record['matches']) == 50 and service_record['matches'][0].id == pending_id
        assert service_record['total_matches'] == 53

    login(client, ids['users'][0])
    record = player_data(application, client)
    assert client.get('/player/api/players/alpha-account').get_json() == record
    assert record['stats'] == {
        'wins': 31, 'losses': 15, 'draws': 7, 'total_matches': 53,
        'formal_matches': 54, 'decided_matches': 53, 'settled_matches': 53,
        'season_scope': 'current', 'season_id': ids['season'],
    }
    recent = record['recent_matches']
    assert len(recent) == 20 and recent[0]['id'] == pending_id
    assert {row['season_id'] for row in recent} == {ids['season']}
    assert not {segment_id, exhibition_id} & {row['id'] for row in recent}
    page = client.get('/tournament/my-matches?season=all').get_data(as_text=True)
    assert '共 54 场正式比赛' in page and 'page=2&amp;season=all' in page
    page = client.get('/tournament/my-matches?season=all&page=3').get_data(as_text=True)
    assert f'href="/tournament/match/{ids["match"]}"' in page


def test_scope_after_switch_and_without_active_season(application, client):
    ids = application.test_ids
    complete(application, client)
    assert client.post('/admin/new-season', data=transition_data(application, client)).status_code == 302
    with application.app_context():
        old = a.db.session.get(a.Tournament, ids['season'])
        old_number = old.season_number
        root = a.db.session.get(a.Match, ids['match'])
        orphan = a.Match(player_a_id=root.player_a_id, player_b_id=root.player_b_id,
                         roguelike=root.roguelike, status='finished', winner_id=root.player_a_id)
        a.db.session.add(orphan)
        a.Roguelike.query.filter_by(key='water').one().name = '本届替换后的赛道'
        a.db.session.commit()
        orphan_id = orphan.id

    login(client, ids['users'][0])
    active = player_data(application, client)
    assert active['stats']['wins'] == 0 and active['stats']['season_id'] != ids['season']
    assert active['player']['team'] is None and active['recent_matches'] == []
    all_record = player_data(application, client, 'all')
    assert all_record['stats']['wins'] == 1
    assert all_record['stats']['season_scope'] == 'all'
    assert [m['id'] for m in all_record['recent_matches']] == [ids['match']]
    assert all_record['recent_matches'][0]['season_number'] == old_number
    history_html = client.get(f"/history/{ids['season']}").get_data(as_text=True)
    assert '正式场次 1 · 已判胜负 1 · 已结算 1' in history_html

    for path in ('/player/profile', '/captain/profile', '/tournament/my-matches'):
        response = client.get(path)
        html = response.get_data(as_text=True)
        assert response.status_code == 200 and '本届战绩' in html and '该范围暂无正式比赛' in html
        assert f'href="/tournament/match/{ids["match"]}"' not in html
        response = client.get(path + '?season=all')
        html = response.get_data(as_text=True)
        assert response.status_code == 200 and '累计战绩' in html
        assert f'第 {old_number} 届 · 往届' in html
        assert '水月与深蓝之树' in html and '本届替换后的赛道' not in html
        assert f'href="/tournament/match/{ids["match"]}"' in html
        assert f'href="/tournament/match/{orphan_id}"' not in html
        assert '/submit-result' not in html

    with application.app_context():
        a.Tournament.get_active().is_active = False
        a.db.session.commit()
    current = player_data(application, client)
    assert current['stats']['season_id'] is None and current['stats']['formal_matches'] == 0
    assert current['player']['team'] is None and current['recent_matches'] == []
    assert player_data(application, client, 'all')['stats']['wins'] == 1
    no_active = client.get('/tournament/my-matches').get_data(as_text=True)
    assert '当前没有活跃赛季' in no_active and '该范围暂无正式比赛' in no_active
    assert client.get(f"/player/api/players/{ids['users'][0]}?season=archive").status_code == 400
    assert client.get('/player/api/players/alpha?season=archive').status_code == 400


def test_forfeit_counts_immediately_and_review_preserves_record(application, client):
    ids = application.test_ids
    match = f"/tournament/match/{ids['match']}"
    login(client, ids['users'][0])
    assert client.post(match + '/forfeit').status_code == 302
    record = player_data(application, client)
    assert (record['stats']['losses'], record['stats']['settled_matches']) == (1, 0)
    assert record['recent_matches'][0]['result'] == 'loss'

    login(client, ids['users'][1])
    assert client.post(match + '/submit-result', data={'score': 0, 'difficulty': 12}).status_code == 302
    login(client, ids['admin'])
    admin_match = f"/admin/match/{ids['match']}"
    assert client.post(admin_match + '/edit-result', data={'action': 'win_b'}).status_code == 302
    assert client.post(admin_match + '/review', data={'action': 'reject', 'reason': '验收驳回'}).status_code == 302
    result = player_data(application, client)
    assert (result['stats']['losses'], result['stats']['settled_matches']) == (1, 1)
    assert result['recent_matches'][0]['result'] == 'loss'
