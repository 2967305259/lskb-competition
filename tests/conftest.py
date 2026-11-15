import os
from pathlib import Path
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as models
from app import create_app, db
from management import initialize_database
from migrations import migrate_database


@pytest.fixture(params=['sqlite', 'mysql'] if os.environ.get('P0_MYSQL_URL') else ['sqlite'])
def application(tmp_path, request):
    admin_engine = None
    if request.param == 'mysql':
        base = make_url(os.environ['P0_MYSQL_URL'])
        database_name = 'p0_' + uuid.uuid4().hex
        admin_engine = create_engine(base.set(database=None))
        with admin_engine.begin() as conn:
            conn.execute(text('CREATE DATABASE ' + database_name + ' CHARACTER SET utf8mb4'))
        uri = base.set(database=database_name).render_as_string(hide_password=False)
    else:
        uri = 'sqlite:///' + str(tmp_path / 'instance.db')
    config = {'SQLALCHEMY_DATABASE_URI': uri, 'STATE_DIR': str(tmp_path / 'state'),
              'BACKUP_DIR': str(tmp_path / 'backups'), 'UPLOAD_ROOT': str(tmp_path / 'uploads'),
              'IMAGE_DIR': str(tmp_path / 'image'), 'SQLALCHEMY_ECHO': False,
              'SECRET_KEY': 'isolated-test-key', 'ADMIN_PASSWORD': 'isolated-admin-password', 'APP_VERSION': 'p0-v1',
              'SUPER_ADMIN_USERNAME': 'super', 'SUPER_ADMIN_PASSWORD': 'isolated-password'}
    if os.environ.get('P0_MYSQLDUMP'):
        config['MYSQLDUMP_COMMAND'] = os.environ['P0_MYSQLDUMP']
    for path in [tmp_path / 'image'] + [tmp_path / 'uploads' / n for n in ('avatars', 'teams', 'screenshots')]:
        path.mkdir(parents=True)
    (tmp_path / 'image' / '1.png').write_bytes(b'fixture-media')
    app = create_app('testing', config)
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
        app.test_ids = {'season': season.id, 'users': users, 'teams': teams, 'members': [m.id for m in members],
                        'match': match.id, 'admin': models.User.query.filter_by(username='admin').one().id,
                        'super': models.User.query.filter_by(username='super').one().id}
        engine = db.engine
    yield app
    with app.app_context():
        db.session.remove()
        engine.dispose()
    if admin_engine:
        with admin_engine.begin() as conn:
            conn.execute(text('DROP DATABASE ' + database_name))
        admin_engine.dispose()


@pytest.fixture
def client(application):
    return application.test_client()


def login(client, user_id):
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
