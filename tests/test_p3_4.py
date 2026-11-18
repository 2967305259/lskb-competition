import importlib

import pytest

import app as a


def test_development_missing_credentials_are_reported(monkeypatch, caplog):
    monkeypatch.delenv('SECRET_KEY', raising=False)
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)

    first = a.create_app('development')
    second = a.create_app('development')
    assert first.secret_key and first.secret_key != second.secret_key
    assert '未设置 SECRET_KEY' in first.config['SECRET_KEY_WARNING']
    assert '未设置 SECRET_KEY' in caplog.text
    assert '未设置 ADMIN_PASSWORD' in caplog.text
    assert first.secret_key not in caplog.text


@pytest.mark.parametrize('missing', ['SECRET_KEY', 'ADMIN_PASSWORD'])
def test_production_requires_explicit_credentials(monkeypatch, missing):
    monkeypatch.delenv('FLASK_ENV', raising=False)
    monkeypatch.setenv('SECRET_KEY', 'isolated-explicit-test-key')
    monkeypatch.setenv('ADMIN_PASSWORD', 'isolated-explicit-test-password')
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=f'必须显式设置 {missing}'):
        a.create_app('production')
    with pytest.raises(RuntimeError, match=f'必须显式设置 {missing}'):
        importlib.import_module('wsgi')


def test_production_warns_on_known_defaults(monkeypatch, caplog):
    monkeypatch.setenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    monkeypatch.setenv('ADMIN_PASSWORD', 'admin123')

    app = a.create_app('production')
    assert '正在使用已知的默认会话密钥' in app.config['SECRET_KEY_WARNING']
    assert '正在使用已知的默认会话密钥' in caplog.text
    assert 'ADMIN_PASSWORD 使用已知的默认口令' in caplog.text
    assert app.secret_key not in caplog.text and 'admin123' not in caplog.text


def test_default_password_warns_in_admin_without_blocking_existing_logins(application, client):
    ids = application.test_ids
    with application.app_context():
        admin = a.db.session.get(a.User, ids['admin'])
        admin.set_password('admin123')
        assert admin.password_changed is False
        a.db.session.commit()

    data = {'username': 'admin', 'password': 'admin123'}
    response = client.post('/auth/login', data=data, follow_redirects=True)
    assert response.status_code == 200 and response.request.path == '/admin/dashboard'
    page = response.get_data(as_text=True)
    assert '当前账号仍在使用已知的默认口令' in page
    assert 'admin123' not in page

    response = client.post('/auth/change-password', data={
        'old_password': 'admin123', 'password': 'NewIsolated987', 'confirm_password': 'NewIsolated987',
    }, follow_redirects=True)
    assert response.request.path == '/admin/dashboard'
    assert '当前账号仍在使用已知的默认口令' not in response.get_data(as_text=True)
    client.get('/auth/logout')
    response = client.post('/auth/login', data={'username': 'alpha', 'password': 'test-password'})
    assert response.status_code == 302 and response.location == '/captain/dashboard'
