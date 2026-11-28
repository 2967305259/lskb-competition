from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path

import pytest

import app as a
import calculator_submission
from calculator_rules import initial_configuration
from conftest import login, png_bytes
from test_calculator_preview import form_url
from test_calculator_publication import set_calculator_mode
from test_calculator_storage import publish_rule
from test_p0 import settle, submit, transition_data


def enable_calculator(application):
    ids = application.test_ids
    with application.app_context():
        publish_rule(ids, initial_configuration())
    with application.test_client() as admin:
        login(admin, ids['super'])
        assert set_calculator_mode(admin, ids['season'], 'calculator').status_code == 302


def calculator_form(scores, total, version=1, screenshot=None):
    return {
        'calculator_scores': json.dumps(scores), 'calculator_total': str(total),
        'calculator_rule_version': str(version), 'difficulty': '14',
        'screenshots': (BytesIO(png_bytes() if screenshot is None else screenshot), 'score.png'),
    }


def assert_no_calculator_write(application):
    with application.app_context():
        match = a.db.session.get(a.Match, application.test_ids['match'])
        assert match.player_a_score is None and match.scoring_rule is None
        assert a.MatchCalculatorResult.query.count() == a.ScoreLog.query.count() == a.UploadAsset.query.count() == 0
    assert not list((Path(application.config['UPLOAD_ROOT']) / 'screenshots').iterdir())


def test_first_submission_locks_version_and_settles_using_existing_multiplier(application, client):
    enable_calculator(application)
    ids = application.test_ids
    login(client, ids['users'][0])
    page = client.get(form_url(application)).get_data(as_text=True)
    assert 'name="score"' not in page and 'name="calculator_rule_version" value="1"' in page
    assert client.post(form_url(application), data=calculator_form({'items': {'basic-relic': '1.234'}}, '6.17')).status_code == 302

    with application.app_context():
        newer = initial_configuration()
        newer['basic']['groups'][0]['items'][0]['score'] = 10
        publish_rule(ids, newer, version=2, source='import')
        match = a.db.session.get(a.Match, ids['match'])
        assert match.player_a_score == 6 and match.score_origin_a == 'calculator'
        assert match.settlement_state == 'pending'
        card = a.db.session.get(a.MatchCalculatorResult, match.id)
        assert card.rule_version == 1
        assert json.loads(card.details_a)['scores']['items']['basic-relic'] == '1.234'
        segment = a.Match(tournament_id=match.tournament_id, parent_id=match.id,
                          player_a_id=match.player_a_id, player_b_id=match.player_b_id,
                          team_a_id=match.team_a_id, team_b_id=match.team_b_id, roguelike=match.roguelike)
        a.db.session.add(segment)
        a.db.session.commit()
        segment_id = segment.id

    login(client, ids['super'])
    assert set_calculator_mode(client, ids['season'], 'manual', 2).status_code == 302
    login(client, ids['users'][1])
    url = form_url(application, segment_id)
    page = client.get(url).get_data(as_text=True)
    assert 'name="calculator_rule_version" value="1"' in page
    assert client.post(url, data=calculator_form({'original': '-1.50'}, '-1.50', 2)).status_code == 409
    assert client.post(url, data=calculator_form({'original': '-1.50'}, '-1.50')).status_code == 302
    assert settle(application, client).status_code == 302
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        assert match.player_b_score == -1 and match.settlement_state == 'settled'
        assert [a.db.session.get(a.TeamMember, key).final_score for key in ids['members']] == [7, -1]
        card = a.db.session.get(a.MatchCalculatorResult, match.id)
        assert a.MatchCalculatorResult.query.count() == 1
        details = (card.details_a, card.details_b)
        entries = a.ScoreLog.query.filter_by(reason='计分器提交').order_by(a.ScoreLog.id).all()
        assert [json.loads(log.new_result)['raw_score'] for log in entries] == [6, -1]
    assert client.post(f"/admin/match/{ids['match']}/edit-result", data={
        'score_a': '9', 'action': 'win_a', 'reason': '校核原始成绩',
    }).status_code == 302
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        card = a.db.session.get(a.MatchCalculatorResult, match.id)
        assert match.score_origin_a == 'corrected'
        assert (card.details_a, card.details_b) == details
        assert a.db.session.get(a.TeamMember, ids['members'][0]).final_score == 10
    page = client.get(f"/tournament/match/{ids['match']}").get_data(as_text=True)
    assert '计分器 v1 · 原始计分 6.17' in page and '人工更正后按上方成绩结算' in page
    switch = transition_data(application, client)
    assert client.post('/admin/new-season', data=switch).status_code == 302
    login(client, ids['super'])
    assert client.post(f"/admin/history-corrections/{ids['season']}", data={
        'match_id': ids['match'], 'score_b': '2', 'reason': '复核往届成绩',
    }).status_code == 200
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        card = a.db.session.get(a.MatchCalculatorResult, match.id)
        assert match.score_origin_b == 'corrected'
        assert (card.details_a, card.details_b) == details


