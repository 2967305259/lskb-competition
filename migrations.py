"""Explicit forward schema migration; never called while importing or serving requests."""
from datetime import datetime
from pathlib import Path

from sqlalchemy import MetaData, Table, inspect, text
from backup import create_backup, maintenance_lock

SCHEMA_VERSION = 4
# These additions cover the supported pre-P0 schema. Other missing base columns are an error.
ADDITIONS = {
    'users': {'has_completed_onboarding': '0', 'tutorial_step': '0'},
    'teams': {'tournament_id': 'NULL'},
    'team_members': {'position': 'NULL', 'squad': "''", 'operator': "''"},
    'settings': {'team_size': 'NULL', 'allow_incomplete_teams': '0'},
    'tournaments': {'context_snapshot': 'NULL', 'snapshot_version': '0', 'archived_at': 'NULL',
                    'window_times_utc': '0'},
    'matches': {'tournament_id': 'NULL', 'player_a_difficulty': 'NULL', 'player_b_difficulty': 'NULL',
                'match_type': "'match'", 'parent_id': 'NULL', 'segment_order': '0',
                'player_a_multiplier': 'NULL', 'player_b_multiplier': 'NULL', 'scoring_rule': 'NULL', 'legacy_payload': 'NULL', 'context_version': 'NULL',
                'settlement_state': "'legacy'", 'settled_at': 'NULL', 'forfeit_side': 'NULL',
                'forfeit_reason': "''", 'score_origin_a': 'NULL', 'score_origin_b': 'NULL',
                'scheduled_time_utc': '0'},
}


def assert_schema_current(engine):
    with engine.connect() as conn:
        if 'schema_version' not in inspect(conn).get_table_names():
            raise RuntimeError('数据库未准备，请先运行 flask --app app:create_app prepare')
        version = conn.execute(text('SELECT MAX(version) FROM schema_version')).scalar()
        if version != SCHEMA_VERSION:
            raise RuntimeError('数据库版本不匹配，须先在备份副本验证迁移')
        validate_schema(conn)


