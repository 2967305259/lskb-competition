from copy import deepcopy
import os
from pathlib import Path
from shutil import copyfile
import sys
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as models
from app import create_app, db
from management import initialize_database
from migrations import migrate_database


def make_test_app(root, uri):
    config = {'SQLALCHEMY_DATABASE_URI': uri, 'STATE_DIR': str(root / 'state'),
              'BACKUP_DIR': str(root / 'backups'), 'UPLOAD_ROOT': str(root / 'uploads'),
              'IMAGE_DIR': str(root / 'image'), 'SQLALCHEMY_ECHO': False,
              'SECRET_KEY': 'isolated-test-key', 'ADMIN_USERNAME': 'admin',
              'ADMIN_PASSWORD': 'isolated-admin-password', 'APP_VERSION': 'p0-v1',
              'SUPER_ADMIN_USERNAME': 'super', 'SUPER_ADMIN_PASSWORD': 'isolated-password'}
    if make_url(uri).get_backend_name() == 'mysql' and (command := os.environ.get('P0_MYSQLDUMP')):
        config['MYSQLDUMP_COMMAND'] = command
    for path in [root / 'image'] + [root / 'uploads' / name for name in ('avatars', 'teams', 'screenshots')]:
        path.mkdir(parents=True)
    (root / 'image' / '1.png').write_bytes(b'fixture-media')
    return create_app('testing', config)


def seed_test_database(app):
    with app.app_context():
        migrate_database(db.engine, app.config)
        initialize_database()
        season = models.Tournament.get_active()
        users = []
        teams = []
        members = []
        for name in ('alpha', 'beta'):
            user = models.User(username=name, nickname=name, role='captain')
            user.set_password('test-password')
            db.session.add(user)
            db.session.flush()
            team = models.Team(team_name=name, captain_id=user.id, tournament_id=season.id)
            db.session.add(team)
            db.session.flush()
            member = models.TeamMember(team_id=team.id, user_id=user.id, roguelike='water')
            db.session.add(member)
            users.append(user.id)
            teams.append(team.id)
            members.append(member)
        match = models.Match(tournament_id=season.id, player_a_id=users[0], player_b_id=users[1],
                             team_a_id=teams[0], team_b_id=teams[1], roguelike='water', status='confirmed')
        db.session.add(match)
        db.session.commit()
        return {'season': season.id, 'users': users, 'teams': teams, 'members': [m.id for m in members],
                'match': match.id, 'admin': models.User.query.filter_by(username='admin').one().id,
                'super': models.User.query.filter_by(username='super').one().id}


@pytest.fixture(scope='session')
def sqlite_seed(tmp_path_factory):
    root = tmp_path_factory.mktemp('sqlite-seed')
    database = root / 'seed.db'
    app = make_test_app(root, f'sqlite:///{database}')
    try:
        ids = seed_test_database(app)
    finally:
        with app.app_context():
            db.engine.dispose()
    return database, ids


@pytest.fixture(params=['sqlite', 'mysql'] if os.environ.get('P0_MYSQL_URL') else ['sqlite'])
def application(tmp_path, request):
    if request.param == 'sqlite':
        seed_path, ids = request.getfixturevalue('sqlite_seed')
        database = tmp_path / 'instance.db'
        copyfile(seed_path, database)
        app = make_test_app(tmp_path, f'sqlite:///{database}')
        app.test_ids = deepcopy(ids)
        yield app
        with app.app_context():
            db.engine.dispose()
        return

    base = make_url(os.environ['P0_MYSQL_URL'])
    database_name = 'p0_' + uuid.uuid4().hex
    admin_engine = create_engine(base.set(database=None))
    try:
        with admin_engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE {database_name} CHARACTER SET utf8mb4')
        try:
            uri = base.set(database=database_name).render_as_string(hide_password=False)
            app = make_test_app(tmp_path, uri)
            try:
                app.test_ids = seed_test_database(app)
                yield app
            finally:
                with app.app_context():
                    db.engine.dispose()
        finally:
            with admin_engine.begin() as conn:
                conn.exec_driver_sql(f'DROP DATABASE {database_name}')
    finally:
        admin_engine.dispose()


@pytest.fixture
def client(application):
    return application.test_client()


def login(client, user_id):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
