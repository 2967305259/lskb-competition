import json
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from app import create_app, db
from backup import _mysql_command
from migrations import SCHEMA_VERSION


def test_mysql_fields_preserve_password_and_use_matching_cli_endpoint(monkeypatch):
    password = 'isolated$@:#%+/password'
    monkeypatch.delenv('DATABASE_URL', raising=False)
    for key, value in {'DB_BACKEND': 'mysql', 'MYSQL_HOST': 'localhost', 'MYSQL_PORT': '23306',
                       'MYSQL_DATABASE': 'other_event', 'MYSQL_USER': 'other_user',
                       'MYSQL_PASSWORD': password}.items():
        monkeypatch.setenv(key, value)
    app = create_app('development', {'SQLALCHEMY_ENGINE_OPTIONS': {'connect_args': {'check_same_thread': True}}})
    with app.app_context():
        assert db.engine.url == URL.create('mysql+pymysql', username='other_user', password=password,
                                           host='localhost', port=23306, database='other_event',
                                           query={'charset': 'utf8mb4'})
        assert not app.config['SQLALCHEMY_ENGINE_OPTIONS']['connect_args']
        args, env = _mysql_command(db.engine, 'mysqldump')
        assert args[-5:] == ['--protocol=TCP', '--host', 'localhost', '--port', '23306']
        assert env['MYSQL_PWD'] == password and all(password not in arg for arg in args)
        assert db.engine.hide_parameters


def test_explicit_sqlite_url_overrides_mysql_and_invalid_backend_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / 'override.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{path}')
    monkeypatch.setenv('DB_BACKEND', 'mysql')
    monkeypatch.setenv('MYSQL_PORT', 'invalid')
    with create_app('development').app_context():
        assert db.engine.url.database == str(path)
        assert db.engine.dialect.name == 'sqlite'
    assert not path.exists()
    monkeypatch.delenv('DATABASE_URL')
    monkeypatch.setenv('DB_BACKEND', 'invalid')
    with pytest.raises(RuntimeError, match='DB_BACKEND'):
        create_app('development')
    with create_app('testing').app_context():
        assert db.engine.url.database == ':memory:'


def test_mysql_unix_socket_is_passed_to_maintenance_cli(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'mysql+pymysql://user:secret@localhost/db?unix_socket=%2Ftmp%2Fisolated.sock')
    with create_app('development').app_context():
        args, _ = _mysql_command(db.engine, 'mysql')
        assert args[-3:] == ['--protocol=SOCKET', '--socket', '/tmp/isolated.sock']
        assert '--port' not in args


def test_deployment_info_reports_actual_schema_without_credentials(application):
    result = application.test_cli_runner().invoke(args=['deployment-info'])
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    with application.app_context():
        assert info['backend'] == db.engine.dialect.name
        assert info['database'] == db.engine.url.database
        if db.engine.dialect.name == 'mysql':
            assert info['port'] == db.engine.url.port
    assert info['schema_version'] == SCHEMA_VERSION
    assert info['app_version'] == application.config['APP_VERSION']
    assert info['paths']['BACKUP_DIR'] == str(Path(application.config['BACKUP_DIR']).resolve())
    assert 'password' not in result.output