def validate_schema(conn):
    from app import db
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    for table in db.metadata.sorted_tables:
        if table.name not in tables:
            raise RuntimeError(f'缺少表: {table.name}')
        names = {c['name'] for c in inspector.get_columns(table.name)}
        missing = set(table.columns.keys()) - names
        if missing:
            raise RuntimeError(f'缺少列: {table.name}: {sorted(missing)}')
        actual_foreign_keys = {(tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']))
                               for f in inspector.get_foreign_keys(table.name)}
        for constraint in table.foreign_key_constraints:
            expected = (tuple(e.parent.name for e in constraint.elements),
                        next(iter(constraint.elements)).column.table.name,
                        tuple(e.column.name for e in constraint.elements))
            if expected not in actual_foreign_keys:
                raise RuntimeError(f'缺少外键: {table.name} {expected}')
    constraints = {tuple(c['column_names']) for c in inspector.get_unique_constraints('teams')}
    for columns in (('tournament_id', 'team_name'), ('tournament_id', 'captain_id')):
        if columns not in constraints:
            raise RuntimeError(f'缺少联合唯一约束: teams {columns}')


def _rebuild_sqlite(conn, name):
    from app import db
    # Copy the current model columns while rebuilding legacy constraints.
    old = db.metadata.tables[name]
    staging_metadata = MetaData()
    for table in db.metadata.tables.values():
        table.to_metadata(staging_metadata)
    staging = old.to_metadata(staging_metadata, name=name + '_p0_new')
    # Index names are global in SQLite; original indexes remain until the old table is dropped.
    staging.indexes.clear()
    staging.create(conn)
    quote = conn.dialect.identifier_preparer.quote
    columns = ', '.join(quote(c.name) for c in old.columns)
    conn.exec_driver_sql(f'INSERT INTO {quote(staging.name)} ({columns}) SELECT {columns} FROM {quote(name)}')
    conn.exec_driver_sql(f'DROP TABLE {quote(name)}')
    conn.exec_driver_sql(f'ALTER TABLE {quote(staging.name)} RENAME TO {quote(name)}')
    for index in old.indexes:
        index.create(conn)


def migrate_database(engine, config, *, prepare_sqlite_directory=None):
    from app import db
    with maintenance_lock(config):
        # Only these explicit preparation commands create filesystem paths.
        if engine.dialect.name == 'sqlite' and engine.url.database not in (None, '', ':memory:'):
            directory = Path(engine.url.database).parent
            if prepare_sqlite_directory is None:
                directory.mkdir(parents=True, exist_ok=True)
            else:
                prepare_sqlite_directory(directory)
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            current = 0
            if 'schema_version' in tables:
                current = conn.execute(text('SELECT MAX(version) FROM schema_version')).scalar() or 0
                if current == SCHEMA_VERSION:
                    validate_schema(conn)
                    return None
                if current and current > SCHEMA_VERSION:
                    raise RuntimeError('数据库版本较新，禁止用旧代码迁移')
            # Recognize old shape before performing any mutations.
            for table in db.metadata.tables.values():
                if table.name not in tables:
                    continue
                actual = {c['name'] for c in inspect(conn).get_columns(table.name)}
                missing = set(table.columns.keys()) - actual - set(ADDITIONS.get(table.name, {}))
                if missing:
                    raise RuntimeError(f'不支持的旧结构 {table.name}: 缺少 {sorted(missing)}；请先核对')
        batch = create_backup(engine, config, 'schema-upgrade') if tables else None
        with engine.connect() as conn:
            mysql_lock = False
            try:
                if engine.dialect.name == 'sqlite':
                    conn.exec_driver_sql('PRAGMA foreign_keys=OFF')
                    conn.commit()
                    conn.exec_driver_sql('BEGIN IMMEDIATE')
                elif engine.dialect.name == 'mysql':
                    mysql_lock = conn.execute(text("SELECT GET_LOCK('lskb_schema_upgrade', 30)")).scalar() == 1
                    if not mysql_lock:
                        raise RuntimeError('无法取得数据库迁移锁')
                else:
                    raise RuntimeError('迁移仅支持 SQLite / MySQL')
                # MySQL DDL may commit implicitly: no success version is written until all checks pass.
                # A failed MySQL DDL upgrade must be recovered from the verified batch, not hidden.
                for table, additions in ADDITIONS.items():
                    if table not in tables:
                        continue
                    existing = {c['name'] for c in inspect(conn).get_columns(table)}
                    for name, default in additions.items():
                        if name in existing:
                            continue
                        column = db.metadata.tables[table].columns[name]
                        sql_type = column.type.compile(dialect=engine.dialect)
                        nullable = '' if column.nullable else ' NOT NULL'
                        # MySQL rejects literal defaults on TEXT (even though SQLite accepts them).
                        default_clause = f' DEFAULT {default}'
                        if engine.dialect.name == 'mysql' and str(sql_type).upper() in ('TEXT', 'LONGTEXT') and default != 'NULL':
                            default_clause = ''
                        conn.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {name} {sql_type}{nullable}{default_clause}')
                        if not default_clause:
                            conn.exec_driver_sql(f'UPDATE {table} SET {name}={default} WHERE {name} IS NULL')
                if current == 0:
                    db.metadata.create_all(bind=conn)
                    if engine.dialect.name == 'sqlite':
                        constraints = {tuple(c['column_names']) for c in inspect(conn).get_unique_constraints('teams')}
                        if ('tournament_id', 'captain_id') not in constraints or ('tournament_id', 'team_name') not in constraints:
                            _rebuild_sqlite(conn, 'teams')
                        logs = inspect(conn).get_columns('score_logs')
                        if any(c['name'] in ('old_result', 'new_result') and str(c['type']).upper() != 'TEXT' for c in logs):
                            _rebuild_sqlite(conn, 'score_logs')
                    else:
                        unique = inspect(conn).get_unique_constraints('teams')
                        quote = conn.dialect.identifier_preparer.quote
                        for item in unique:
                            if tuple(item['column_names']) in (('captain_id',), ('team_name',)):
                                if item['column_names'] == ['captain_id']:
                                    indexes = {i['name'] for i in inspect(conn).get_indexes('teams')}
                                    if 'ix_teams_captain_reference' not in indexes:
                                        conn.exec_driver_sql('CREATE INDEX ix_teams_captain_reference ON teams (captain_id)')
                                conn.exec_driver_sql('ALTER TABLE teams DROP INDEX ' + quote(item['name']))
                        unique = {tuple(c['column_names']) for c in inspect(conn).get_unique_constraints('teams')}
                        for columns, name in ((('tournament_id', 'team_name'), 'uq_tournament_team_name'),
                                              (('tournament_id', 'captain_id'), 'uq_tournament_captain')):
                            if columns not in unique:
                                conn.exec_driver_sql(f'ALTER TABLE teams ADD CONSTRAINT {name} UNIQUE ({", ".join(columns)})')
                        conn.exec_driver_sql('ALTER TABLE score_logs MODIFY old_result LONGTEXT NULL, MODIFY new_result LONGTEXT NULL')
                    if engine.dialect.name == 'mysql':
                        conn.exec_driver_sql('ALTER TABLE system_logs MODIFY detail LONGTEXT NULL')
                    # Complete foreign keys for columns added to old SQLite/MySQL structures.
                    for table in db.metadata.sorted_tables:
                        actual_fk = {(tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']))
                                     for f in inspect(conn).get_foreign_keys(table.name)}
                        missing_fk = []
                        for constraint in table.foreign_key_constraints:
                            expected = (tuple(e.parent.name for e in constraint.elements),
                                        next(iter(constraint.elements)).column.table.name,
                                        tuple(e.column.name for e in constraint.elements))
                            if expected not in actual_fk:
                                missing_fk.append(expected)
                        if missing_fk and engine.dialect.name == 'sqlite':
                            _rebuild_sqlite(conn, table.name)
                        elif missing_fk:
                            quote = conn.dialect.identifier_preparer.quote
                            for columns, target, target_cols in missing_fk:
                                name = 'fk_p0_' + table.name + '_' + '_'.join(columns)
                                conn.exec_driver_sql(f'ALTER TABLE {quote(table.name)} ADD CONSTRAINT {quote(name)} FOREIGN KEY ({", ".join(map(quote, columns))}) REFERENCES {quote(target)} ({", ".join(map(quote, target_cols))})')
                    season_unique = {tuple(c['column_names']) for c in inspect(conn).get_unique_constraints('tournaments')}
                    season_indexes = {tuple(i['column_names']) for i in inspect(conn).get_indexes('tournaments') if i.get('unique')}
                    if ('season_number',) not in season_unique | season_indexes:
                        conn.exec_driver_sql('CREATE UNIQUE INDEX uq_tournament_season_number ON tournaments (season_number)')
                    conn.exec_driver_sql("UPDATE matches SET settlement_state='pending' WHERE settlement_state='legacy' AND player_a_score IS NULL AND player_b_score IS NULL AND player_a_result IS NULL AND player_b_result IS NULL AND status IN ('pending', 'scheduled', 'confirmed')")
                # Older versioned databases can still carry retired account/announcement data.
                for table in ('announcements', 'notifications'):
                    if table in tables:
                        conn.exec_driver_sql(f'DROP TABLE {table}')
                for table, column in (('users', 'approval_status'), ('settings', 'tournament_status')):
                    if table in tables and column in {c['name'] for c in inspect(conn).get_columns(table)}:
                        conn.exec_driver_sql(f'ALTER TABLE {table} DROP COLUMN {column}')
                conn.exec_driver_sql('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)')
                validate_schema(conn)
                if engine.dialect.name == 'sqlite':
                    violations = conn.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
                    if violations:
                        raise RuntimeError(f'外键核对失败（需人工修复）: {violations[:10]}')
                conn.execute(text('INSERT INTO schema_version (version, applied_at) VALUES (:v, :at)'),
                             {'v': SCHEMA_VERSION, 'at': datetime.utcnow().isoformat()})
                conn.commit()
            except Exception as error:
                conn.rollback()
                raise RuntimeError(f'迁移失败，未记录成功版本；备份批次: {batch.name if batch else "空库"}；{error}') from error
            finally:
                if mysql_lock:
                    conn.execute(text("SELECT RELEASE_LOCK('lskb_schema_upgrade')"))
                    conn.commit()
                if engine.dialect.name == 'sqlite':
                    conn.exec_driver_sql('PRAGMA foreign_keys=ON')
                    conn.commit()
        return batch
