"""Manual season admission and local-time presentation for UTC-backed dates."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app


REGISTRATION_ACTIONS = {
    'auth.register', 'captain.create_team', 'player.request_join',
    'captain.invite_player', 'captain.search_players', 'player.accept_invitation',
    'player.reject_invitation', 'captain.accept_join_request', 'captain.reject_join_request',
    'captain.cancel_invitation', 'player.select_roguelike', 'captain.assign_position',
}
FINISHED_SCHEDULING_ACTIONS = {
    'admin.manual_match',
    'admin.schedule_update_time', 'admin.schedule_split_match', 'admin.schedule_delete_match',
    'admin.schedule_confirm_match', 'admin.schedule_insert_exhibition', 'admin.schedule_quick_exhibition',
}
NO_TRACKS = '没有启用赛道，不能开放参赛'


def has_enabled_track():
    from app import Roguelike
    return Roguelike.query.filter_by(is_active=True).first() is not None


def registration_denial(season, *, tracks_enabled=None, registration_open=None):
    from app import Setting
    if season is None:
        return '当前没有活跃赛季'
    if tracks_enabled is None:
        tracks_enabled = has_enabled_track()
    if not tracks_enabled:
        return NO_TRACKS
    if registration_open is None:
        registration_open = Setting.get_instance().registration_open
    if not registration_open:
        return '报名已由管理员关闭'
    return None


def is_match_in_current_season(match, season):
    return season is not None and match.tournament_id == season.id


def player_match_denial(action, match, user_id, season):
    if not is_match_in_current_season(match, season):
        return '该比赛属于往届或归属待核对，普通入口只读', 403
    if user_id not in (match.player_a_id, match.player_b_id):
        return '仅本场比赛选手可操作', 403
    if action in ('schedule_match', 'confirm_schedule'):
        if season.status == 'finished':
            return '本届赛事已结束，已停止排赛', 409
        states = ('pending', 'scheduled') if action == 'schedule_match' else ('scheduled',)
        if match.status not in states:
            return '当前状态不允许设定或确认时间', 409
        if action == 'confirm_schedule':
            confirmed = match.confirmed_a if user_id == match.player_a_id else match.confirmed_b
            if confirmed:
                return '该选手已确认时间', 409
    elif action == 'submit_result':
        side = 'a' if user_id == match.player_a_id else 'b'
        if (match.settlement_state == 'settled'
                or match.status not in ('confirmed', 'submitted', 'forfeit_a', 'forfeit_b')
                or match.forfeit_side == side):
            return '当前整场状态不允许提交', 409
        if getattr(match, f'player_{side}_score') is not None:
            return '该选手已经提交过整场成绩', 409
    elif action == 'forfeit_match':
        if match.status not in ('confirmed', 'submitted') or match.settlement_state == 'settled':
            return '当前比赛状态不允许弃权', 409
    elif action == 'send_match_note' and match.status not in ('pending', 'scheduled', 'confirmed', 'submitted'):
        return '当前比赛状态不允许留言', 409
    return None


def admin_result_denial(action, match):
    if action == 'review':
        if match.settlement_state != 'settled' or match.status not in ('finished', 'forfeit_a', 'forfeit_b'):
            return '只能审核已确认结算的比赛'
    else:
        if match.status not in ('finished', 'forfeit_a', 'forfeit_b', 'submitted'):
            return '该整场尚未提交齐备或结束'
        if match.settlement_state == 'legacy':
            return '旧数据须先核对结算和规则基准'
    return None


def admin_schedule_protected(match):
    return any(row.player_a_score is not None or row.player_b_score is not None
               or row.settlement_state == 'settled'
               or row.status in ('submitted', 'dispute', 'finished', 'forfeit_a', 'forfeit_b')
               for row in (match, *match.children))


def event_zone():
    return ZoneInfo(current_app.config['EVENT_TIMEZONE'])


def event_now():
    return datetime.now(event_zone())


def parse_event_time(value, fmt='%Y-%m-%dT%H:%M'):
    if not value or not value.strip():
        return None
    try:
        local = datetime.strptime(value.strip(), fmt)
    except ValueError as error:
        raise ValueError('时间格式无效') from error
    zone = event_zone()
    utc = local.replace(tzinfo=zone).astimezone(timezone.utc)
    if (utc.astimezone(zone).replace(tzinfo=None) != local
            or local.replace(tzinfo=zone, fold=0).utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset()):
        raise ValueError('该当地时间不存在或有歧义，请选择其他时刻')
    return utc.replace(tzinfo=None)


def event_local(value, stored_utc=True):
    if value is None or not stored_utc:
        return value
    return value.replace(tzinfo=timezone.utc).astimezone(event_zone()).replace(tzinfo=None)


def format_event_time(value, stored_utc=True, fmt='%Y-%m-%d %H:%M'):
    local = event_local(value, stored_utc)
    return local.strftime(fmt) if local is not None else ''


def window_state(start, end, stored_utc, now=None):
    if start is None or end is None:
        return 'missing'
    if start >= end:
        return 'invalid'
    if not stored_utc:
        return 'legacy'
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return 'expired' if now >= end else 'ready'


def window_warnings(season):
    descriptions = {'missing': '缺少起止时间', 'invalid': '结束时间须晚于开始时间',
                    'legacy': '旧日期时区待核对', 'expired': '已过期'}
    warnings = {}
    for key, title in (('registration', '报名日期'), ('tournament', '比赛日期')):
        state = window_state(getattr(season, f'{key}_start'), getattr(season, f'{key}_end'),
                             season.window_times_utc)
        warnings[key] = f'{title}{descriptions[state]}' if state in descriptions else ''
    return warnings
