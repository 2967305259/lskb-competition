"""Exercise both Compose stacks with disposable projects and the same built application image."""
from html.parser import HTMLParser
import http.cookiejar
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
FILES = {'/static/uploads/avatars/player.png': b'avatar',
         '/static/uploads/teams/team.png': b'team photo',
         '/static/uploads/screenshots/score.png': b'score evidence',
         '/carousel-media/cover.png': b'carousel'}
PASSWORD = 'isolated$@:#%+/password'
ADMIN_PASSWORD = 'IsolatedSmoke123'


def docker(*args, env=None, data=None):
    result = subprocess.run(['docker', *args], input=data, capture_output=True, text=True, cwd=ROOT, env=env)
    if result.returncode:
        detail = (result.stdout + result.stderr).replace(PASSWORD, '<redacted>').replace(ADMIN_PASSWORD, '<redacted>')
        raise RuntimeError(detail)
    return result.stdout.strip()


def get_port():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        return str(server.getsockname()[1])


class CSRFToken(HTMLParser):
    value = None

    def handle_starttag(self, tag, attributes):
        fields = dict(attributes)
        if tag == 'input' and fields.get('name') == 'csrf_token':
            self.value = fields.get('value')


class Stack:
    def __init__(self, root, backend, image, port, mysql_image):
        self.backend = backend
        self.name = f'p37-{backend}-{uuid.uuid4().hex[:8]}'
        self.event = 'Compose ' + backend
        self.root = root / backend
        self.root.mkdir()
        media = self.root / 'image'
        media.mkdir()
        (media / 'cover.png').write_bytes(FILES['/carousel-media/cover.png'] + backend.encode())
        repo, tag = image.rsplit(':', 1) if ':' in image.rsplit('/', 1)[-1] else (image, 'latest')
        settings = {'IMAGE': repo, 'IMAGE_TAG': tag, 'MYSQL_IMAGE': mysql_image, 'PORT': port,
                    'EVENT_NAME': self.event, 'TOURNAMENT_NAME': self.event, 'SEASON_NUMBER': '3',
                    'IMAGE_HOST_DIR': str(media), 'ADMIN_USERNAME': 'admin', 'ADMIN_PASSWORD': ADMIN_PASSWORD,
                    'SECRET_KEY': uuid.uuid4().hex, 'SUPER_ADMIN_USERNAME': 'operator',
                    'SUPER_ADMIN_PASSWORD': ADMIN_PASSWORD, 'WEB_WORKERS': '4', 'TEAM_SIZE': '4',
                    'DEFAULT_BONUS_SCORE': '500', 'DEFAULT_DIFFICULTY_MIN': '12', 'DEFAULT_DIFFICULTY_MAX': '18',
                    'DEFAULT_DIFFICULTY_STEP': '0.1', 'DATABASE_URL': '', 'MYSQL_DATABASE': 'custom_arena',
                    'MYSQL_USER': 'custom_operator', 'MYSQL_PASSWORD': PASSWORD, 'MYSQL_ROOT_PASSWORD': uuid.uuid4().hex}
        env_file = self.root / 'stack.env'
        env_file.write_text(''.join(f"{key}='{value}'\n" for key, value in settings.items()))
        env_file.chmod(0o600)
        self.env = {key: value for key, value in os.environ.items() if key not in settings}
        manifest = 'docker-compose.sqlite.yml' if backend == 'sqlite' else 'docker-compose.yml'
        self.args = ['compose', '--project-name', self.name, '--env-file', str(env_file), '-f', manifest]
        self.base = f'http://127.0.0.1:{port}'
        self.opener = None

    def compose(self, *args, data=None):
        return docker(*self.args, *args, env=self.env, data=data)

    def app_python(self, code, *args):
        return self.compose('exec', '-T', 'app', 'python', '-', *args, data=code)

    def start(self):
        try:
            self.compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '120')
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}\n{self.compose('logs', '--tail', '30')}") from None
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if self.event.encode() in self.request('/'):
                    self.form('/auth/login', '/auth/login', {'username': 'operator', 'password': ADMIN_PASSWORD})
                    assert '数据库与素材备份'.encode() in self.request('/admin/data-tools')
                    return
            except OSError:
                pass
            time.sleep(1)
        raise RuntimeError(self.compose('logs', '--tail', '40', 'app').replace(PASSWORD, '<redacted>'))

    def request(self, path, data=None):
        payload = urllib.parse.urlencode(data).encode() if data is not None else None
        with self.opener.open(self.base + path, data=payload, timeout=30) as response:
            return response.read()

    def form(self, source, action, fields):
        token = CSRFToken()
        token.feed(self.request(source).decode())
        assert token.value, f'CSRF token missing on {source}'
        return self.request(action, dict(fields, csrf_token=token.value))

    def info(self):
        return json.loads(self.compose('exec', '-T', 'app', 'python', '-m', 'flask',
                                       '--app', 'app:create_app()', 'deployment-info'))


