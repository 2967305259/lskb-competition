from concurrent.futures import ThreadPoolExecutor
from html import unescape
from io import BytesIO
import json
from pathlib import Path
import re

import pytest

import app as a
from calculator_rules import MAX_CONFIG_BYTES, initial_configuration, parse_configuration
from conftest import login
from season_safety import SeasonError
from test_p0 import transition_data


RULES = '/admin/score-rules'
SWITCH = '/admin/new-season'
EXPORT = Path(__file__).with_name('fixtures') / 'calculator-v3-config.json'


def exported_rules():
    return json.loads(EXPORT.read_text(encoding='utf-8'))


def payload(export):
    return json.dumps(export, ensure_ascii=False).encode('utf-8')


def field(response, name):
    return unescape(re.search(rf'name="{name}" value="([^"]*)"', response.get_data(as_text=True)).group(1))


def confirmation(response):
    assert response.status_code == 200
    assert response.cache_control.no_store
    return {key: field(response, key) for key in ('score_rules_preview', 'score_rules_payload')}


def preview_import(client, export):
    return client.post(RULES, data={'operation': 'preview',
                                   'score_rules_file': (BytesIO(payload(export)), '计分配置_测试.json')})


def set_calculator_mode(client, season_id, mode, version=1):
    return client.post(f'{RULES}?season_id={season_id}', data={
        'operation': 'set_mode', 'calculator_mode': mode, 'calculator_rule_version': str(version),
    })


@pytest.mark.parametrize(('modify', 'message'), [
    (lambda x: x.pop('kind'), '导出配置'),
    (lambda x: x.update(version=4), '当前支持独立计分器 v3 配置'),
    (lambda x: x['config'].pop('penalty'), 'penalty'),
    (lambda x: x['config']['basic']['timeRules'][0].update(min=3), '最短用时'),
    (lambda x: x['config']['basic']['groups'][0]['items'][0].update(score=True), '有限数字'),
    (lambda x: x['config']['basic']['groups'][0]['items'][0].update(score=100001), '100000'),
    (lambda x: x['config']['basic']['groups'][0]['items'][0].update(type='custom'), 'threshold'),
    (lambda x: x['config']['basic']['timeOverflow'].update(perHour=float('inf')), '非有限数字'),
])
def test_import_validates_the_independent_config(modify, message):
    export = exported_rules()
    modify(export)
    with pytest.raises(SeasonError, match=message):
        parse_configuration(payload(export))


def test_initial_rules_match_real_independent_export_and_size_limits():
    config, changes = parse_configuration(b'\xef\xbb\xbf' + EXPORT.read_bytes())
    assert changes == []
    assert config == initial_configuration()
    with pytest.raises(SeasonError, match='64 KiB'):
        parse_configuration(b' ' * MAX_CONFIG_BYTES + b'{}')
    with pytest.raises(SeasonError, match='重复的 JSON 字段'):
        parse_configuration(b'{"app":"one","app":"two"}')


def test_only_super_admin_can_import_or_view_rules(application, client):
    for user_id in (application.test_ids['users'][0], application.test_ids['admin']):
        login(client, user_id)
        for path in (RULES, RULES + '/1'):
            assert client.get(path).status_code == 403
        assert preview_import(client, exported_rules()).status_code == 403
    form = transition_data(application, client, force=True)
    assert client.post(SWITCH, data={**form, 'score_rules_mode': 'import',
                                    'score_rules_file': (BytesIO(EXPORT.read_bytes()), 'rules.json')}).status_code == 403
    with application.app_context():
        assert a.ScoreRuleSet.query.count() == 0
        assert a.SeasonTransition.query.count() == 0


def test_publish_validated_export_and_fix_legacy_ids(application, client):
    ids = application.test_ids
    login(client, ids['super'])
    export = exported_rules()
    config = export['config']
    config['basic']['timeRules'] = []
    config['extraBonus']['groups'].append({
        'title': '补充规则',
        'items': [
            {'id': 'lv-1', 'name': '<script>旧编号</script>', 'type': 'fixed', 'score': -1.005},
            {'id': 'it-1', 'name': '保留编号', 'type': 'number', 'score': 0.125},
        ],
    })
    response = preview_import(client, export)
    form = confirmation(response)
    assert '用时不计分' in response.get_data(as_text=True)
    assert '重复条目编号' in response.get_data(as_text=True)
    assert '&lt;script&gt;旧编号' in response.get_data(as_text=True)
    assert len(json.loads(form['score_rules_payload'])['extraBonus']['groups'][-1]['items']) == 2
    with application.app_context():
        assert a.ScoreRuleSet.query.count() == 0

    tampered = {**form, 'score_rules_payload': form['score_rules_payload'].replace('-1.005', '-9.005')}
    assert client.post(RULES, data={**tampered, 'operation': 'publish'}).status_code == 400
    response = client.post(RULES, data={**form, 'operation': 'publish'})
    assert response.status_code == 302
    assert client.post(RULES, data={**form, 'operation': 'publish'}).status_code == 409
    view = client.get(f"{RULES}/1?season_id={ids['season']}")
    assert view.status_code == 200 and '保留编号' in view.get_data(as_text=True)
    with application.app_context():
        rule = a.ScoreRuleSet.query.one()
        config = json.loads(rule.config)
        added = config['extraBonus']['groups'][-1]['items']
        assert [item['id'] for item in added] == ['it-2', 'it-1']
        assert [item['score'] for item in added] == [-1.005, 0.125]
        assert a.SystemLog.query.filter_by(action='publish_calculator_rules').count() == 1


