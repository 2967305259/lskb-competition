"""Configuration readiness checks for the active season dashboard."""

from datetime import datetime

from flask import has_request_context, url_for


def _app():
    import app
    return app


def _admin_url(endpoint):
    if has_request_context():
        return url_for(endpoint)
    fallback = {
        'admin.tournament_settings': '/admin/tournament-settings',
        'admin.registration_control': '/admin/registration-control',
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
    now = datetime.utcnow()
    registration_window_done = bool(
        tournament.registration_start and tournament.registration_end)
    tournament_window_done = bool(
        tournament.tournament_start and tournament.tournament_end)
    registration_overdue = bool(
        setting.registration_open and tournament.registration_end and
        now > tournament.registration_end)
    carousel_done = a.CarouselItem.query.filter_by(
        tournament_id=tournament.id, is_active=True).count() > 0
    roguelike_done = a.Roguelike.query.filter_by(is_active=True).count() > 0
    rules_done = a.Rule.query.filter_by(is_published=True).count() > 0
    return [
        {'code': 'no_registration_window', 'level': 'info', 'order': 2,
         'message': '未设置报名时间', 'url': _admin_url('admin.tournament_settings'),
         'done': registration_window_done},
        {'code': 'no_tournament_window', 'level': 'info', 'order': 2,
         'message': '未设置比赛时间', 'url': _admin_url('admin.tournament_settings'),
         'done': tournament_window_done},
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
         'message': '没有启用赛道，不能开放参赛',
         'url': _admin_url('admin.roguelikes'), 'done': roguelike_done},
        {'code': 'no_rules', 'level': 'info', 'order': 2,
         'message': '没有已发布规则', 'url': _admin_url('admin.manage_rules'),
         'done': rules_done},
    ]
