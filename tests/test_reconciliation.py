import copy
import json

import pytest
from sqlalchemy import text
import app as a
from app import db
from management import data_report, reconcile_plan
from scoring import recalculate_all_scores
from season_safety import SeasonError, scoring_rule
from conftest import login
from test_p0 import transition_data


def legacy_plan(application):
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE matches SET status='finished', player_a_score=100, player_b_score=90, player_a_difficulty=14,player_b_difficulty=14,settlement_state='legacy',scoring_rule=NULL"))
        conn.execute(text('UPDATE team_members SET final_score=CASE WHEN id=:a THEN 120 ELSE 108 END'), {'a':application.test_ids['members'][0]})
    rule = scoring_rule(a.Roguelike.query.filter_by(key='water').one())
    return {'confirmed': True, 'reason': '按原始截图和赛事规则人工核对', 'report_digest': data_report()['digest'],
        'team_total_decisions': [{'team_id':application.test_ids['teams'][0], 'before':0, 'after':60000}, {'team_id':application.test_ids['teams'][1], 'before':0, 'after':54000}],
        'matches': [{'id':application.test_ids['match'],'tournament_id':application.test_ids['season'],
                     'rule':rule,'score_a':100,'score_b':90,'difficulty_a':14,'difficulty_b':14,'outcome':'win_a'}]}


def test_report_is_read_only_and_plan_is_explicit(application):
    with application.app_context():
        plan = legacy_plan(application)
        before = data_report()
        assert data_report() == before
        with pytest.raises(SeasonError):
            recalculate_all_scores()
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 120
        operator = db.session.get(a.User, application.test_ids['super'])
        with pytest.raises(SeasonError):
            reconcile_plan(dict(plan, confirmed=False), operator)
        reconcile_plan(plan, operator)
        match = db.session.get(a.Match, application.test_ids['match'])
        assert match.scoring_rule and match.settlement_state == 'settled'
        assert a.SystemLog.query.filter_by(action='legacy_reconciliation').count() == 1
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 120


def test_unknown_difference_not_automatically_an_adjustment(application):
    with application.app_context():
        plan = legacy_plan(application)
        with db.engine.begin() as conn:
            conn.execute(text('UPDATE team_members SET final_score=999'))
        db.session.expire_all()
        plan['report_digest'] = data_report()['digest']
        operator = db.session.get(a.User, application.test_ids['super'])
        with pytest.raises(SeasonError, match='累计变化尚未明确确认'):
            reconcile_plan(plan, operator)
        assert a.ScoreAdjustment.query.count() == 0
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 999
        assert db.session.get(a.Match, application.test_ids['match']).settlement_state == 'legacy'


def test_forced_archive_unsettled_can_only_finish_via_super_correction(application, client):
    data = transition_data(application, client, True)
    assert client.post('/admin/new-season', data=data).status_code == 302
    login(client, application.test_ids['super'])
    response = client.post(f"/admin/history-corrections/{application.test_ids['season']}", data={
        'match_id':application.test_ids['match'], 'score_a':100,'score_b':90,'diff_a':14,'diff_b':14,
        'confirm_settlement':'1','action':'win_a','reason':'按备存材料纠正强制归档场次'})
    assert response.status_code == 200
    with application.app_context():
        assert db.session.get(a.Match, application.test_ids['match']).settlement_state == 'settled'
        assert db.session.get(a.TeamMember, application.test_ids['members'][0]).final_score == 120
