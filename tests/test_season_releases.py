from io import BytesIO
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

import app as a
from backup import restore_backup, verify_backup
from conftest import login, make_test_app
from management import initialize_database
from migrations import SCHEMA_VERSION, assert_schema_current, migrate_database
from season_releases import _seal
from test_p0 import complete, transition_data
from test_p1_acceptance import workbook


URL = '/admin/release-packs'


def configure_release(application):
    with application.app_context():
        season = a.Tournament.get_active()
        season.window_times_utc = True
        season.bonus_score = 100
        a.Setting.get_instance().advance_count = 1
        a.db.session.add(a.Rule(title='本届规则', content='以整场为准\n<b>规则正文</b>'))
        track = a.Roguelike.query.filter_by(key='water').one()
        track.name = '自定义赛道'
        track.difficulty_step = 0.2
        a.db.session.add(a.CarouselItem(tournament_id=season.id, file_path='1.png', title='赛事封面'))
        a.db.session.commit()
        return season.id


def action(client, action_name, season_id, **values):
    return client.post(f'{URL}/{action_name}', data={'season_id': season_id, **values})


def draft_checksum(application, season_id):
    with application.app_context():
        return a.SeasonRelease.query.filter_by(tournament_id=season_id, version=0).one().checksum


def publish(application, client, season_id):
    assert action(client, 'capture', season_id).status_code == 302
    checksum = draft_checksum(application, season_id)
    assert action(client, 'publish', season_id, checksum=checksum).status_code == 302
    with application.app_context():
        release = a.SeasonRelease.query.filter_by(tournament_id=season_id).order_by(a.SeasonRelease.version.desc()).first()
        release_id = release.id
    response = client.get(f'{URL}/{release_id}/json')
    assert response.status_code == 200 and response.cache_control.no_store
    return release_id, response.get_json()