def test_calculator_rejects_forged_invalid_and_stale_submissions(application, client):
    enable_calculator(application)
    ids = application.test_ids
    with application.app_context():
        publish_rule(ids, initial_configuration(), version=2, source='import')
    login(client, ids['users'][0])
    url = form_url(application)
    checks = [
        ({'score': '99', 'difficulty': '14'}, 400, '填写计分明细'),
        (calculator_form({'original': '6.17'}, '6.17'), 409, '规则版本已更新'),
        (calculator_form({'original': '6.17'}, '9999', 2), 400, '重算的 6.17 不一致'),
        (calculator_form({'items': {'fake': True}}, '0', 2), 400, '不是该规则中的可填写条目'),
        (calculator_form({'original': 6.17}, '6.17', 2), 400, '计分明细格式无效'),
    ]
    for form, status, message in checks:
        response = client.post(url, data=form)
        assert response.status_code == status and message in response.get_data(as_text=True)
        assert_no_calculator_write(application)

    no_screenshot = calculator_form({'original': '6.17'}, '6.17', 2)
    no_screenshot.pop('screenshots')
    assert client.post(url, data=no_screenshot).status_code == 400
    assert_no_calculator_write(application)
    invalid = client.post(url, data=calculator_form({'original': '6.17'}, '6.17', 2, b'invalid image'))
    assert invalid.status_code == 400
    html = invalid.get_data(as_text=True)
    assert 'id="screenshots-error"' in html and '{"original": "6.17"}' in html
    assert_no_calculator_write(application)


def test_prior_manual_submission_survives_enabling_calculator(application, client):
    assert submit(application, client, 0, 0).status_code == 302
    enable_calculator(application)
    ids = application.test_ids
    login(client, ids['users'][1])
    page = client.get(form_url(application)).get_data(as_text=True)
    assert 'name="score"' in page and 'name="calculator_rule_version"' not in page
    assert submit(application, client, 1, 93).status_code == 302
    assert settle(application, client).status_code == 302
    with application.app_context():
        assert a.MatchCalculatorResult.query.count() == 0


def test_enabled_match_requires_an_available_rule(application, client):
    with application.app_context():
        a.Tournament.get_active().calculator_enabled = True
        a.db.session.commit()
    login(client, application.test_ids['users'][0])
    assert client.get(form_url(application)).status_code == 409
    assert client.post(form_url(application), data={'score': '99', 'difficulty': '14'}).status_code == 409
    assert_no_calculator_write(application)


def test_forfeit_zero_keeps_calculator_available_for_real_submission(application, client):
    enable_calculator(application)
    ids = application.test_ids
    login(client, ids['users'][0])
    assert client.post(f"/tournament/match/{ids['match']}/forfeit", data={'reason': '弃权'}).status_code == 302
    login(client, ids['admin'])
    response = client.post(f"/admin/match/{ids['match']}/edit-result", data={'score_b': '12', 'action': 'win_b'})
    assert response.status_code == 409
    login(client, ids['users'][1])
    assert client.post(form_url(application), data=calculator_form({'original': '0'}, '0')).status_code == 302
    assert settle(application, client).status_code == 302
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        assert (match.score_origin_a, match.score_origin_b) == ('forfeit_zero', 'calculator')
        assert (match.player_a_score, match.player_b_score) == (0, 0)
        card = a.db.session.get(a.MatchCalculatorResult, match.id)
        assert card.details_a is None and json.loads(card.details_b)['result']['total'] == '0.00'


def test_calculator_rolls_back_audit_and_file_on_late_failure(application, client, monkeypatch):
    enable_calculator(application)
    original = calculator_submission.record_submission

    def fail_after_audit(*args):
        original(*args)
        raise RuntimeError('injected calculator audit failure')

    monkeypatch.setattr(calculator_submission, 'record_submission', fail_after_audit)
    login(client, application.test_ids['users'][0])
    with pytest.raises(RuntimeError, match='calculator audit failure'):
        client.post(form_url(application), data=calculator_form({'original': '1'}, '1'))
    assert_no_calculator_write(application)


def test_duplicate_calculator_requests_create_one_score_and_one_audit(application):
    enable_calculator(application)

    def submit_once(_):
        with application.test_client() as worker:
            login(worker, application.test_ids['users'][0])
            return worker.post(form_url(application), data=calculator_form({'original': '0'}, '0')).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit_once, range(2))) == [302, 409]
    with application.app_context():
        assert a.MatchCalculatorResult.query.count() == a.ScoreLog.query.count() == a.UploadAsset.query.count() == 1
