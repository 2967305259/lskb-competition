from datetime import datetime, timedelta, timezone

import app as a
from app import db
from conftest import login
from season_health import get_season_todos


def test_dashboard_lists_incomplete_season_configuration(application, client):
    login(client, application.test_ids['admin'])
    response = client.get('/admin/dashboard')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '本季待办' in html
    assert '未设置报名时间' in html
    assert '未设置比赛时间' in html
    assert '晋级数量为 0' in html
    assert '当前赛季没有启用轮播条目' in html
    assert '/admin/tournament-settings' in html
    assert '/admin/score-config' in html


def test_season_todos_clear_when_configuration_is_ready(application, client):
    start = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    with application.app_context():
        season = a.Tournament.get_active()
        season.registration_start = start
        season.registration_end = start + timedelta(days=1)
        season.tournament_start = start + timedelta(days=2)
        season.tournament_end = start + timedelta(days=3)
        season.window_times_utc = True
        setting = a.Setting.get_instance()
        setting.advance_count = 4
        setting.registration_open = False
        db.session.add(a.CarouselItem(tournament_id=season.id,
                                       file_path='image/1.png', sort_order=1,
                                       is_active=True))
        db.session.add(a.Rule(title='测试规则', content='ready', is_published=True))
        db.session.commit()
    login(client, application.test_ids['admin'])
    response = client.get('/admin/dashboard')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '配置就绪' in html
    assert '未设置报名时间' not in html
    assert '晋级数量为 0' not in html


def test_no_active_track_is_blocking_todo(application, client):
    with application.app_context():
        for track in a.Roguelike.query.all():
            track.is_active = False
        db.session.commit()
        todos = get_season_todos(a.Tournament.get_active())
        item = next(todo for todo in todos if todo['code'] == 'no_roguelike')
        assert item['done'] is False
        assert item['level'] == 'block'
        assert item['url'].endswith('/admin/roguelikes')