def test_skip_switch_creates_initial_rules_and_allows_later_import(application, client):
    ids = application.test_ids
    form = transition_data(application, client, force=True)
    missing = {key: value for key, value in form.items() if key != 'score_rules_mode'}
    assert client.post(SWITCH, data=missing).status_code == 400
    assert client.post(SWITCH, data=form).status_code == 302
    assert client.post(SWITCH, data=form).status_code == 302
    with application.app_context():
        first = a.ScoreRuleSet.query.one()
        current_id = a.Tournament.get_active().id
        assert first.tournament_id == current_id != ids['season']
        assert first.version == 1 and first.source == 'initial' and first.published_by_id == ids['admin']
        assert json.loads(first.config) == initial_configuration()

    login(client, ids['super'])
    export = exported_rules()
    export['config']['basic']['scoreLabel'] = '补导入原始分'
    form = confirmation(client.post(RULES, data={'operation': 'preview', 'score_rules_json': payload(export).decode()}))
    assert client.post(RULES, data={**form, 'operation': 'publish'}).status_code == 302
    page = client.get(f'{RULES}/1?season_id={current_id}').get_data(as_text=True)
    assert '初始配置' in page and '补导入原始分' not in page
    with application.app_context():
        published = a.ScoreRuleSet.query.order_by(a.ScoreRuleSet.version).all()
        assert [(row.version, row.source) for row in published] == [(1, 'initial'), (2, 'import')]
        assert json.loads(published[1].config)['basic']['scoreLabel'] == '补导入原始分'


def test_switch_import_previews_and_preserves_archived_rules(application, client):
    ids = application.test_ids
    login(client, ids['super'])
    before = confirmation(preview_import(client, exported_rules()))
    assert client.post(RULES, data={**before, 'operation': 'publish'}).status_code == 302
    stale_publish = confirmation(preview_import(client, exported_rules()))
    page = client.get('/admin/tournament-settings')
    switch = {key: field(page, key) for key in ('idempotency_key', 'expected_current_season_id')}
    switch.update(score_rules_mode='import', season_name='换届计分测试', force='1', force_reason='隔离测试')
    invalid = client.post(SWITCH, data={**switch, 'score_rules_file': (BytesIO(b'{}'), 'invalid.json')})
    assert invalid.status_code == 400
    assert '跳过，用初始配置开启新赛季' in invalid.get_data(as_text=True)
    export = exported_rules()
    export['config']['basic']['scoreLabel'] = '换届原始分'
    response = client.post(SWITCH, data={**switch, 'score_rules_file': (BytesIO(payload(export)), 'rules.json')})
    preview = confirmation(response)
    assert field(response, 'season_name') == switch['season_name']
    assert field(response, 'force_reason') == switch['force_reason']
    with application.app_context():
        assert a.Tournament.get_active().id == ids['season']
        assert a.ScoreRuleSet.query.count() == 1
    assert client.post(SWITCH, data={**switch, **preview}).status_code == 302
    assert client.post(RULES, data={**stale_publish, 'operation': 'publish'}).status_code == 409
    assert client.post(f"{RULES}?season_id={ids['season']}", data={
        **before, 'operation': 'publish',
    }).status_code == 403
    archive = client.get(f"{RULES}/1?season_id={ids['season']}")
    assert archive.status_code == 200 and '换届原始分' not in archive.get_data(as_text=True)
    with application.app_context():
        active = a.Tournament.get_active()
        assert active.name == '换届计分测试'
        selected = a.ScoreRuleSet.query.filter_by(tournament_id=active.id).one()
        assert (selected.version, selected.source) == (1, 'import')
        assert json.loads(selected.config)['basic']['scoreLabel'] == '换届原始分'
        assert json.loads(a.SystemLog.query.filter_by(action='new_season').one().detail)['score_rule_source'] == 'import'


def test_simultaneous_rule_publication_creates_one_version(application, client):
    operator = application.test_ids['super']
    login(client, operator)
    form = confirmation(preview_import(client, exported_rules()))

    def publish(_):
        with application.test_client() as worker:
            login(worker, operator)
            return worker.post(RULES, data={**form, 'operation': 'publish'}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(publish, range(2))) == [302, 409]
    with application.app_context():
        assert a.ScoreRuleSet.query.one().version == 1
        assert a.SystemLog.query.filter_by(action='publish_calculator_rules').count() == 1


