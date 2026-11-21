import json

import pytest

import app as a
from app import db
from conftest import login
from scoring import get_member_score_breakdown
from season_safety import SeasonError
from management import data_report
from test_p0 import complete


def add_adjustment(application, client, target='130'):
    ids = application.test_ids
    login(client, ids['admin'])
    response = client.post(
        f"/admin/team/{ids['teams'][0]}/update-scores",
        data={f"score_{ids['members'][0]}": target, 'reason': 'P1-6 校准'},
    )
    assert response.status_code == 302


def test_adjustment_breakdown_survives_rebuild_and_is_audited(application, client):
    complete(application, client)
    ids = application.test_ids
    add_adjustment(application, client)
    with application.app_context():
        breakdown = get_member_score_breakdown(ids['members'][0])
        assert breakdown == {
            'member_id': ids['members'][0], 'tournament_id': ids['season'],
            'match_score': 120, 'adjustment_score': 10, 'final_score': 130,
        }
        detail = json.loads(a.SystemLog.query.filter_by(action='score_adjustment').one().detail)
        assert detail['before']['final_score'] == 120
        assert detail['after']['final_score'] == 130
        assert detail['tournament'] == ids['season']
    assert client.post('/admin/reset-scores').status_code == 302
    with application.app_context():
        assert get_member_score_breakdown(ids['members'][0])['final_score'] == 130
        assert db.session.get(a.Team, ids['teams'][0]).team_score == 65000


def test_adjustment_immutable_fields_cannot_be_changed(application, client):
    complete(application, client)
    add_adjustment(application, client)
    with application.app_context():
        adjustment = a.ScoreAdjustment.query.one()
        adjustment.delta = 999
        with pytest.raises(SeasonError, match='人工调整记录'):
            db.session.commit()
        db.session.rollback()
        assert a.ScoreAdjustment.query.one().delta == 10


def test_revoke_audit_contains_before_after_and_rebuilds(application, client):
    complete(application, client)
    add_adjustment(application, client)
    ids = application.test_ids
    with application.app_context():
        adjustment_id = a.ScoreAdjustment.query.one().id
    response = client.post(
        f'/admin/adjustments/{adjustment_id}/revoke',
        data={'reason': '撤销 P1-6 校准'},
    )
    assert response.status_code == 200
    with application.app_context():
        adjustment = db.session.get(a.ScoreAdjustment, adjustment_id)
        assert adjustment.revoked
        detail = json.loads(a.SystemLog.query.filter_by(action='revoke_adjustment').one().detail)
        assert detail['before']['final_score'] == 130
        assert detail['after']['final_score'] == 120
        assert db.session.get(a.Team, ids['teams'][0]).team_score == 60000


def test_revoke_audit_failure_rolls_back_adjustment_and_score(application, client, monkeypatch):
    complete(application, client)
    add_adjustment(application, client)
    ids = application.test_ids
    with application.app_context():
        adjustment_id = a.ScoreAdjustment.query.one().id
    import season_safety

    def fail(*args, **kwargs):
        raise RuntimeError('audit failed')

    monkeypatch.setattr(season_safety, '_log', fail)
    with pytest.raises(RuntimeError):
        client.post(f'/admin/adjustments/{adjustment_id}/revoke',
                    data={'reason': '必须回滚'})
    with application.app_context():
        assert not db.session.get(a.ScoreAdjustment, adjustment_id).revoked
        assert get_member_score_breakdown(ids['members'][0])['final_score'] == 130


def test_data_report_lists_adjustment_ledger(application, client):
    complete(application, client)
    add_adjustment(application, client)
    with application.app_context():
        report = data_report()
        assert len(report['adjustments']) == 1
        member = next(row for row in report['member_totals']
                      if row['id'] == application.test_ids['members'][0])
        assert member['breakdown']['match_score'] == 120
        assert member['breakdown']['adjustment_score'] == 10
