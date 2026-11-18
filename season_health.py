"""Configuration readiness checks for the active season dashboard."""

from flask import has_request_context, url_for
from season_phase import has_enabled_track, no_tracks_message, window_state


def _app():
    import app
    return app


def _admin_url(endpoint):
    if has_request_context():
        return url_for(endpoint)
    fallback = {
        'admin.tournament_settings': '/admin/tournament-settings',
        'admin.registration_control': '/admin/registration',
        'admin.score_config': '/admin/score-config',
        'admin.carousel': '/admin/carousel',
        'admin.roguelikes': '/admin/roguelikes',
        'admin.manage_rules': '/admin/rules',
    }
    return fallback[endpoint]


def get_season_todos(tournament):
    """Return readiness checks for the current season.

    These checks describe configuration completeness. They do not change the
    manual registration and phase controls used by the application.
    """
    if tournament is None:
        return []
    a = _app()
    setting = a.Setting.get_instance()
    registration_state = window_state(tournament.registration_start, tournament.registration_end,
                                      tournament.window_times_utc)
    tournament_state = window_state(tournament.tournament_start, tournament.tournament_end,
                                    tournament.window_times_utc)
    registration_overdue = setting.registration_open and registration_state == 'expired'
    carousel_done = a.CarouselItem.query.filter_by(
        tournament_id=tournament.id, is_active=True).count() > 0
    roguelike_done = has_enabled_track()
    rules_done = a.Rule.query.filter_by(is_published=True).count() > 0
    return [
        {'code': 'no_registration_window', 'level': 'info', 'order': 2,
         'message': '未设置报名时间', 'url': _admin_url('admin.tournament_settings'),
         'done': registration_state != 'missing'},
        {'code': 'no_tournament_window', 'level': 'info', 'order': 2,
         'message': '未设置比赛时间', 'url': _admin_url('admin.tournament_settings'),
         'done': tournament_state != 'missing'},
        {'code': 'invalid_registration_window', 'level': 'warning', 'order': 1,
         'message': '报名结束时间须晚于开始时间', 'url': _admin_url('admin.tournament_settings'),
         'done': registration_state != 'invalid'},
        {'code': 'invalid_tournament_window', 'level': 'warning', 'order': 1,
         'message': '比赛结束时间须晚于开始时间', 'url': _admin_url('admin.tournament_settings'),
         'done': tournament_state != 'invalid'},
        {'code': 'tournament_overdue', 'level': 'warning', 'order': 1,
         'message': '比赛计划结束时间已过，赛事仍未手动结束',
         'url': _admin_url('admin.tournament_settings'),
         'done': tournament_state != 'expired' or tournament.status == 'finished'},
        {'code': 'registration_overdue', 'level': 'warning', 'order': 1,
         'message': '报名截止时间已过但报名仍开放',
         'url': _admin_url('admin.registration_control'),
         'done': not registration_overdue},
        {'code': 'no_advance_count', 'level': 'warning', 'order': 1,
         'message': '晋级数量为 0', 'url': _admin_url('admin.score_config'),
         'done': (setting.advance_count or 0) > 0},
        {'code': 'no_carousel', 'level': 'info', 'order': 2,
         'message': '当前赛季没有启用轮播条目',
         'url': _admin_url('admin.carousel'), 'done': carousel_done},
        {'code': 'no_roguelike', 'level': 'block', 'order': 0,
         'message': no_tracks_message(),
         'url': _admin_url('admin.roguelikes'), 'done': roguelike_done},
        {'code': 'no_rules', 'level': 'info', 'order': 2,
         'message': '没有已发布规则', 'url': _admin_url('admin.manage_rules'),
         'done': rules_done},
    ]