def test_failed_rule_audit_rolls_back_switch(application, client, monkeypatch):
    old_id = application.test_ids['season']
    form = transition_data(application, client, force=True)
    original = a.log_system_action

    def fail_calculator_publication(action, detail=''):
        if action == 'publish_calculator_rules':
            raise RuntimeError('injected calculator audit failure')
        return original(action, detail)

    monkeypatch.setattr(a, 'log_system_action', fail_calculator_publication)
    with pytest.raises(RuntimeError, match='calculator audit failure'):
        client.post(SWITCH, data=form)
    with application.app_context():
        assert a.Tournament.get_active().id == old_id
        assert a.Tournament.query.count() == 1
        assert a.SeasonTransition.query.count() == a.ScoreRuleSet.query.count() == 0


def test_enabling_requires_super_admin_and_the_latest_valid_rule(application, client):
    ids = application.test_ids
    season_id = ids['season']
    login(client, ids['admin'])
    assert set_calculator_mode(client, season_id, 'calculator').status_code == 403
    login(client, ids['super'])
    assert b'value="set_mode"' not in client.get(RULES).data
    assert set_calculator_mode(client, season_id, 'calculator').status_code == 409
    assert client.post(RULES, data={'operation': 'set_mode', 'calculator_mode': 'calculator'}).status_code == 400

    for version in (1, 2):
        preview = confirmation(preview_import(client, exported_rules()))
        assert client.post(RULES, data={**preview, 'operation': 'publish'}).status_code == 302
    assert set_calculator_mode(client, season_id, 'calculator').status_code == 409
    assert set_calculator_mode(client, season_id, 'unknown', 2).status_code == 400
    with application.app_context():
        assert not a.Tournament.get_active().calculator_enabled
        assert a.SystemLog.query.filter_by(action='set_calculator_mode').count() == 0

    assert set_calculator_mode(client, season_id, 'calculator', 2).status_code == 302
    assert set_calculator_mode(client, season_id, 'calculator', 2).status_code == 302
    assert '当前使用计分器' in client.get(RULES).get_data(as_text=True)
    assert set_calculator_mode(client, season_id, 'manual').status_code == 302
    assert '当前使用手填成绩' in client.get(RULES).get_data(as_text=True)
    with application.app_context():
        events = a.SystemLog.query.filter_by(action='set_calculator_mode').order_by(a.SystemLog.id).all()
        assert [(e.user_id, json.loads(e.detail)) for e in events] == [
            (ids['super'], {'tournament_id': season_id, 'old_mode': 'manual',
                           'new_mode': 'calculator', 'rule_version': 2}),
            (ids['super'], {'tournament_id': season_id, 'old_mode': 'calculator',
                           'new_mode': 'manual', 'rule_version': 2}),
        ]
    assert set_calculator_mode(client, season_id, 'calculator', 2).status_code == 302

    switch = transition_data(application, client, force=True)
    assert client.post(SWITCH, data=switch).status_code == 302
    login(client, ids['super'])
    assert set_calculator_mode(client, season_id, 'manual').status_code == 403
    with application.app_context():
        assert not a.Tournament.get_active().calculator_enabled
        assert a.db.session.get(a.Tournament, season_id).calculator_enabled


def test_enabling_rejects_invalid_published_configuration(application, client):
    ids = application.test_ids
    login(client, ids['super'])
    preview = confirmation(preview_import(client, exported_rules()))
    assert client.post(RULES, data={**preview, 'operation': 'publish'}).status_code == 302
    with application.app_context():
        corrupted = initial_configuration()
        corrupted['basic']['groups'][0]['items'][0].pop('score')
        table = a.ScoreRuleSet.__table__
        a.db.session.execute(table.update().values(config=json.dumps(corrupted)))
        a.db.session.commit()

    response = set_calculator_mode(client, ids['season'], 'calculator')
    assert response.status_code == 409
    assert '请发布有效配置后重试' in response.get_data(as_text=True)
    with application.app_context():
        assert not a.Tournament.get_active().calculator_enabled


def test_mode_change_rolls_back_if_audit_write_fails(application, client, monkeypatch):
    ids = application.test_ids
    login(client, ids['super'])
    preview = confirmation(preview_import(client, exported_rules()))
    assert client.post(RULES, data={**preview, 'operation': 'publish'}).status_code == 302
    original = a.log_system_action

    def fail_mode_audit(action, detail=''):
        if action == 'set_calculator_mode':
            raise RuntimeError('injected calculator mode audit failure')
        return original(action, detail)

    monkeypatch.setattr(a, 'log_system_action', fail_mode_audit)
    with pytest.raises(RuntimeError, match='calculator mode audit failure'):
        set_calculator_mode(client, ids['season'], 'calculator')
    with application.app_context():
        assert not a.Tournament.get_active().calculator_enabled
        assert a.SystemLog.query.filter_by(action='set_calculator_mode').count() == 0
