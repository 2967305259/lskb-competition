from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text
from app import create_app, db
from migrations import migrate_database, assert_schema_current
from backup import verify_backup


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
        assert_schema_current(legacy_engine)
        with legacy_engine.connect() as conn:
            assert conn.execute(text('SELECT tournament_id FROM teams WHERE id=2')).scalar() is None
            row = conn.execute(text('SELECT player_a_score,tournament_id,scoring_rule,settlement_state FROM matches WHERE id=1')).one()
            assert tuple(row) == (100, None, None, 'legacy')
            assert conn.execute(text('SELECT COUNT(*) FROM score_logs')).scalar() == 1
        assert str(next(c for c in inspect(legacy_engine).get_columns('score_logs') if c['name']=='old_result')['type']).upper() in ('TEXT', 'LONGTEXT')
        assert migrate_database(legacy_engine, application.config) is None


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
