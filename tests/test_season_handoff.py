from hashlib import sha256
from io import BytesIO
import re
import shutil

from openpyxl import load_workbook
import pytest
from sqlalchemy import create_engine, inspect

import app as a
from backup import restore_backup, verify_backup
from conftest import login
from migrations import SCHEMA_VERSION, assert_schema_current, migrate_database
from test_p0 import complete, transition_data
from test_season_releases import configure_release, publish


HANDOFF = '/admin/data-tools/handoff'


def package(client, season):
    response = client.get(f'{HANDOFF}/{season}?format=json')
    assert response.status_code == 200 and response.cache_control.no_store
    return response.get_json()


def test_handoff_access_and_concurrent_notes(application, client):
    ids = application.test_ids
    url = f"{HANDOFF}/{ids['season']}"
    edits = {'revision': '0', 'operation': 'notes', 'operations': '<script>private</script>'}
    for target in (HANDOFF, url, url + '?format=md', url + '?format=json', url + '/excel'):
        assert client.get(target).status_code == 302
    for operator in (ids['users'][0], ids['admin']):
        login(client, operator)
        for target in (HANDOFF, url, url + '?format=md', url + '?format=json', url + '/excel'):
            assert client.get(target).status_code == 403
        assert client.post(url, data=edits).status_code == 403
    login(client, ids['super'])
    assert client.get(HANDOFF).location == url
    assert client.post(url, data=edits).status_code == 302
    assert client.post(url, data={**edits, 'operations': '旧页面覆盖'}).status_code == 409
    assert client.post(url, data={**edits, 'revision': 1, 'operations': '过' * 2001}).status_code == 400
    html = client.get(url)
    assert html.cache_control.no_store
    assert b'&lt;script&gt;private&lt;/script&gt;' in html.data
    assert package(client, ids['season'])['notes']['operations'] == edits['operations']
    before = len(package(client, ids['season'])['exports']['excel_downloads'])
    assert client.head(url + '/excel').status_code == 200
    assert len(package(client, ids['season'])['exports']['excel_downloads']) == before
    assert client.get(url + '/excel').status_code == 200
    assert len(package(client, ids['season'])['exports']['excel_downloads']) == before + 1


