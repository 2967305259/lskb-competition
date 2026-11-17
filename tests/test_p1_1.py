import json
from datetime import datetime
from pathlib import Path

import app as a
from app import db
from conftest import login
from test_p0 import transition_data


def prepare_previous_configuration(application):
    ids = application.test_ids
    (Path(application.config['IMAGE_DIR']) / 'clip.mp4').write_bytes(b'fixture-video')
    with application.app_context():
        season = a.db.session.get(a.Tournament, ids['season'])
        season.registration_start = datetime(2026, 12, 1, 9, 0)
        season.registration_end = datetime(2026, 12, 5, 23, 0)
        season.tournament_start = datetime(2026, 12, 6, 10, 0)
        season.tournament_end = datetime(2026, 12, 8, 22, 0)
        setting = a.Setting.get_instance()
        setting.advance_count = 4
        setting.team_size = 5
        setting.allow_incomplete_teams = True
        db.session.add_all([
            a.CarouselItem(tournament_id=season.id, media_type='image',
                           file_path='image/1.png', title='往届图片', sort_order=2, is_active=False),
            a.CarouselItem(tournament_id=season.id, media_type='video',
                           file_path='image/clip.mp4', title='继承视频', sort_order=1, link_url='/rules'),
        ])
        db.session.commit()


def test_new_season_wizard_inherits_selected_values_and_carousel(application, client):
    prepare_previous_configuration(application)
    data = transition_data(application, client, True)
    data.update({
        'inherit_registration_window': '1',
        'inherit_tournament_window': '1',
        'inherit_advance_count': '1',
        'inherit_team_size': '1',
        'inherit_allow_incomplete_teams': '1',
        'inherit_carousel': '1',
    })
    assert client.post('/admin/new-season', data=data).status_code == 302
    with application.app_context():
        old = db.session.get(a.Tournament, application.test_ids['season'])
        new = a.Tournament.get_active()
        setting = a.Setting.get_instance()
        assert new.registration_start == old.registration_start
        assert new.registration_end == old.registration_end
        assert new.tournament_start == old.tournament_start
        assert new.tournament_end == old.tournament_end
        assert setting.advance_count == 4
        assert setting.team_size == 5
        assert setting.allow_incomplete_teams is True
        assert a.CarouselItem.query.filter_by(tournament_id=old.id).count() == 2
        copied = a.CarouselItem.query.filter_by(tournament_id=new.id).order_by(a.CarouselItem.sort_order).all()
        assert len(copied) == 2
        assert [item.file_path for item in copied] == ['image/clip.mp4', 'image/1.png']
        assert [item.is_active for item in copied] == [True, False]
        log = a.SystemLog.query.filter_by(action='new_season').one()
        detail = json.loads(log.detail)
        assert set(detail['inherited']) == {
            'bonus_score', 'registration_window', 'tournament_window',
            'advance_count', 'team_size', 'allow_incomplete_teams', 'carousel',
        }
        assert detail['carousel_copied'] == 2
    home = client.get('/').get_data(as_text=True)
    assert '<video src="/carousel-media/clip.mp4"' in home and '继承视频' in home
    assert '/carousel-media/1.png' not in home
    assert client.get('/carousel-media/clip.mp4').data == b'fixture-video'


def test_new_season_wizard_without_inheritance_keeps_legacy_defaults(application, client):
    prepare_previous_configuration(application)
    data = transition_data(application, client, True)
    assert client.post('/admin/new-season', data=data).status_code == 302
    with application.app_context():
        new = a.Tournament.get_active()
        setting = a.Setting.get_instance()
        assert new.registration_start is None
        assert new.registration_end is None
        assert new.tournament_start is None
        assert new.tournament_end is None
        assert setting.advance_count == 0
        # Team settings retain the established global behavior when unchecked.
        assert setting.team_size == 5
        assert setting.allow_incomplete_teams is True
        assert a.CarouselItem.query.filter_by(tournament_id=new.id).count() == 0
    home = client.get('/').get_data(as_text=True)
    assert '<img src="/carousel-media/1.png"' in home
    assert '继承视频' not in home
    assert '首页来源：素材目录（image/）' in client.get('/admin/carousel').get_data(as_text=True)


def test_new_season_can_inherit_empty_dates(application, client):
    data = transition_data(application, client, True)
    data.update({'inherit_registration_window': '1', 'inherit_tournament_window': '1'})
    assert client.post('/admin/new-season', data=data).status_code == 302
    with application.app_context():
        new = a.Tournament.get_active()
        assert new.registration_start is None
        assert new.registration_end is None
        assert new.tournament_start is None
        assert new.tournament_end is None
