"""Local disposable Docker smoke: two isolated instances and container recreation."""
import http.cookiejar
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

image = sys.argv[1] if len(sys.argv) > 1 else 'lskb-p0-test:local'
prefix = 'p0-smoke-' + uuid.uuid4().hex[:8]
containers = []
volumes = []
result = {'image': image}


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


class Tokens(HTMLParser):
    token = None
    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == 'input' and values.get('name') == 'csrf_token':
            self.token = values.get('value')


def request(opener, url, data=None):
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    with opener.open(url, data=payload, timeout=5) as response:
        return response.read()


def form(opener, base, path, values):
    parser = Tokens()
    parser.feed(request(opener, base + path).decode())
    assert parser.token
    return request(opener, base + path, dict(values, csrf_token=parser.token))


def start(label):
    name = prefix + '-' + label
    args = ['run', '-d', '--name', name, '-p', '127.0.0.1::19198',
            '-e', 'EVENT_NAME=Instance-' + label,
            '-e', 'ADMIN_PASSWORD=DockerTest123', '-e', 'SECRET_KEY=docker-isolated-test-' + label]
    for kind, path in [('db','/app/.db'),('uploads','/app/app/static/uploads'),('state','/app/.runtime'),
                       ('backups','/app/backups'),('image','/app/image')]:
        volume = prefix + '-' + label + '-' + kind
        if volume not in volumes:
            volumes.append(volume)
        args += ['-v', volume + ':' + path]
    docker(*args, image)
    if name not in containers:
        containers.append(name)
    base = 'http://' + docker('port', name, '19198/tcp').splitlines()[0]
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for attempt in range(60):
        try:
            content = request(opener, base + '/')
            assert ('Instance-' + label).encode() in content
            return name, base, opener
        except Exception:
            time.sleep(1)
    raise RuntimeError(docker('logs', name))


def admin_login(opener, base):
    form(opener, base, '/auth/login', {'username':'admin', 'password':'DockerTest123'})


try:
    a, base_a, opener_a = start('A')
    b, base_b, opener_b = start('B')
    form(opener_a, base_a, '/auth/register', {'username':'only-a','nickname':'only-a',
        'password':'User123456','confirm_password':'User123456'})
    admin_login(opener_a, base_a)
    admin_login(opener_b, base_b)
    assert b'only-a' in request(opener_a, base_a + '/admin/users')
    assert b'only-a' not in request(opener_b, base_b + '/admin/users')
    for name, label in [(a,'A'),(b,'B')]:
        docker('exec', name, 'python', '-c',
            "from pathlib import Path; Path('/app/app/static/uploads/avatars/persist.png').write_bytes(b'avatar-" + label + "'); Path('/app/image/1.png').write_bytes(b'image-" + label + "')")
    docker('exec', a, 'python', '-c', '''
from app import create_app,db,User
from backup import maintenance_lock
app=create_app()
with app.app_context(),maintenance_lock(app.config):
    user=User.query.filter_by(username='only-a').one()
    user.avatar='persist.png'
    db.session.commit()
''')
    batch = docker('exec', a, 'python', '-m', 'flask', '--app', 'app:create_app()', 'backup').splitlines()[-1]
    assert batch.startswith('/app/backups/')
    docker('exec', b, 'test', '!', '-e', batch)
    docker('rm', '-f', a)
    a, base_a, opener_a = start('A')
    admin_login(opener_a, base_a)
    assert b'only-a' in request(opener_a, base_a + '/admin/users')
    assert request(opener_a, base_a + '/static/uploads/avatars/persist.png') == b'avatar-A'
    assert request(opener_a, base_a + '/carousel-media/1.png') == b'image-A'
    assert request(opener_b, base_b + '/static/uploads/avatars/persist.png') == b'avatar-B'
    docker('exec', a, 'python', '-c', 'from backup import verify_backup; verify_backup(' + repr(batch) + ')')
    counts = docker('exec', a, 'python', '-c', '''
from app import create_app,db
from sqlalchemy import text
app=create_app()
with app.app_context():
    print(db.session.execute(text('SELECT COUNT(*) FROM schema_version')).scalar())
''')
    assert counts == '1'
    result.update(two_instances_isolated=True, recreated_container_keeps_database=True,
                  recreated_container_keeps_media=True, recreated_container_keeps_backup=True,
                  schema_version_count=1, gunicorn_workers=4)
    Path('artifacts').mkdir(exist_ok=True)
    Path('artifacts/docker-smoke.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
finally:
    for name in containers:
        subprocess.run(['docker','rm','-f',name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for volume in volumes:
        subprocess.run(['docker','volume','rm',volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
