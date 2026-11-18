from decimal import Decimal
from io import BytesIO

from openpyxl import load_workbook

import app as a
from conftest import login
from test_p0 import complete, submit, transition_data


def test_configured_terms_in_player_form_admin_messages_and_export(application, client):
    application.config.update(
        TRACK_LABEL='赛段', MEMBER_FIELD1_LABEL='编组', MEMBER_FIELD2_LABEL='起始角色',
    )
    ids = application.test_ids
    login(client, ids['users'][0])
    path = '/player/team/roguelike'
    page = client.get(path).get_data(as_text=True)
    assert all(text in page for text in ('赛段主题', '所选编组', '起始角色', 'maxlength="50"'))
    invalid = client.post(path, data={'roguelike': 'water', 'squad': '甲' * 51, 'operator': '乙' * 51})
    assert invalid.status_code == 200
    assert '编组名称最多50个字符' in invalid.get_data(as_text=True)
    assert '起始角色名称最多50个字符' in invalid.get_data(as_text=True)
    response = client.post(path, data={'roguelike': 'water', 'squad': '甲', 'operator': '乙'},
                           follow_redirects=True)
    assert response.status_code == 200
    assert '已选择赛段：水月与深蓝之树' in response.get_data(as_text=True)

    login(client, ids['admin'])
    rules = client.get('/admin/rules').get_data(as_text=True)
    assert '关联赛段' in rules and '主规则（不关联赛段）' in rules
    response = client.get('/admin/export')
    assert response.status_code == 200
    workbook = load_workbook(BytesIO(response.data))
    assert workbook['报名信息']['G1'].value == '赛段选择'
    assert workbook['比赛结果']['C1'].value == '赛段'

    with application.app_context():
        a.Roguelike.query.update({'is_active': False})
        a.db.session.commit()
    dashboard = client.get('/admin/dashboard').get_data(as_text=True)
    assert '没有启用赛段，不能开放参赛' in dashboard
    assert '开放报名（需要启用赛段）' in client.get('/admin/registration').get_data(as_text=True)
    response = client.post('/admin/start-tournament')
    assert response.status_code == 409
    assert '没有启用赛段，不能开放参赛' in response.get_data(as_text=True)


def test_admin_track_defaults_and_validation_use_config(application, client):
    application.config.update(
        TRACK_LABEL='赛段', DEFAULT_DIFFICULTY_MIN=5, DEFAULT_DIFFICULTY_MAX=7,
        DEFAULT_DIFFICULTY_STEP=0.125,
    )
    login(client, application.test_ids['admin'])
    page = client.get('/admin/roguelikes').get_data(as_text=True)
    assert 'name="difficulty_min" value="5"' in page
    assert 'name="difficulty_max" value="7"' in page
    assert 'name="difficulty_step" value="0.125"' in page
    added = client.post('/admin/roguelike/add', data={'key': 'timed', 'name': '计时赛段'},
                        follow_redirects=True)
    assert added.status_code == 200
    assert '已添加' in added.get_data(as_text=True)

    with application.app_context():
        created = a.Roguelike.query.filter_by(key='timed').one()
        assert (created.difficulty_min, created.difficulty_max, created.difficulty_step) == (5, 7, 0.125)
        track_id = created.id
    edit = f'/admin/roguelike/{track_id}/edit'
    assert client.post(edit, data={'name': '已编辑赛段', 'difficulty_min': '6', 'is_active': '1'}).status_code == 302
    assert client.post(edit, data={'difficulty_max': '5', 'is_active': '1'}).status_code == 400
    assert client.post('/admin/roguelike/add', data={
        'key': 'bad', 'name': '无效赛段', 'difficulty_step': '1e400',
    }).status_code == 400
    with application.app_context():
        created = a.db.session.get(a.Roguelike, track_id)
        assert (created.difficulty_min, created.difficulty_max, created.difficulty_step) == (6, 7, 0.125)
        assert a.Roguelike.query.filter_by(key='bad').first() is None


def test_result_form_uses_existing_match_rule_after_track_edit(application, client):
    ids = application.test_ids
    path = f"/tournament/match/{ids['match']}/submit-result"
    login(client, ids['users'][0])
    assert '<option value="14">14 难</option>' in client.get(path).get_data(as_text=True)
    with application.app_context():
        assert a.db.session.get(a.Match, ids['match']).scoring_rule is None
        track_id = a.Roguelike.query.filter_by(key='water').one().id
    assert submit(application, client, 0, 100).status_code == 302
    login(client, ids['admin'])
    assert client.post(f'/admin/roguelike/{track_id}/edit', data={
        'difficulty_min': '5', 'difficulty_max': '7', 'difficulty_step': '0.5', 'is_active': '1',
    }).status_code == 302
    login(client, ids['users'][1])
    page = client.get(path).get_data(as_text=True)
    assert '<option value="14">14 难（使用该整场保存的难度规则）</option>' in page
    assert '<option value="5">' not in page
    assert client.post(path, data={'score': '90', 'difficulty': '5'}).status_code == 400
    assert submit(application, client, 1, 90).status_code == 302
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        assert match.player_b_multiplier == Decimal('1.2000')


def test_missing_track_requires_review_instead_of_default_difficulty(application, client):
    ids = application.test_ids
    application.config['TRACK_LABEL'] = '赛段'
    # Simulate a preexisting database with a broken reference.
    with application.app_context(), a.db.engine.begin() as connection:
        connection.execute(a.db.delete(a.Roguelike).where(a.Roguelike.key == 'water'))
    login(client, ids['users'][0])
    base = f"/tournament/match/{ids['match']}"
    detail = client.get(base)
    assert detail.status_code == 200
    assert 'water（名称待核对）' in detail.get_data(as_text=True)
    for response in (client.get(base + '/submit-result'), submit(application, client, 0, 100)):
        assert response.status_code == 409
        assert '比赛所用赛段不存在，须先核对' in response.get_data(as_text=True)
    with application.app_context():
        match = a.db.session.get(a.Match, ids['match'])
        assert match.scoring_rule is None and match.player_a_score is None


def test_new_terms_leave_archived_labels_names_and_scores_frozen(application, client):
    ids = application.test_ids
    application.config.update(TRACK_LABEL='上一届赛制', MEMBER_FIELD1_LABEL='旧编组')
    complete(application, client)
    assert client.post('/admin/new-season', data=transition_data(application, client)).status_code == 302
    application.config.update(TRACK_LABEL='本届赛段', MEMBER_FIELD1_LABEL='新编组')
    with application.app_context():
        track_id = a.Roguelike.query.filter_by(key='water').one().id
    assert client.post(f'/admin/roguelike/{track_id}/edit', data={
        'name': '本届赛段名称', 'is_active': '1',
    }, follow_redirects=True).status_code == 200
    history = client.get(f"/history/{ids['season']}")
    assert history.status_code == 200
    page = history.get_data(as_text=True)
    assert '<th>上一届赛制</th>' in page
    assert '水月与深蓝之树' in page
    assert '本届赛段名称' not in page
    assert '60000' in page and '54000' in page
    assert '本届赛段管理' in client.get('/admin/roguelikes').get_data(as_text=True)
    with application.app_context():
        archived = a.db.session.get(a.Tournament, ids['season'])
        assert a.historical_context(archived)['labels']['field1'] == '旧编组'
