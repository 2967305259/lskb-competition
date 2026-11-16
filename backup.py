"""Consistent database + media backups. All app writes share the maintenance lock."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect, text

_mutex = threading.RLock()
_local = threading.local()


@contextlib.contextmanager
def maintenance_lock(config):
    """Serialize worker writes, migration, backup and season transition on this instance."""
    with _mutex:
        if getattr(_local, 'depth', 0):
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        path = Path(config['STATE_DIR']) / 'maintenance.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _local.depth = 1
            try:
                yield
            finally:
                _local.depth = 0
                fcntl.flock(handle, fcntl.LOCK_UN)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _mysql_command(engine, executable):
    url = engine.url
    env = dict(os.environ, MYSQL_PWD=url.password or '')
    args = [executable, '--host', url.host or 'localhost', '--port', str(url.port or 3306),
            '--user', url.username or '', '--default-character-set=utf8mb4']
    return args, env


def verify_media_references(engine, config):
    """A directory existing is insufficient when database attachments are missing."""
    uploads = Path(config['UPLOAD_ROOT']).resolve()
    image = Path(config['IMAGE_DIR']).resolve()
    expected = []
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        for table, column, folder in (('users', 'avatar', 'avatars'), ('teams', 'team_photo', 'teams')):
            if table in tables and column in {c['name'] for c in inspect(conn).get_columns(table)}:
                expected.extend((uploads, Path(folder) / row[0]) for row in conn.execute(text(
                    f'SELECT {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != :empty'), {'empty': ''}))
        if 'matches' in tables and 'match_note' in {c['name'] for c in inspect(conn).get_columns('matches')}:
            for row in conn.execute(text('SELECT match_note FROM matches WHERE match_note IS NOT NULL')):
                expected.extend((uploads, Path('screenshots') / name) for name in re.findall(
                    r'/static/uploads/screenshots/([A-Za-z0-9_.-]+)', row[0]))
        if 'carousel_items' in tables:
            for row in conn.execute(text('SELECT file_path FROM carousel_items')):
                path = row[0] or ''
                if path.startswith('/static/uploads/'):
                    expected.append((uploads, Path(path.removeprefix('/static/uploads/'))))
                elif path.startswith('/carousel-media/'):
                    expected.append((image, Path(path.removeprefix('/carousel-media/'))))
                elif path.startswith('image/'):
                    expected.append((image, Path(path.removeprefix('image/'))))
    for root, relative in expected:
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise RuntimeError('素材引用缺失，备份不完整: ' + str(relative))


def create_backup(engine, config, reason='manual'):
    """Returns only a verified complete batch. Exceptions leave no successful manifest."""
    with maintenance_lock(config):
        verify_media_references(engine, config)
        root = Path(config['BACKUP_DIR']).resolve()
        root.mkdir(parents=True, exist_ok=True)
        batch = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]
        stage = Path(tempfile.mkdtemp(prefix='.partial-', dir=root))
        try:
            dialect = engine.dialect.name
            db_file = stage / ('database.db' if dialect == 'sqlite' else 'database.sql')
            if dialect == 'sqlite':
                if not engine.url.database or engine.url.database == ':memory:':
                    raw = engine.raw_connection()
                    try:
                        with sqlite3.connect(db_file) as out:
                            raw.driver_connection.backup(out)
                    finally:
                        raw.close()
                else:
                    with sqlite3.connect(engine.url.database) as source, sqlite3.connect(db_file) as out:
                        source.backup(out)
                with sqlite3.connect(db_file) as check:
                    if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('SQLite 备份完整性校验失败')
            elif dialect == 'mysql':
                args, env = _mysql_command(engine, config.get('MYSQLDUMP_COMMAND', 'mysqldump'))
                with db_file.open('wb') as stream:
                    result = subprocess.run(args + ['--single-transaction', '--routines', '--triggers',
                                                   '--hex-blob', url_database(engine)],
                                            env=env, stdout=stream, stderr=subprocess.PIPE, timeout=120)
                if result.returncode:
                    raise RuntimeError('MySQL 备份失败，请检查 dump 工具及数据库权限')
            else:
                raise RuntimeError('备份仅支持 SQLite / MySQL')
            if not db_file.stat().st_size:
                raise RuntimeError('数据库备份为空')
            paths = {'uploads': Path(config['UPLOAD_ROOT']), 'image': Path(config['IMAGE_DIR'])}
            for name, path in paths.items():
                if not path.is_dir():
                    raise RuntimeError(f'素材目录缺失，备份不完整: {name}')
                if any(p.is_symlink() for p in path.rglob('*')):
                    raise RuntimeError(f'素材目录包含符号链接，请先核对: {name}')
            archive = stage / 'media.tar.gz'
            with tarfile.open(archive, 'w:gz') as out:
                for name, path in paths.items():
                    out.add(path, arcname=name)
            with engine.connect() as connection:
                inspector = inspect(connection)
                tables = inspector.get_table_names()
                quote = engine.dialect.identifier_preparer.quote
                counts = {table: connection.execute(text(f'SELECT COUNT(*) FROM {quote(table)}')).scalar()
                          for table in tables}
                schema = connection.execute(text('SELECT MAX(version) FROM schema_version')).scalar() if 'schema_version' in tables else 0
                season_number = None
                if 'tournaments' in tables and {'is_active', 'season_number'} <= {
                    column['name'] for column in inspector.get_columns('tournaments')
                }:
                    active = connection.execute(text(
                        'SELECT season_number FROM tournaments WHERE is_active = true LIMIT 2'
                    )).scalars().all()
                    if len(active) == 1:
                        season_number = active[0]
            manifest = {'batch': batch, 'complete': True, 'created_at': datetime.now(timezone.utc).isoformat(),
                        'timezone': 'UTC', 'reason': reason, 'database_type': dialect, 'season_number': season_number,
                        'schema_version': schema or 0, 'code_version': config.get('APP_VERSION', 'unknown') if schema else config.get('LEGACY_APP_VERSION', 'unknown'),
                        'source_identity': hashlib.sha256(str(engine.url.set(password=None)).encode()).hexdigest(),
                        'media_roots': list(paths), 'rows': counts,
                        'files': {p.name: {'sha256': digest(p), 'bytes': p.stat().st_size} for p in (db_file, archive)}}
            (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            verify_backup(stage)
            final = root / batch
            stage.rename(final)
            # Retention is explicit; callers prune only after the protected operation succeeds.
            return final
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise


def url_database(engine):
    if not engine.url.database:
        raise RuntimeError('缺少数据库名称')
    return engine.url.database


def verify_backup(batch):
    batch = Path(batch)
    manifest = json.loads((batch / 'manifest.json').read_text())
    files = manifest.get('files') if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or manifest.get('complete') is not True or set(files) not in (
            {'database.db', 'media.tar.gz'}, {'database.sql', 'media.tar.gz'}):
        raise RuntimeError('备份清单不完整')
    for name, expected in files.items():
        path = batch / name
        if (not isinstance(expected, dict) or type(expected.get('bytes')) is not int
                or expected['bytes'] <= 0 or path.is_symlink() or not path.is_file()
                or path.stat().st_size != expected['bytes'] or digest(path) != expected.get('sha256')):
            raise RuntimeError('备份校验失败: ' + name)
    return manifest


def prune_backups(config):
    root = Path(config['BACKUP_DIR'])
    batches = []
    for path in root.iterdir():
        if path.is_dir() and not path.is_symlink() and not path.name.startswith('.'):
            try:
                verify_backup(path)
                batches.append(path)
            except (OSError, ValueError, KeyError, RuntimeError):
                continue
    for path in sorted(batches, reverse=True)[max(1, int(config.get('BACKUP_KEEP', 5))):]:
        shutil.rmtree(path)


def restore_backup(batch, destination_engine, media_destination, expected_code_version):
    """Restore rehearsal only: destination DB and media directory must be empty."""
    manifest = verify_backup(batch)
    if manifest['code_version'] != expected_code_version:
        raise RuntimeError('代码版本不匹配，须先验证跨版本恢复路径')
    identity = hashlib.sha256(str(destination_engine.url.set(password=None)).encode()).hexdigest()
    if identity == manifest['source_identity']:
        raise RuntimeError('禁止恢复到源数据库')
    target = Path(media_destination).resolve()
    if target.exists() and any(target.iterdir()):
        raise RuntimeError('恢复素材目标必须为空')
    if inspect(destination_engine).get_table_names():
        raise RuntimeError('恢复目标数据库必须为空')
    batch = Path(batch)
    # Validate the entire archive before mutating the destination.
    with tarfile.open(batch / 'media.tar.gz', 'r:gz') as archive:
        for item in archive.getmembers():
            path = (target / item.name).resolve()
            if not path.is_relative_to(target) or item.issym() or item.islnk() or not (item.isfile() or item.isdir()):
                raise RuntimeError('素材包含不安全路径')
        if manifest['database_type'] != destination_engine.dialect.name:
            raise RuntimeError('备份和恢复数据库类型不一致')
        if manifest['database_type'] == 'sqlite':
            destination_engine.dispose()
            with sqlite3.connect(batch / 'database.db') as source, sqlite3.connect(destination_engine.url.database) as out:
                source.backup(out)
        else:
            args, env = _mysql_command(destination_engine, 'mysql')
            with (batch / 'database.sql').open('rb') as stream:
                result = subprocess.run(args + [url_database(destination_engine)], env=env,
                                        stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if result.returncode:
                raise RuntimeError('MySQL 恢复失败；源数据库未修改，目标需重新创建')
        target.mkdir(parents=True, exist_ok=True)
        archive.extractall(target, filter='data')
    with destination_engine.connect() as conn:
        quote = destination_engine.dialect.identifier_preparer.quote
        for table, count in manifest['rows'].items():
            if conn.execute(text(f'SELECT COUNT(*) FROM {quote(table)}')).scalar() != count:
                raise RuntimeError('恢复行数不一致: ' + table)
    return manifest