SEED = '''
from io import BytesIO
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from app import CarouselItem, Match, ScoreLog, Team, TeamMember, Tournament, User, create_app, db
from backup import maintenance_lock
from season_safety import apply_outcome, capture_match_rule, set_score
from scoring import recalculate_all_scores

app = create_app()
suffix = sys.argv[1]
with app.app_context(), maintenance_lock(app.config):
    uploads = Path(app.config['UPLOAD_ROOT'])
    for path, content in [('avatars/player.png', b'avatar'), ('teams/team.png', b'team photo'),
                          ('screenshots/score.png', b'score evidence')]:
        (uploads / path).write_bytes(content + suffix.encode())
    season = Tournament.get_active()
    people = [User(username=f'{suffix}-{side}', nickname=f'{suffix}-{side}', role='captain',
                   password_hash='fixture', avatar='player.png') for side in ('a', 'b')]
    db.session.add_all(people)
    db.session.flush()
    teams = [Team(team_name=user.username, captain_id=user.id, tournament_id=season.id, team_photo='team.png')
             for user in people]
    db.session.add_all(teams)
    db.session.flush()
    db.session.add_all(TeamMember(team_id=team.id, user_id=team.captain_id, roguelike='water') for team in teams)
    match = Match(tournament_id=season.id, team_a_id=teams[0].id, team_b_id=teams[1].id,
                  player_a_id=people[0].id, player_b_id=people[1].id, roguelike='water',
                  match_note='[截图] /static/uploads/screenshots/score.png')
    db.session.add(match)
    db.session.flush()
    capture_match_rule(match)
    set_score(match, 'a', '100', '14')
    set_score(match, 'b', '90', '14')
    apply_outcome(match, 'win_a')
    match.settlement_state = 'settled'
    from datetime import datetime
    match.settled_at = datetime.utcnow()
    db.session.add(ScoreLog(match_id=match.id, modified_by=User.query.filter_by(username='operator').one().id,
                            new_result='fixture final score'))
    db.session.add(CarouselItem(tournament_id=season.id, file_path='image/cover.png', title='Smoke carousel'))
    recalculate_all_scores(season.id)
    db.session.commit()
    operator_id = User.query.filter_by(username='operator').one().id

with TemporaryDirectory(prefix='lskb-smoke-clip-') as directory, app.test_client() as client:
    clip = Path(directory) / 'sample.webm'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=32x32:r=10:d=0.3',
                    '-c:v', 'libvpx-vp9', str(clip)], check=True, capture_output=True, timeout=15)
    with client.session_transaction() as session:
        session['_user_id'] = str(operator_id)
        session['_fresh'] = True
    page = client.get('/admin/carousel').get_data(as_text=True)
    token = re.search('name="csrf_token" value="([^"]+)"', page).group(1)
    response = client.post('/admin/carousel/add', data={'csrf_token': token, 'media_type': 'video',
        'title': 'Managed smoke clip', 'media': (BytesIO(clip.read_bytes()), 'sample.webm')})
    assert response.status_code == 302, response.get_data(as_text=True)
'''


