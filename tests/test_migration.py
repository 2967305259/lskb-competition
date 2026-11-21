from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text
from app import create_app, db
from migrations import SCHEMA_VERSION, migrate_database, assert_schema_current
from backup import verify_backup
from conftest import login


pytestmark = pytest.mark.maintenance


def make_legacy(engine, dialect):
    sql = (Path(__file__).parent / 'fixtures' / f'pre_p0_{dialect}.sql').read_text()
    sql = '\n'.join(line for line in sql.splitlines() if not line.startswith('--'))
    # Exercise the supported older, globally unique team structure as well as the exact baseline.
    sql = sql.replace('CONSTRAINT uq_tournament_team_name UNIQUE (tournament_id, team_name)', 'CONSTRAINT old_team_name UNIQUE (team_name)')
    sql = sql.replace('CONSTRAINT uq_tournament_captain UNIQUE (tournament_id, captain_id)', 'CONSTRAINT old_captain UNIQUE (captain_id)')
    if dialect == 'sqlite':
        raw = engine.raw_connection()
        raw.driver_connection.executescript(sql)
        raw.close()
    else:
        with engine.begin() as conn:
            for statement in sql.split(';'):
                if statement.strip():
                    conn.exec_driver_sql(statement)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tournaments (id,name,season_number,is_active,bonus_score) VALUES (1,'legacy',3,1,500)"))
        conn.execute(text("INSERT INTO users (id, username,nickname,password_hash,role) VALUES (1,'a','a','fixture','super_admin'),(2,'b','b','fixture','player')"))
        conn.execute(text("INSERT INTO teams (id,team_name,captain_id,tournament_id,team_score) VALUES (1,'one',1,1,60000),(2,'two',2,NULL,54000)"))
        conn.execute(text("INSERT INTO team_members (id,team_id,user_id,final_score) VALUES (1,1,1,120),(2,2,2,108)"))
        conn.execute(text("INSERT INTO matches (id,tournament_id,player_a_id,player_b_id,team_a_id,team_b_id,roguelike,status,player_a_score,player_b_score,difficulty) VALUES (1,NULL,1,2,1,2,'water','finished',100,90,14)"))
        conn.execute(text("INSERT INTO score_logs (id,match_id,modified_by,old_result,new_result) VALUES (1,1,1,'old','new')"))
    add_legacy_notification(engine, user_id=1)


