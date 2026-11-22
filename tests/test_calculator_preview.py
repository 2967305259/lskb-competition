from io import BytesIO
import json
import re

import app as a
from calculator_rules import initial_configuration
from conftest import login, png_bytes
from test_calculator_storage import publish_rule, store_first_submission


def form_url(application, match_id=None):
    return f"/tournament/match/{match_id or application.test_ids['match']}/submit-result"


def embedded_config(page):
    markup = re.search(r'<script type="application/json" id="match-calculator-config">(.*?)</script>',
                       page, re.DOTALL)
    assert markup, page
    return json.loads(markup.group(1))


def test_preview_uses_current_season_rule_and_escapes_published_labels(application, client):
    ids = application.test_ids
    login(client, ids['users'][0])
    original = client.get(form_url(application))
    assert original.status_code == 200
    assert b'match-preview.js' not in original.data
    assert b'name="score"' in original.data

    configuration = initial_configuration()
    configuration['basic']['scoreLabel'] = '</script><script>alert(1)</script>'
    with application.app_context():
        publish_rule(ids, initial_configuration())
        publish_rule(ids, configuration, version=2, source='import')

    response = client.get(form_url(application))
    assert response.status_code == 200 and response.cache_control.no_store
    page = response.get_data(as_text=True)
    assert embedded_config(page) == configuration
    assert '规则 v2' in page and '预览总分未乘难度倍率' in page
    assert '<script>alert(1)</script>' not in page
    assert page.index('data-calculator-preview') < page.index('name="difficulty"') < page.index('name="screenshots"')
    assert 'name="csrf_token"' in page and 'enctype="multipart/form-data"' in page
    for asset in ('score-calculator.js', 'match-preview.js', 'match-preview.css'):
        assert f'/static/calculator/{asset}' in page
        assert client.get(f'/static/calculator/{asset}').status_code == 200


def test_locked_rule_is_used_from_segment_and_preview_is_participant_only(application, client):
    ids = application.test_ids
    first = initial_configuration()
    with application.app_context():
        publish_rule(ids, first)
        store_first_submission(ids, first)
        newer = initial_configuration()
        newer['basic']['scoreLabel'] = '新版原始分'
        publish_rule(ids, newer, version=2, source='import')
        parent = a.db.session.get(a.Match, ids['match'])
        segment = a.Match(tournament_id=parent.tournament_id, parent_id=parent.id,
                          player_a_id=parent.player_a_id, player_b_id=parent.player_b_id,
                          team_a_id=parent.team_a_id, team_b_id=parent.team_b_id, roguelike=parent.roguelike)
        a.db.session.add(segment)
        a.db.session.commit()
        segment_id = segment.id

    login(client, ids['users'][1])
    response = client.get(form_url(application, segment_id))
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert embedded_config(page) == first
    assert '规则 v1' in page and '新版原始分' not in page

    login(client, ids['admin'])
    denied = client.get(form_url(application, segment_id))
    assert denied.status_code == 403 and b'match-calculator-config' not in denied.data
    with application.app_context():
        a.Tournament.get_active().is_active = False
        a.db.session.commit()
    login(client, ids['users'][1])
    archived = client.get(form_url(application, segment_id))
    assert archived.status_code == 403 and b'match-calculator-config' not in archived.data


def test_exhibition_uses_existing_form_without_calculator(application, client):
    ids = application.test_ids
    with application.app_context():
        publish_rule(ids, initial_configuration())
        a.db.session.get(a.Match, ids['match']).match_type = 'exhibition'
        a.db.session.commit()

    login(client, ids['users'][0])
    response = client.get(form_url(application))
    assert response.status_code == 200
    assert b'name="score"' in response.data and b'match-calculator-config' not in response.data


def test_unreadable_published_rule_leaves_manual_form_usable(application, client):
    ids = application.test_ids
    with application.app_context():
        rule = publish_rule(ids, initial_configuration())
        # Simulate damaged historical storage independently of the guarded publication route.
        table = a.ScoreRuleSet.__table__
        a.db.session.execute(table.update().where(table.c.id == rule.id).values(config='null'))
        a.db.session.commit()

    login(client, ids['users'][0])
    response = client.get(form_url(application))
    assert response.status_code == 200
    assert b'name="score"' in response.data and b'match-calculator-config' not in response.data


def test_preview_does_not_change_existing_score_and_screenshot_submission(application, client):
    ids = application.test_ids
    with application.app_context():
        publish_rule(ids, initial_configuration())
    login(client, ids['users'][0])
    form = {'score': '26', 'difficulty': '14', 'calculator_scores': '{"original":"999"}'}
    reported = client.post(form_url(application), data={
        **form, 'screenshots': (BytesIO(b'invalid image'), 'invalid.png'),
    })
    assert reported.status_code == 400
    page = reported.get_data(as_text=True)
    assert embedded_config(page) == initial_configuration()
    assert 'id="screenshots-error"' in page and 'name="score" value="26"' in page
    assert '<option value="14" selected>' in page

    accepted = client.post(form_url(application), data={
        **form, 'screenshots': (BytesIO(png_bytes()), 'score.png'),
    })
    assert accepted.status_code == 302
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        assert match.player_a_score == 26 and match.player_a_difficulty == 14
        assert match.score_origin_a == 'submitted'
        assert a.MatchCalculatorResult.query.count() == 0