RESTORE = '''
from hashlib import sha256
from pathlib import Path
import os
import sqlite3
import sys
from app import Match, ScoreLog, Team, UploadAsset, User, create_app, db
from migrations import assert_schema_current

app = create_app()
batch = Path('/tmp/p37-batch')
media = Path('/tmp/p37-recovery')
with app.app_context():
    url = (db.engine.url.set(database='p37_recovered') if db.engine.dialect.name == 'mysql'
           else 'sqlite:////tmp/p37-recovered.db')
from sqlalchemy.engine import make_url
os.environ['RESTORE_DATABASE_URL'] = make_url(url).render_as_string(hide_password=False)
result = app.test_cli_runner().invoke(args=['restore-rehearsal', '--batch', str(batch),
                                           '--media-destination', str(media)])
assert result.exit_code == 0, result.output
target = create_app('production', {'SQLALCHEMY_DATABASE_URI': url, 'UPLOAD_ROOT': str(media / 'uploads'),
                    'IMAGE_DIR': str(media / 'image'), 'STATE_DIR': '/tmp/p37-state',
                    'BACKUP_DIR': '/tmp/p37-backups'})
with target.app_context():
    assert_schema_current(db.engine)
    assert User.query.filter_by(username=sys.argv[1] + '-a').count() == 1
    assert Match.query.one().player_a_score == 100
    assert Team.query.filter_by(team_name=sys.argv[1] + '-a').one().team_score == 60000
    assert ScoreLog.query.one().new_result == 'fixture final score'
    operator_id = User.query.filter_by(username='operator').one().id
    clip = UploadAsset.query.filter_by(original_name='sample.webm').one()
    clip_path, checksum = clip.relative_path, clip.sha256
    assert clip.mime_type == 'video/webm'
    version = (db.session.execute(db.text('SELECT VERSION()')).scalar() if db.engine.dialect.name == 'mysql'
               else sqlite3.sqlite_version)
with target.test_client() as client:
    assert client.get('/').status_code == 200
    evidence_url = '/static/uploads/screenshots/score.png'
    assert client.get(evidence_url).status_code == 404
    public_clip = client.get('/static/uploads/' + clip_path)
    assert public_clip.status_code == 200 and sha256(public_clip.data).hexdigest() == checksum
    with client.session_transaction() as session:
        session['_user_id'] = str(operator_id)
        session['_fresh'] = True
    for path, content in [('/static/uploads/avatars/player.png', b'avatar'),
                          ('/static/uploads/teams/team.png', b'team photo'),
                          (evidence_url, b'score evidence'),
                          ('/carousel-media/cover.png', b'carousel')]:
        assert client.get(path).data == content + sys.argv[1].encode()
print(version)
'''


def assert_http_data(stack, other):
    roster = stack.request('/admin/users')
    assert f'{stack.backend}-a'.encode() in roster and f'{other.backend}-a'.encode() not in roster
    assert stack.request('/admin/export').startswith(b'PK')
    for path, content in FILES.items():
        assert stack.request(path) == content + stack.backend.encode()


