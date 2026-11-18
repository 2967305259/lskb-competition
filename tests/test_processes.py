"""Separate interpreter probes catch locks that protect only threads, not workers."""
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from app import db, Tournament, SeasonTransition
from conftest import login
from test_p0 import transition_data


pytestmark = pytest.mark.maintenance


def launch(application, code, data=None):
    keys = ['SQLALCHEMY_DATABASE_URI','STATE_DIR','BACKUP_DIR','UPLOAD_ROOT','IMAGE_DIR','SECRET_KEY',
            'APP_VERSION','MYSQLDUMP_COMMAND','SQLALCHEMY_ECHO']
    config = {key: application.config[key] for key in keys if key in application.config}
    return subprocess.Popen([sys.executable, '-c', code, json.dumps(config), json.dumps(data)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ)


def test_different_tokens_from_same_page_across_processes(application, client):
    first = transition_data(application, client, True)
    second = transition_data(application, client, True)
    code = '''
import json,sys
from app import create_app
config=json.loads(sys.argv[1]); data=json.loads(sys.argv[2]); actor=data.pop('actor')
app=create_app('testing',config)
with app.test_client() as client:
    with client.session_transaction() as session:
        session['_user_id']=str(actor); session['_fresh']=True
    print(client.post('/admin/new-season',data=data).status_code)
'''
    workers = [launch(application, code, dict(data, actor=application.test_ids['admin'])) for data in (first, second)]
    results = [worker.communicate(timeout=40) for worker in workers]
    assert all(worker.returncode == 0 for worker in workers), results
    assert sorted(int(stdout.strip()) for stdout, stderr in results) == [302, 409]
    with application.app_context():
        assert Tournament.query.count() == 2
        assert SeasonTransition.query.count() == 1


def test_four_worker_preparation_does_not_repeat_schema_changes(application):
    code = '''
import json,sys
from app import create_app,db
from migrations import migrate_database,assert_schema_current
app=create_app('testing',json.loads(sys.argv[1]))
with app.app_context():
    assert migrate_database(db.engine,app.config) is None
    assert_schema_current(db.engine)
    print('ready')
'''
    workers = [launch(application, code) for _ in range(4)]
    results = [worker.communicate(timeout=40) for worker in workers]
    assert all(worker.returncode == 0 for worker in workers), results
    assert all(stdout.strip() == 'ready' for stdout, stderr in results), results
    with application.app_context():
        with db.engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM schema_version')).scalar() == 1