def add_legacy_notification(engine, user_id):
    with engine.begin() as conn:
        conn.exec_driver_sql("""
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title VARCHAR(100), message TEXT,
                `read` BOOLEAN, created_at DATETIME, FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        conn.execute(text("INSERT INTO notifications (id, user_id, title) VALUES (1, :user_id, '旧比赛通知')"),
                     {'user_id': user_id})


@pytest.fixture
def legacy_engine(application, tmp_path):
    with application.app_context():
        from sqlalchemy.engine import make_url
        source = db.engine
        dialect = source.dialect.name
        if dialect == 'sqlite':
            engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
            admin = None
        else:
            name = source.url.database + '_legacy'
            admin = create_engine(source.url.set(database=None))
            with admin.begin() as conn:
                conn.execute(text('CREATE DATABASE ' + name))
            engine = create_engine(source.url.set(database=name))
        make_legacy(engine, dialect)
    yield engine
    engine.dispose()
    if admin:
        with admin.begin() as conn:
            conn.execute(text('DROP DATABASE ' + name))
        admin.dispose()


def test_real_legacy_upgrade_preserves_unknowns_and_is_idempotent(application, legacy_engine):
    with application.app_context():
        batch = migrate_database(legacy_engine, application.config)
        manifest = verify_backup(batch)
        assert manifest['schema_version'] == 0 and manifest['code_version'] == '8238f20'
        assert manifest['rows']['notifications'] == 1
        assert not inspect(legacy_engine).has_table('notifications')
        assert_schema_current(legacy_engine)
        with legacy_engine.connect() as conn:
            assert conn.execute(text('SELECT tournament_id FROM teams WHERE id=2')).scalar() is None
            row = conn.execute(text('SELECT player_a_score,tournament_id,scoring_rule,settlement_state FROM matches WHERE id=1')).one()
            assert tuple(row) == (100, None, None, 'legacy')
            assert conn.execute(text('SELECT COUNT(*) FROM score_logs')).scalar() == 1
        assert str(next(c for c in inspect(legacy_engine).get_columns('score_logs') if c['name']=='old_result')['type']).upper() in ('TEXT', 'LONGTEXT')
        assert migrate_database(legacy_engine, application.config) is None


def test_prepare_v2_removes_legacy_notifications_after_backup(application):
    user_id = application.test_ids['users'][0]
    with application.app_context():
        engine = db.engine
        add_legacy_notification(engine, user_id=user_id)
        with engine.begin() as conn:
            conn.exec_driver_sql('UPDATE schema_version SET version=2')

    runner = application.test_cli_runner()
    result = runner.invoke(args=['prepare'])
    assert result.exit_code == 0, result.output
    assert f'schema={SCHEMA_VERSION}' in result.output
    assert_schema_current(engine)
    assert not inspect(engine).has_table('notifications')

    backups = Path(application.config['BACKUP_DIR'])
    batch, = [path.parent for path in backups.glob('*/manifest.json')]
    manifest = verify_backup(batch)
    assert manifest['schema_version'] == 2 and manifest['rows']['notifications'] == 1
    if engine.dialect.name == 'sqlite':
        with sqlite3.connect(batch / 'database.db') as saved:
            assert saved.execute('SELECT title FROM notifications').fetchone() == ('旧比赛通知',)
    else:
        assert f"(1,{user_id},'旧比赛通知'" in (batch / 'database.sql').read_text()
    with engine.connect() as conn:
        for table in ('users', 'tournaments', 'teams', 'team_members', 'matches', 'score_logs', 'system_logs'):
            assert conn.exec_driver_sql(f'SELECT COUNT(*) FROM {table}').scalar() == manifest['rows'][table]
        assert conn.exec_driver_sql('SELECT MAX(version) FROM schema_version').scalar() == SCHEMA_VERSION

    result = runner.invoke(args=['prepare'])
    assert result.exit_code == 0, result.output
    assert list(backups.glob('*/manifest.json')) == [batch / 'manifest.json']


def test_prepare_v3_backs_up_retired_fields_and_preserves_live_phase(application, client):
    with application.app_context():
        engine = db.engine
    with engine.begin() as conn:
        conn.exec_driver_sql('UPDATE schema_version SET version = 3')
        conn.exec_driver_sql('ALTER TABLE settings ADD COLUMN tournament_status VARCHAR(20)')
        conn.exec_driver_sql('ALTER TABLE users ADD COLUMN approval_status VARCHAR(20)')
        conn.exec_driver_sql("UPDATE settings SET tournament_status = 'finished', tournament_name = '旧设置名称'")
        conn.exec_driver_sql("UPDATE users SET approval_status = 'pending'")
        conn.exec_driver_sql("UPDATE tournaments SET status = 'running', name = '当前赛事名称' WHERE is_active")
        conn.exec_driver_sql('CREATE TABLE announcements (id INTEGER PRIMARY KEY, title VARCHAR(100))')
        conn.exec_driver_sql("INSERT INTO announcements (id, title) VALUES (1, '旧公告样本')")

    runner = application.test_cli_runner()
    result = runner.invoke(args=['prepare'])
    assert result.exit_code == 0, result.output
    assert_schema_current(engine)
    schema = inspect(engine)
    assert 'tournament_status' not in {col['name'] for col in schema.get_columns('settings')}
    assert 'approval_status' not in {col['name'] for col in schema.get_columns('users')}
    assert not schema.has_table('announcements')
    backup_dir = Path(application.config['BACKUP_DIR'])
    batch, = [path.parent for path in backup_dir.glob('*/manifest.json')]
    manifest = verify_backup(batch)
    assert manifest['schema_version'] == 3 and manifest['rows']['announcements'] == 1
    if engine.dialect.name == 'sqlite':
        with sqlite3.connect(batch / 'database.db') as saved:
            assert saved.execute('SELECT tournament_status FROM settings').fetchone() == ('finished',)
            assert saved.execute('SELECT DISTINCT approval_status FROM users').fetchall() == [('pending',)]
    else:
        dump = (batch / 'database.sql').read_text()
        assert '`tournament_status`' in dump and '`approval_status`' in dump
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT tournament_name FROM settings').scalar() == '旧设置名称'
        for table in ('users', 'tournaments', 'teams', 'team_members', 'matches', 'score_logs', 'system_logs'):
            assert conn.exec_driver_sql(f'SELECT COUNT(*) FROM {table}').scalar() == manifest['rows'][table]

    response = client.get('/')
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '当前赛事名称' in page and '旧设置名称' not in page
    login(client, application.test_ids['admin'])
    dashboard = client.get('/admin/dashboard').get_data(as_text=True)
    assert 'href="/admin/registration"' in dashboard and '比赛进行中' in dashboard
    assert runner.invoke(args=['prepare']).exit_code == 0
    assert list(backup_dir.glob('*/manifest.json')) == [batch / 'manifest.json']


def test_failure_stops_migration_and_sqlite_rolls_back_ddl(application, legacy_engine, monkeypatch):
    import migrations
    with application.app_context():
        if legacy_engine.dialect.name == 'sqlite':
            original = migrations._rebuild_sqlite
            def fail_after_ddl(*args):
                original(*args)
                raise RuntimeError('injected after table rebuild')
            monkeypatch.setattr(migrations, '_rebuild_sqlite', fail_after_ddl)
        else:
            monkeypatch.setattr(migrations, 'validate_schema', lambda *args: (_ for _ in ()).throw(RuntimeError('injected validation failure')))
        with pytest.raises(RuntimeError, match='迁移失败'):
            migrate_database(legacy_engine, application.config)
        with pytest.raises(RuntimeError):
            assert_schema_current(legacy_engine)
        if legacy_engine.dialect.name == 'sqlite':
            assert 'scoring_rule' not in {c['name'] for c in inspect(legacy_engine).get_columns('matches')}
        with legacy_engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM score_logs')).scalar() == 1
            assert conn.execute(text('SELECT player_a_score FROM matches')).scalar() == 100
        assert list(Path(application.config['BACKUP_DIR']).glob('*/manifest.json'))


def test_concurrent_prepare_has_one_version(application, legacy_engine):
    def run(_):
        with application.app_context():
            migrate_database(legacy_engine, application.config)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, range(4)))
    with legacy_engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM schema_version')).scalar() == 1