def backup_and_recreate(stack, other, app_version):
    page = stack.form('/admin/data-tools', '/admin/data-tools/backup', {})
    link = re.search(rb'href="(/admin/data-tools/backup/[^"/]+/manifest\.json)"', page)
    assert link, stack.compose('logs', '--tail', '30', 'app')
    manifest_path = link[1].decode()
    prefix = manifest_path.removesuffix('manifest.json')
    manifest = json.loads(stack.request(manifest_path))
    assert manifest['code_version'] == app_version and manifest['database_type'] == stack.backend
    assert manifest['schema_version'] == stack.info()['schema_version']
    batch = stack.root / 'batch'
    batch.mkdir()
    for name in ('manifest.json', *manifest['files']):
        (batch / name).write_bytes(stack.request(prefix + name))
    assert manifest['rows']['matches'] == 1 and manifest['rows']['score_logs'] == 1
    if other.opener is not None:
        try:
            other.request(manifest_path)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError('backup from a different instance is accessible')

    original = stack.compose('ps', '-q', 'app')
    stack.compose('down')
    stack.start()
    assert stack.compose('ps', '-q', 'app') != original
    assert_http_data(stack, other)
    for path in batch.iterdir():
        assert stack.request(prefix + path.name) == path.read_bytes()
    assert stack.app_python(
        "from pathlib import Path\n"
        "assert b'wsgi:app' in Path('/proc/1/cmdline').read_bytes()\n"
        "workers = 0\n"
        "for process in Path('/proc').glob('[0-9]*/stat'):\n"
        "    try:\n"
        "        parent = process.read_text().rsplit(')', 1)[1].split()[1]\n"
        "        if parent == '1' and b'wsgi:app' in process.with_name('cmdline').read_bytes():\n"
        "            workers += 1\n"
        "    except (FileNotFoundError, ProcessLookupError):\n"
        "        continue\n"
        "print(workers)") == '4'
    if stack.backend == 'mysql':
        stack.compose('exec', '-T', 'mysql', 'sh', '-c',
                      'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --user=root', data=
                      "CREATE DATABASE p37_recovered; GRANT ALL ON p37_recovered.* TO 'custom_operator'@'%';")
    docker('cp', str(batch), stack.compose('ps', '-q', 'app') + ':/tmp/p37-batch')
    database_version = stack.app_python(RESTORE, stack.backend)
    assert_http_data(stack, other)
    return {'database_version': database_version, 'deployment': stack.info(),
            'recreated': True, 'download_and_restore': True}


def main(image, backend, mysql_image):
    app_version = docker('image', 'inspect', image, '--format', '{{index .Config.Labels "org.opencontainers.image.version"}}')
    assert app_version and app_version != '<no value>'
    with TemporaryDirectory(prefix='lskb-compose-smoke-') as directory:
        root = Path(directory)
        ports = {get_port(), get_port()}
        while len(ports) < 2:
            ports.add(get_port())
        stacks = [Stack(root, backend, image, port, mysql_image)
                  for backend, port in zip(('sqlite', 'mysql'), sorted(ports))]
        pairs = [(stack, other) for stack, other in (stacks, stacks[::-1])
                 if backend == 'both' or stack.backend == backend]
        try:
            for stack, _ in pairs:
                stack.start()
                assert stack.info()['app_version'] == app_version
                stack.app_python(SEED, stack.backend)
                print(f'{stack.backend}: deployed and seeded', flush=True)
            if backend != 'sqlite':
                mysql = stacks[1]
                assert mysql.info()['database'] == 'custom_arena'
                assert 'custom_operator@' in mysql.app_python(
                    "from app import create_app, db\nwith create_app().app_context():\n"
                    "    print(db.session.execute(db.text('SELECT CURRENT_USER()')).scalar())")
                bindings = docker('inspect', mysql.compose('ps', '-q', 'mysql'),
                                  '--format', '{{json .HostConfig.PortBindings}}')
                assert not json.loads(bindings)
            for stack, other in pairs:
                assert_http_data(stack, other)
            report = {}
            for stack, other in pairs:
                report[stack.backend] = backup_and_recreate(stack, other, app_version)
                print(f'{stack.backend}: recreated, downloaded and restored', flush=True)
            if 'mysql' in report:
                report['mysql']['server_image'] = mysql_image
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            for stack, _ in reversed(pairs):
                stack.compose('down', '--volumes', '--remove-orphans')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', help='local application image built from this repository')
    parser.add_argument('--backend', choices=('both', 'sqlite', 'mysql'), default='both',
                        help='run both projects concurrently or one backend on its own')
    parser.add_argument('--mysql-image', default='mysql:8.0', help='MySQL service image to verify')
    args = parser.parse_args()
    main(args.image, args.backend, args.mysql_image)