@pytest.mark.parametrize('next_track,next_step,next_bonus', [('sami', 0.3, 200), ('garden', 0.4, 300)])
def test_publication_and_two_season_settlement(application, client, next_track, next_step, next_bonus):
    season_id = configure_release(application)
    complete(application, client)
    ids = application.test_ids
    super_id = ids['super']
    login(client, ids['admin'])
    assert client.post(f"/admin/team/{ids['teams'][0]}/update-scores",
                       data={f"score_{ids['members'][0]}": 150, 'reason': '联验人工调整'}).status_code == 302
    assert workbook(client)['队伍积分榜']['D2'].value == 15000
    login(client, super_id)
    assert client.post('/admin/data-tools/backup').status_code == 302
    batch, = [path.parent for path in Path(application.config['BACKUP_DIR']).glob('*/manifest.json')]
    assert verify_backup(batch)['rows']['score_adjustments'] == 1
    assert client.get(URL).status_code == 200
    first_id, first = publish(application, client, season_id)
    assert first['payload']['schema_version'] == SCHEMA_VERSION
    assert first['payload']['publication']['version'] == 1
    assert first['payload']['publication']['publisher'] == 'super'
    assert first['payload']['configuration']['carousel'][0]['file_path'] == '1.png'
    assert first['payload']['configuration']['media'][0]['sha256']
    markdown = client.get(f'{URL}/{first_id}/md')
    assert markdown.status_code == 200 and first['sha256'] in markdown.get_data(as_text=True)
    assert '本届规则' in markdown.get_data(as_text=True)
    assert first['sha256'] in client.get(URL).get_data(as_text=True)

    assert action(client, 'capture', season_id).status_code == 302
    stale = draft_checksum(application, season_id)
    with application.app_context():
        a.Rule.query.one().title = '修订规则'
        a.db.session.commit()
    assert action(client, 'publish', season_id, checksum=stale).status_code == 409
    second_id, second = publish(application, client, season_id)
    assert second_id != first_id and second['payload']['publication']['version'] == 2
    assert client.get(f'{URL}/{first_id}/json').get_json() == first

    transition = transition_data(application, client)
    transition.update(inherit_advance_count='1', inherit_carousel='1')
    assert client.post('/admin/new-season', data=transition).status_code == 302
    archived = client.get(f'/history/{season_id}').get_data(as_text=True)
    assert '赛事发布包 v2' in archived and second['sha256'] in archived
    assert '&lt;b&gt;规则正文&lt;/b&gt;' in archived and '<b>规则正文</b>' not in archived
    with application.app_context():
        current_season = a.Tournament.get_active()
        assert current_season.id != season_id
        current_id = current_season.id
        archive_before_apply = a.db.session.get(a.Tournament, season_id).context_snapshot
    login(client, super_id)
    assert action(client, 'copy', current_id, release_id=first_id).status_code == 302
    assert action(client, 'apply', current_id, checksum=first['sha256']).status_code == 302
    with application.app_context():
        assert a.Rule.query.one().title == '本届规则'
        assert a.db.session.get(a.Tournament, season_id).context_snapshot == archive_before_apply
        assert a.Match.query.filter_by(tournament_id=season_id, settlement_state='settled').count() == 1
        a.Rule.query.one().title = '新届规则'
        other_track = a.Roguelike.query.filter_by(key=next_track).one()
        other_track.difficulty_step = next_step
        a.db.session.commit()
    assert client.post('/admin/tournament-settings', data={'team_size': '1'}).status_code == 302
    assert client.post('/admin/score-config', data={'advance_count': '1', 'bonus_score': str(next_bonus)}).status_code == 302
    _, imported = publish(application, client, current_id)
    assert imported['payload']['source']['sha256'] == first['sha256']
    assert imported['payload']['source']['version'] == 1
    assert imported['payload']['configuration']['rules'][0]['title'] == '新届规则'
    assert client.post(f'{URL}/apply', data={'season_id': current_id, 'checksum': first['sha256']}).status_code == 409
    login(client, ids['users'][0])
    assert client.post('/captain/team/create', data={'team_name': '新届甲队'}).status_code == 403
    login(client, ids['admin'])
    assert client.post('/admin/registration', data={'registration_open': 'open'}).status_code == 302
    for user, team in zip(ids['users'], ('新届甲队', '新届乙队')):
        login(client, user)
        assert client.post('/captain/team/create', data={'team_name': team}).status_code == 302
        assert client.post('/player/team/roguelike', data={'roguelike': next_track}).status_code == 302
    login(client, ids['admin'])
    assert client.post('/admin/start-tournament').status_code == 302
    assert client.post('/admin/run-matching').status_code == 302
    with application.app_context():
        match = a.Match.query.filter_by(tournament_id=current_id).one()
        assert match.roguelike == next_track
        match_id, player_a, player_b = match.id, match.player_a_id, match.player_b_id
    match_path = f'/tournament/match/{match_id}'
    login(client, player_a)
    assert client.post(match_path + '/schedule', data={'scheduled_time': '2030-03-04T14:00'}).status_code == 302
    login(client, player_b)
    assert client.post(match_path + '/confirm-schedule').status_code == 302
    for user, score in ((player_a, 100), (player_b, 90)):
        login(client, user)
        assert client.post(match_path + '/submit-result', data={'score': score, 'difficulty': 14}).status_code == 302
    login(client, ids['admin'])
    assert client.post(f'/admin/match/{match_id}/edit-result', data={'action': 'win_a'}).status_code == 302
    new_total = round(100 * (1 + 2 * next_step)) * next_bonus
    assert workbook(client)['队伍积分榜']['D2'].value == new_total
    assert str(new_total) in client.get('/tournament/rankings').get_data(as_text=True)
    assert '15000' in client.get(f'/history/{season_id}').get_data(as_text=True)