@pytest.mark.maintenance
def test_next_operator_restores_and_creates_season(application, client, tmp_path):
    ids = application.test_ids
    season = configure_release(application)
    url = f'{HANDOFF}/{season}'
    application.config['DEPLOYMENT_IMAGE'] = 'lskb-handoff:test'
    with application.app_context():
        successor = a.User(username='next-operator', nickname='接手超管', role='super_admin')
        successor.set_password('isolated-successor-password')
        a.db.session.add(successor)
        a.db.session.commit()
        operator_id = successor.id

    complete(application, client)
    login(client, ids['admin'])
    assert client.post(f"/admin/team/{ids['teams'][0]}/update-scores", data={
        f"score_{ids['members'][0]}": '150', 'reason': '赛末人工复核',
    }).status_code == 302
    login(client, ids['super'])
    release_id, release = publish(application, client, season)
    assert client.post(url, data={'revision': 0, 'operation': 'notes',
        'operations': '核对截止名单', 'refereeing': '按归档赛道核分',
        'known_issues': '核对旧记录', 'temporary_config': '交接时检查轮播',
        'disputes': '工单 R21',}).status_code == 302
    assert client.post('/admin/data-tools/backup').status_code == 302

    login(client, operator_id)
    handoff = package(client, season)
    assert handoff['deployment'] == {'application_version': application.config['APP_VERSION'],
                                     'schema_version': SCHEMA_VERSION, 'image': 'lskb-handoff:test'}
    assert handoff['published_config']['sha256'] == release['sha256']
    assert handoff['published_config']['files']['json'].endswith(f'/{release_id}/json')
    assert handoff['rules']['titles'] == ['本届规则']
    assert handoff['adjustments'][0]['reason'] == '赛末人工复核'
    assert handoff['unfinished_matches'] == []
    markdown = client.get(url + '?format=md').get_data(as_text=True)
    assert '工单 R21' in markdown and '交接说明版本　1' in markdown
    backup, = handoff['backups']
    downloaded = tmp_path / 'downloaded-handoff' / backup['batch']
    downloaded.mkdir(parents=True)
    for name, download_url in backup['files'].items():
        response = client.get(download_url)
        assert response.status_code == 200 and response.cache_control.no_store
        (downloaded / name).write_bytes(response.data)
        response.close()
    assert verify_backup(downloaded)['rows']['season_handoffs'] == 1

    with application.app_context():
        source_url = a.db.engine.url
    database_admin = None
    if source_url.get_backend_name() == 'mysql':
        name = source_url.database + '_handoff'
        database_admin = create_engine(source_url.set(database=None))
        with database_admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE {name} CHARACTER SET utf8mb4')
        destination = create_engine(source_url.set(database=name))
    else:
        destination = create_engine(f"sqlite:///{tmp_path / 'restored-handoff.db'}")
    media = tmp_path / 'restored-handoff-media'
    try:
        restore_backup(downloaded, destination, media, backup['code_version'])
        restored = a.create_app('testing', dict(application.config, SQLALCHEMY_DATABASE_URI=destination.url,
                            UPLOAD_ROOT=str(media / 'uploads'), IMAGE_DIR=str(media / 'image'),
                            STATE_DIR=str(tmp_path / 'restored-state'), BACKUP_DIR=str(downloaded.parent)))
        try:
            restored_client = restored.test_client()
            login(restored_client, operator_id)
            recovered = package(restored_client, season)
            assert recovered['notes'] == handoff['notes']
            assert recovered['adjustments'] == handoff['adjustments']
            assert recovered['published_config'] == handoff['published_config']
            assert (media / 'uploads' / 'screenshots').is_dir()
            malformed = {'revision': recovered['revision'], 'operation': 'recovery', 'batch': 'missing',
                         'result': 'passed', 'reference': 'DR-21 SQLite/MySQL 隔离演练'}
            assert restored_client.post(url, data=malformed).status_code == 400
            assert restored_client.post(url, data={**malformed, 'batch': backup['batch']}).status_code == 302
            recorded = package(restored_client, season)['restores'][0]
            assert recorded['manifest_sha256'] == sha256((downloaded / 'manifest.json').read_bytes()).hexdigest()
            assert recorded['operator_id'] == operator_id and recorded['result'] == 'passed'
            assert recorded['batch_available'] and recorded['schema_version'] == SCHEMA_VERSION

            settings_page = restored_client.get('/admin/tournament-settings').get_data(as_text=True)
            token = re.search('name="idempotency_key" value="([^"]+)"', settings_page).group(1)
            transition = {'idempotency_key': token, 'expected_current_season_id': season,
                          'season_name': '接手运营下一届'}
            assert restored_client.post('/admin/new-season', data=transition).status_code == 302
            archived = package(restored_client, season)
            new_season = archived['next_season']['season_id']
            assert new_season != season and archived['transition']['new_season_id'] == new_season
            assert archived['season']['is_active'] is False
            assert any(item['code'] == 'no_advance_count' and not item['done'] for item in archived['next_season']['items'])
            assert restored_client.get(url).status_code == 200
            workbook_response = restored_client.get(archived['exports']['excel_url'])
            assert workbook_response.status_code == 200 and workbook_response.cache_control.no_store
            workbook = load_workbook(BytesIO(workbook_response.data))
            assert workbook['队伍积分榜']['H1'].value == 1
            assert workbook['队伍积分榜']['D2'].value == 15000
            assert workbook['比赛结果']['C2'].value == '自定义赛道'
            assert package(restored_client, season)['exports']['excel_downloads'][0]['operator_id'] == operator_id

            assert restored_client.post('/admin/data-tools/backup').status_code == 302
            next_backup, = package(restored_client, new_season)['backups']
            assert restored_client.post(url, data={**malformed, 'revision': archived['revision'],
                                                   'batch': next_backup['batch']}).status_code == 400
            assert restored_client.post('/admin/score-config', data={'advance_count': 2, 'bonus_score': 200}).status_code == 302
            assert all(item['done'] for item in package(restored_client, season)['next_season']['items']
                       if item['code'] == 'no_advance_count')
            shutil.rmtree(downloaded)
            assert package(restored_client, season)['restores'][0]['batch_available'] is False
            with restored.app_context():
                assert a.Tournament.get_active().id == new_season
                assert a.Tournament.get_active().name == '接手运营下一届'
                assert not a.Setting.get_instance().registration_open
                assert a.ScoreAdjustment.query.filter_by(tournament_id=season).count() == 1
        finally:
            with restored.app_context():
                a.db.engine.dispose()
        with application.app_context():
            assert a.Tournament.get_active().id == season
            assert a.SeasonHandoff.query.one().revision == 1
    finally:
        destination.dispose()
        if database_admin is not None:
            with database_admin.begin() as connection:
                connection.exec_driver_sql(f'DROP DATABASE {name}')
            database_admin.dispose()


@pytest.mark.maintenance
def test_schema_seven_upgrade_and_forced_archival_handoff(application, client):
    ids = application.test_ids
    with application.app_context():
        engine = a.db.engine
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP TABLE season_handoffs')
            connection.exec_driver_sql('UPDATE schema_version SET version=7')
        manifest = verify_backup(migrate_database(engine, application.config))
        assert manifest['schema_version'] == 7 and 'season_handoffs' not in manifest['rows']
        assert manifest['rows']['matches'] == 1
        assert_schema_current(engine)
        assert inspect(engine).has_table('season_handoffs')
        assert migrate_database(engine, application.config) is None

    transition = transition_data(application, client, force=True)
    assert client.post('/admin/new-season', data=transition).status_code == 302
    login(client, ids['super'])
    season = ids['season']
    handoff = package(client, season)
    assert handoff['transition']['unfinished_ids'] == [ids['match']]
    assert handoff['transition']['forced_reason'] == transition['force_reason']
    assert handoff['unfinished_matches'][0]['id'] == ids['match']
    assert handoff['unfinished_matches'][0]['settlement_state'] == 'pending'
    with application.app_context():
        with a.db.engine.begin() as connection:
            connection.exec_driver_sql('UPDATE tournaments SET is_active = FALSE')
            connection.execute(a.Tournament.__table__.update().where(a.Tournament.id == season).values(context_snapshot=None))
    handoff = package(client, season)
    assert handoff['next_season']['items'] is None
    assert handoff['reconciliation']['missing_rules_snapshot']
    assert handoff['exports']['excel_url'] is None
    assert client.get(f'{HANDOFF}/{season}/excel').status_code == 409
    assert client.post(f'{HANDOFF}/{season}', data={'revision': 0, 'operation': 'notes',
                                                   'known_issues': '核对历史规则'}).status_code == 302
    assert package(client, season)['notes']['known_issues'] == '核对历史规则'