@pytest.mark.maintenance
def test_external_package_validates_and_recreates_configuration(application, client, tmp_path):
    season_id = configure_release(application)
    login(client, application.test_ids['super'])
    release_id, package = publish(application, client, season_id)
    login(client, application.test_ids['admin'])
    assert client.get(URL).status_code == 403
    assert client.get(f'{URL}/{release_id}/json').status_code == 403
    assert action(client, 'capture', season_id).status_code == 403

    root = tmp_path / 'destination'
    target = make_test_app(root, f'sqlite:///{root / "empty.db"}')
    with target.app_context():
        migrate_database(a.db.engine, target.config)
        initialize_database()
        target_season = a.Tournament.get_active()
        target_season.name = '隔离复用赛事'
        target_season_id = target_season.id
        target_user_id = a.User.query.filter_by(role='super_admin').one().id
        a.db.session.commit()
    receiver = target.test_client()
    login(receiver, target_user_id)

    def upload(manifest):
        return action(receiver, 'upload', target_season_id,
                      package=(BytesIO(json.dumps(manifest).encode()), 'configuration.json'))

    assert upload(dict(package, sha256='0' * 64)).status_code == 400
    invalid = json.loads(json.dumps(package['payload']))
    invalid['configuration']['rules'][0]['roguelike_key'] = 'missing'
    invalid['environment']['TRACK_LABEL'] = '不兼容术语'
    assert upload(_seal(invalid)).status_code == 302
    preview = receiver.get(URL).get_data(as_text=True)
    assert '引用缺失赛道：missing' in preview and '部署配置不一致：TRACK_LABEL' in preview
    assert upload(package).status_code == 302
    image = Path(target.config['IMAGE_DIR']) / '1.png'
    image.write_bytes(b'different')
    assert '素材校验值不同：image/1.png' in receiver.get(URL).get_data(as_text=True)
    assert action(receiver, 'apply', target_season_id, checksum=package['sha256']).status_code == 400
    image.unlink()
    assert '素材缺失' in receiver.get(URL).get_data(as_text=True)
    image.write_bytes(b'fixture-media')
    assert action(receiver, 'apply', target_season_id, checksum=package['sha256']).status_code == 409
    assert receiver.post('/admin/registration', data={'registration_open': 'closed'}).status_code == 302
    assert action(receiver, 'apply', target_season_id, checksum=package['sha256']).status_code == 302
    assert '自定义赛道' in receiver.get('/rules').get_data(as_text=True)
    assert '赛事封面' in receiver.get('/').get_data(as_text=True)
    assert receiver.get('/carousel-media/1.png').data == b'fixture-media'
    _, reproduced = publish(target, receiver, target_season_id)
    assert reproduced['payload']['configuration'] == package['payload']['configuration']
    assert reproduced['payload']['identity']['name'] == '隔离复用赛事'
    assert reproduced['payload']['source']['sha256'] == package['sha256']

    batch, = [path.parent for path in Path(target.config['BACKUP_DIR']).glob('*/manifest.json')]
    assert verify_backup(batch)['schema_version'] == SCHEMA_VERSION
    restored = create_engine(f'sqlite:///{root / "before-import.db"}')
    try:
        restore_backup(batch, restored, root / 'restored', target.config['APP_VERSION'])
        assert_schema_current(restored)
        with restored.connect() as connection:
            assert connection.exec_driver_sql('SELECT COUNT(*) FROM rules').scalar() == 0
            assert connection.exec_driver_sql('SELECT release_source FROM tournaments').scalar() is None
            assert connection.exec_driver_sql('SELECT version FROM season_releases').scalar() == 0
    finally:
        restored.dispose()


@pytest.mark.maintenance
def test_schema_four_upgrade_preserves_business_data(application):
    with application.app_context():
        engine = a.db.engine
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP TABLE season_releases')
            connection.exec_driver_sql('ALTER TABLE tournaments DROP COLUMN release_source')
            connection.exec_driver_sql('UPDATE schema_version SET version=4')
        batch = migrate_database(engine, application.config)
        manifest = verify_backup(batch)
        assert manifest['schema_version'] == 4 and 'season_releases' not in manifest['rows']
        assert_schema_current(engine)
        assert inspect(engine).has_table('season_releases')
        assert a.Match.query.count() == manifest['rows']['matches'] == 1
        assert a.Tournament.get_active().release_source is None
        assert migrate_database(engine, application.config) is None
