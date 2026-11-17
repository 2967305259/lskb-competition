"""Season ownership, immutable scoring context, atomic transitions and correction service."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import uuid

from flask import abort, current_app, g, jsonify, render_template, request
from flask_login import current_user, login_required
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from backup import create_backup, maintenance_lock, prune_backups
from season_phase import (FINISHED_SCHEDULING_ACTIONS, REGISTRATION_ACTIONS, admin_schedule_protected, has_enabled_track,
                          registration_denial, window_warnings)

_correction = ContextVar('season_correction', default=None)
_reconcile = ContextVar('season_reconciliation', default=False)


class SeasonError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def ns():
    import app
    return app


def json_dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def active_season(required=True):
    season = ns().Tournament.get_active()
    if season is None and required:
        raise SeasonError('没有活跃赛季，请先显式初始化')
    return season


def require_writable(tournament_id):
    season = active_season()
    if tournament_id is None or tournament_id != season.id:
        raise SeasonError('往届或归属待核对的数据不允许通过普通入口修改', 403)
    return season


def correction_allowed(tid):
    return _correction.get() == tid


def root_match(match):
    seen = set()
    while match.parent_id:
        if match.id in seen or match.parent is None:
            raise SeasonError('比赛分段归属异常，需人工核对')
        seen.add(match.id)
        if match.tournament_id != match.parent.tournament_id:
            raise SeasonError('父子比赛赛季冲突')
        match = match.parent
    return match


def verify_match_ownership(match):
    a = ns()
    match = root_match(match)
    if match.tournament_id is None:
        raise SeasonError('比赛缺少赛季归属，须先核对')
    for side in ('a', 'b'):
        team_id = getattr(match, 'team_' + side + '_id')
        team = a.db.session.get(a.Team, team_id) if team_id else None
        if team is None or team.tournament_id != match.tournament_id:
            raise SeasonError('比赛与队伍归属不一致，须先核对')
        member = a.TeamMember.query.filter_by(team_id=team_id, user_id=getattr(match, 'player_' + side + '_id')).first()
        if member is None:
            raise SeasonError('比赛选手不在该届队伍成员记录中')
    return match


def scoring_rule(roguelike):
    return {'version': 1, 'minimum': roguelike.difficulty_min, 'maximum': roguelike.difficulty_max,
            'step': str(roguelike.difficulty_step), 'rounding': 'truncate_toward_zero',
            'track_id': roguelike.id, 'track_key': roguelike.key}


def multiplier(rule, difficulty):
    try:
        difficulty = int(difficulty)
        if not int(rule['minimum']) <= difficulty <= int(rule['maximum']):
            raise SeasonError('难度超出该比赛历史规则区间', 400)
        step = Decimal(str(rule['step']))
        value = Decimal(1) + (difficulty - int(rule['minimum'])) * step
        if not step.is_finite() or not value.is_finite() or step < 0 or not 0 <= value < 10000:
            raise SeasonError('倍率参数无效', 400)
        return value.quantize(Decimal('.0001'))
    except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
        if isinstance(exc, SeasonError):
            raise
        raise SeasonError('历史难度规则无效，须核对', 400) from exc


def capture_match_rule(match):
    """Only a genuinely new/unscored active match can read the current global rule."""
    a = ns()
    match = verify_match_ownership(match)
    if match.scoring_rule:
        return json.loads(match.scoring_rule)
    require_writable(match.tournament_id)
    if match.settlement_state == 'legacy' or match.player_a_score is not None or match.player_b_score is not None:
        raise SeasonError(f'比赛 {match.id} 已有成绩但无历史规则，请先运行核对报告')
    rl = a.Roguelike.query.filter_by(key=match.roguelike).first()
    if rl is None:
        raise SeasonError('比赛赛道不存在')
    season = a.db.session.get(a.Tournament, match.tournament_id)
    save_context(season)
    match.context_version = season.snapshot_version
    rule = scoring_rule(rl)
    rule['context_version'] = match.context_version
    match.scoring_rule = json_dump(rule)
    return rule


def set_score(match, side, raw, difficulty, origin='submitted'):
    rule = json.loads(match.scoring_rule) if match.scoring_rule else capture_match_rule(match)
    try:
        score = int(raw)
    except (TypeError, ValueError) as error:
        raise SeasonError('成绩必须为整数（0 分有效）', 400) from error
    if str(raw).strip() != str(score) or abs(score) > 1000000000:
        raise SeasonError('成绩必须为有效整数', 400)
    setattr(match, 'player_' + side + '_difficulty', int(difficulty))
    setattr(match, 'player_' + side + '_multiplier', multiplier(rule, difficulty))
    setattr(match, 'player_' + side + '_score', score)
    setattr(match, 'score_origin_' + side, origin)


def final_score(match, side):
    raw = getattr(match, 'player_' + side + '_score')
    mult = getattr(match, 'player_' + side + '_multiplier')
    if raw is None or mult is None or not match.scoring_rule:
        raise SeasonError(f'比赛 {match.id} 结算资料/历史倍率缺失，禁止按当前规则猜测')
    return int(Decimal(raw) * Decimal(mult))


def build_context(season):
    a = ns()
    settings = a.Setting.query.first()
    return {'version': 1, 'name': season.name, 'season_number': season.season_number,
            'bonus_score': season.bonus_score, 'team_size': settings.team_size if settings and settings.team_size else current_app.config['TEAM_SIZE'],
            'positions': a.get_team_positions(), 'labels': {k: current_app.config[v] for k, v in (
                ('track', 'TRACK_LABEL'), ('field1', 'MEMBER_FIELD1_LABEL'), ('field2', 'MEMBER_FIELD2_LABEL'))},
            'advance_count': settings.advance_count if settings else 0,
            'score_contract': 'whole-match;confirmed-settlement;ties-inclusive;competition-ranking',
            'tracks': {r.key: {'id': r.id, 'key': r.key, 'name': r.name, 'rule': scoring_rule(r), 'active': r.is_active}
                       for r in a.Roguelike.query.order_by(a.Roguelike.id).all()},
            'rules': [{'id': r.id, 'title': r.title, 'content': r.content, 'track': r.roguelike_key}
                      for r in a.Rule.query.filter_by(is_published=True).order_by(a.Rule.sort_order, a.Rule.id).all()]}


def save_context(season):
    context = build_context(season)
    old = json.loads(season.context_snapshot) if season.context_snapshot else {}
    comparable = {key: value for key, value in old.items() if key != 'captured_at'}
    if context != comparable:
        context['captured_at'] = datetime.utcnow().isoformat() + 'Z'
        season.context_snapshot = json_dump(context)
        season.snapshot_version = (season.snapshot_version or 0) + 1
        ns().db.session.add(ns().SeasonContextVersion(tournament_id=season.id, version=season.snapshot_version, context=season.context_snapshot))
    return json.loads(season.context_snapshot)


def historical_context(season):
    if not season.context_snapshot:
        raise SeasonError(f'第 {season.season_number} 届历史规则尚未核对，禁止使用当前配置替代')
    return json.loads(season.context_snapshot)


def unfinished_matches(tid):
    a = ns()
    return a.Match.query.filter(a.Match.tournament_id == tid, a.Match.parent_id.is_(None),
                                a.Match.match_type == 'match', a.Match.settlement_state != 'settled').order_by(a.Match.id).all()


def _signer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='new-season-v1')


def transition_preview(season):
    if season is None:
        return None
    return {'token': _signer().dumps({'key': uuid.uuid4().hex, 'expected': season.id, 'actor': current_user.id}),
            'expected': season.id, 'unfinished': unfinished_matches(season.id),
            'date_warnings': window_warnings(season), 'has_enabled_track': has_enabled_track()}


def _log(action, detail):
    a = ns()
    a.log_system_action(action, json_dump(detail))


def switch_season():
    a = ns()
    try:
        payload = _signer().loads(request.form.get('idempotency_key', ''))
    except (BadSignature, SignatureExpired) as error:
        raise SeasonError('换季请求令牌无效或过期，请刷新设置页', 400) from error
    if payload['actor'] != current_user.id:
        raise SeasonError('请求令牌不属于当前操作者', 403)
    previous = a.SeasonTransition.query.filter_by(request_key=payload['key']).first()
    if previous:
        return a.db.session.get(a.Tournament, previous.new_season_id)
    try:
        _signer().loads(request.form.get('idempotency_key', ''), max_age=86400)
    except SignatureExpired as error:
        raise SeasonError('换季请求已过期，请刷新页面', 400) from error
    season = active_season()
    if request.form.get('expected_current_season_id', type=int) != season.id or payload['expected'] != season.id:
        raise SeasonError('当前赛季已变化，请刷新页面；旧请求不会再次换季')
    pending = unfinished_matches(season.id)
    reason = request.form.get('force_reason', '').strip()
    forced = request.form.get('force') == '1'
    if pending and (not forced or not reason):
        raise SeasonError('存在未结算整场：' + ', '.join(str(m.id) for m in pending) + '；强制换季须显式勾选并填写原因')
    if not season.context_snapshot and (a.Team.query.filter_by(tournament_id=season.id).first() or a.Match.query.filter_by(tournament_id=season.id).first()):
        raise SeasonError('该届历史上下文尚未核对，先通过 data-report 和人工确认方案建立基准')
    number = season.season_number + 1
    name = request.form.get('season_name', '').strip() or f"{current_app.config['EVENT_NAME']} #{number}"
    if len(name) > 100 or a.Tournament.query.filter(a.db.or_(a.Tournament.name == name, a.Tournament.season_number == number)).first():
        raise SeasonError('新赛季名称或届数无效/已存在', 400)
    # Close the read transaction before acquiring the consistent backup connection.
    old_id, old_bonus = season.id, season.bonus_score
    a.db.session.rollback()
    batch = create_backup(a.db.engine, current_app.config, f'new-season:{old_id}')
    season = a.db.session.get(a.Tournament, old_id)
    if active_season().id != old_id:
        raise SeasonError('赛季已切换，请刷新')
    # The write lock is held from before_request until teardown, including backup, audit and commit.
    save_context(season)
    # Freeze the known current rule for genuinely unscored unfinished matches before archiving.
    # Existing legacy scores with unknown provenance remain explicitly unresolved.
    for unfinished in unfinished_matches(old_id):
        if unfinished.settlement_state == 'pending' and not unfinished.scoring_rule:
            capture_match_rule(unfinished)
    settings = a.Setting.get_instance()
    inherit_registration = request.form.get('inherit_registration_window') == '1'
    inherit_tournament = request.form.get('inherit_tournament_window') == '1'
    inherit_advance = request.form.get('inherit_advance_count') == '1'
    inherit_team_size = request.form.get('inherit_team_size') == '1'
    inherit_incomplete = request.form.get('inherit_allow_incomplete_teams') == '1'
    inherit_carousel = request.form.get('inherit_carousel') == '1'
    count = a.User.query.filter(a.User.role.notin_(['admin', 'super_admin'])).count()
    pending_ids = [m.id for m in unfinished_matches(old_id)]
    from sqlalchemy import update
    with a.db.session.no_autoflush:
        changed = a.db.session.execute(update(a.Tournament).where(
            a.Tournament.id == old_id, a.Tournament.is_active.is_(True)).values(is_active=False),
            execution_options={'synchronize_session': False}).rowcount
    if changed != 1:
        raise SeasonError('赛季状态已改变，本次请求不会创建下一届')
    season.is_active = False
    season.status = 'finished'
    season.archived_at = datetime.utcnow()
    new = a.Tournament(name=name, season_number=number, is_active=True, status='registration', bonus_score=old_bonus)
    if inherit_registration:
        new.registration_start = season.registration_start
        new.registration_end = season.registration_end
    if inherit_tournament:
        new.tournament_start = season.tournament_start
        new.tournament_end = season.tournament_end
    new.window_times_utc = season.window_times_utc
    a.db.session.add(new)
    a.db.session.flush()
    teams = [t.id for t in a.Team.query.filter_by(tournament_id=old_id).all()]
    if teams:
        a.Invitation.query.filter(a.Invitation.team_id.in_(teams), a.Invitation.status == 'pending').delete(synchronize_session=False)
        a.JoinRequest.query.filter(a.JoinRequest.team_id.in_(teams), a.JoinRequest.status == 'pending').delete(synchronize_session=False)
    a.User.query.filter(a.User.role.notin_(['admin', 'super_admin'])).update({'status': 0}, synchronize_session=False)
    a.TimePreference.query.delete(synchronize_session=False)
    old_advance_count = settings.advance_count
    old_team_size = settings.team_size
    old_allow_incomplete = settings.allow_incomplete_teams
    settings.advance_count = old_advance_count if inherit_advance else 0
    # These settings remain global for backward compatibility.  Reading and
    # writing the selected values explicitly makes the inheritance decision
    # auditable while preserving the established unchecked behavior.
    if inherit_team_size:
        settings.team_size = old_team_size
    if inherit_incomplete:
        settings.allow_incomplete_teams = old_allow_incomplete
    settings.registration_open = has_enabled_track()
    if inherit_carousel:
        previous_items = a.CarouselItem.query.filter_by(tournament_id=old_id).order_by(
            a.CarouselItem.sort_order, a.CarouselItem.id).all()
        for item in previous_items:
            a.db.session.add(a.CarouselItem(
                tournament_id=new.id, media_type=item.media_type,
                file_path=item.file_path, title=item.title, link_url=item.link_url,
                sort_order=item.sort_order, is_active=item.is_active,
            ))
    inherited = ['bonus_score']
    if inherit_registration:
        inherited.append('registration_window')
    if inherit_tournament:
        inherited.append('tournament_window')
    if inherit_advance:
        inherited.append('advance_count')
    if inherit_team_size:
        inherited.append('team_size')
    if inherit_incomplete:
        inherited.append('allow_incomplete_teams')
    if inherit_carousel:
        inherited.append('carousel')
    a.db.session.add(a.SeasonTransition(request_key=payload['key'], expected_season_id=old_id,
        new_season_id=new.id, actor_id=current_user.id, backup_batch=batch.name,
        forced_reason=reason if forced else '', unfinished_ids=json_dump(pending_ids)))
    _log('new_season', {'old_id': old_id, 'new_id': new.id, 'name': name,
        'reset_users': count, 'unfinished_matches': pending_ids, 'force_reason': reason if forced else '',
        'backup_batch': batch.name, 'inherited': inherited,
        'carousel_copied': a.CarouselItem.query.filter_by(tournament_id=new.id).count(),
        'inherited_values': {
            'registration_start': str(new.registration_start) if inherit_registration else None,
            'registration_end': str(new.registration_end) if inherit_registration else None,
            'tournament_start': str(new.tournament_start) if inherit_tournament else None,
            'tournament_end': str(new.tournament_end) if inherit_tournament else None,
            'advance_count': settings.advance_count,
            'team_size': settings.team_size,
            'allow_incomplete_teams': bool(settings.allow_incomplete_teams),
            'registration_open': settings.registration_open,
        }})
    save_context(new)
    a.db.session.commit()
    # Retention is housekeeping after the atomic operation; failure cannot turn success into a retry.
    try:
        prune_backups(current_app.config)
    except OSError:
        current_app.logger.exception('备份保留数清理失败，完整备份仍保留')
    return new


def _guard_request():
    a = ns()
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return
    g.maintenance = maintenance_lock(current_app.config)
    g.maintenance.__enter__()
    if not current_user.is_authenticated:
        return
    endpoint = request.endpoint or ''
    if not endpoint.startswith(('admin.', 'captain.', 'player.', 'tournament.')):
        return
    # Historical correction has its own authorization and bounded context.
    if endpoint == 'p0_correct_history':
        return
    args = request.view_args or {}
    match_id = args.get('match_id') or request.form.get('match_id', type=int) or request.form.get('parent_id', type=int)
    if match_id:
        match = a.db.session.get(a.Match, match_id)
        if match is None:
            abort(404)
        root = root_match(match)
        require_writable(root.tournament_id)
        verify_match_ownership(root)
        if endpoint in ('admin.schedule_update_time', 'admin.schedule_split_match',
                        'admin.schedule_delete_match', 'admin.schedule_confirm_match') and admin_schedule_protected(root):
            raise SeasonError('已有成绩或结果的整场不能更改日程')
    team_id = args.get('team_id')
    if team_id:
        team = a.db.session.get(a.Team, team_id)
        if team is None:
            abort(404)
        require_writable(team.tournament_id)
        if endpoint == 'admin.delete_team' and a.ScoreAdjustment.query.join(a.TeamMember, a.ScoreAdjustment.member_id == a.TeamMember.id).filter(a.TeamMember.team_id == team.id).first():
            raise SeasonError('该队伍被积分调整账本引用，不能删除')
        if endpoint == 'admin.delete_team' and a.Match.query.filter(a.db.or_(a.Match.team_a_id == team.id, a.Match.team_b_id == team.id)).first():
            raise SeasonError('已有比赛或审计引用的队伍不可删除')
    for key, model in (('invitation_id', a.Invitation), ('request_id', a.JoinRequest)):
        if key in args:
            item = a.db.session.get(model, args[key])
            if item is None or item.team is None:
                abort(404)
            owner = item.user_id if endpoint.startswith('player.') else item.team.captain_id
            if owner != current_user.id:
                abort(403)
            require_writable(item.team.tournament_id)
    if 'item_id' in args:
        item = a.db.session.get(a.CarouselItem, args['item_id'])
        if item is None:
            abort(404)
        require_writable(item.tournament_id)
    if endpoint == 'admin.delete_user':
        user_id = args['user_id']
        if a.ScoreAdjustment.query.filter(a.db.or_(a.ScoreAdjustment.actor_id == user_id, a.ScoreAdjustment.revoked_by == user_id)).first():
            raise SeasonError('该用户被积分调整审计引用，不能删除')
        member_ids = [m.id for m in a.TeamMember.query.filter_by(user_id=user_id).all()]
        if member_ids and a.ScoreAdjustment.query.filter(a.ScoreAdjustment.member_id.in_(member_ids)).first():
            raise SeasonError('该成员被积分调整账本引用，不能删除')
        historical = a.TeamMember.query.join(a.Team).filter(a.TeamMember.user_id == user_id,
                         a.db.or_(a.Team.tournament_id != active_season().id, a.Team.tournament_id.is_(None))).first()
        owned = a.Team.query.filter(a.Team.captain_id == user_id,
                         a.db.or_(a.Team.tournament_id != active_season().id, a.Team.tournament_id.is_(None))).first()
        if historical or owned:
            raise SeasonError('该账号有关联往届/待核对成员或队伍，不能删除')
    if endpoint.startswith('admin.') and endpoint not in ('admin.new_season', 'admin.create_backup_batch'):
        active_season()
    if endpoint in FINISHED_SCHEDULING_ACTIONS and active_season().status == 'finished':
        raise SeasonError('本届赛事已结束，已停止排赛')
    if endpoint.startswith('captain.') and endpoint not in ('captain.upload_avatar',):
        team = current_user.team
        if team:
            require_writable(team.tournament_id)
    if endpoint in ('player.select_roguelike', 'player.declaration', 'player.profile', 'player.time_preferences'):
        member = current_user.get_team_membership()
        if member:
            require_writable(member.team.tournament_id)
    if endpoint in REGISTRATION_ACTIONS:
        reason = registration_denial(active_season(required=False))
        if reason:
            raise SeasonError(reason, 403)
    if endpoint in ('admin.edit_roguelike', 'admin.delete_roguelike', 'admin.edit_rule', 'admin.delete_rule'):
        # Never discard the only surviving source of a legacy season's unknown rules.
        if a.Tournament.query.filter_by(is_active=False, context_snapshot=None).first():
            raise SeasonError('存在未核对的往届规则，请先保存人工确认的历史上下文')
    if endpoint in ('admin.add_roguelike', 'admin.edit_roguelike'):
        rl = a.db.session.get(a.Roguelike, args['rl_id']) if 'rl_id' in args else None
        try:
            minimum = int(request.form.get('difficulty_min', rl.difficulty_min if rl else 12))
            maximum = int(request.form.get('difficulty_max', rl.difficulty_max if rl else 18))
            step = request.form.get('difficulty_step', str(rl.difficulty_step) if rl else '0.1')
            if not 0 <= minimum <= maximum <= 10000:
                raise SeasonError('难度区间无效', 400)
            multiplier({'minimum': minimum, 'maximum': maximum, 'step': step}, maximum)
        except (ValueError, TypeError) as exc:
            raise SeasonError('难度配置必须为有限有效数值', 400) from exc
    if endpoint in ('admin.edit_roguelike', 'admin.delete_roguelike'):
        rl = a.db.session.get(a.Roguelike, args['rl_id'])
        if rl is None:
            abort(404)
        referenced = (a.Match.query.filter_by(roguelike=rl.key).first() or a.TeamMember.query.filter_by(roguelike=rl.key).first())
        for snapshot in a.SeasonContextVersion.query.all():
            referenced = referenced or rl.key in json.loads(snapshot.context).get('tracks', {})
        if referenced and (endpoint == 'admin.delete_roguelike' or request.form.get('key', rl.key).strip() != rl.key):
            raise SeasonError('已引用赛道 key 不可修改或删除，请改为停用')
        for match in a.Match.query.filter_by(roguelike=rl.key, parent_id=None).all():
            if (match.player_a_score is not None or match.player_b_score is not None) and not match.scoring_rule:
                raise SeasonError(f'比赛 {match.id} 的历史倍率尚未核对，禁止先改全局难度')


@event.listens_for(Session, 'before_commit')
def _freeze_context_on_config_edit(session):
    """Publish the active context in the same transaction as its source configuration."""
    from flask import has_app_context
    if not has_app_context():
        return
    a = ns()
    changed = list(session.new) + list(session.dirty) + list(session.deleted)
    if not any(isinstance(obj, (a.Rule, a.Roguelike, a.Setting, a.Tournament)) for obj in changed):
        return
    with session.no_autoflush:
        for obj in changed:
            if isinstance(obj, a.Tournament) and obj.id and not obj.is_active:
                state = inspect(obj)
                if not state.attrs.is_active.history.has_changes() and not _reconcile.get():
                    for key in ('name', 'bonus_score', 'context_snapshot', 'snapshot_version'):
                        if state.attrs[key].history.has_changes():
                            raise SeasonError('往届上下文不能通过普通提交改写')
        # Recheck after flush so pending new tournaments cannot hide a second active row.
        session.flush()
        season = active_season(required=False)
        if season:
            save_context(season)


def install_safety(app):
    app.before_request(_guard_request)

    @app.before_request
    def read_history_context():
        season = None
        if request.endpoint == 'main.history_detail':
            season = ns().db.session.get(ns().Tournament, request.view_args['tournament_id'])
        elif request.endpoint == 'tournament.match_detail':
            match = ns().db.session.get(ns().Match, request.view_args['match_id'])
            season = match.tournament if match else None
        if season:
            if season and not season.is_active:
                if season.context_snapshot:
                    g.historical_context = historical_context(season)
                else:
                    # Preserve access to known old results, and clearly identify missing provenance.
                    g.historical_context = {'name': season.name, 'labels': {'track': '赛道', 'field1': '字段一', 'field2': '字段二'},
                                            'team_size': 0, 'tracks': {}, 'rules': [], 'unverified': True}

    @app.errorhandler(SeasonError)
    def invalid_season(error):
        ns().db.session.rollback()
        if request.endpoint in ('admin.schedule_update_time', 'admin.schedule_split_match', 'admin.schedule_delete_match'):
            return jsonify(error=str(error)), error.status
        # Error rendering must not query the broken season state again via context processors.
        return current_app.jinja_env.get_template('season_error.html').render(message=str(error)), error.status

    @app.teardown_request
    def release_write_lock(error):
        ns().db.session.rollback()
        manager = g.pop('maintenance', None)
        if manager:
            manager.__exit__(None, None, None)

    @app.route('/admin/history-corrections/<int:tid>', methods=['GET', 'POST'], endpoint='p0_correct_history')
    @login_required
    def correct_history(tid):
        if not current_user.is_super_admin():
            abort(403)
        a = ns()
        season = a.Tournament.query.get_or_404(tid)
        if season.is_active:
            raise SeasonError('此入口仅用于往届专项纠错')
        matches = a.Match.query.filter_by(tournament_id=tid, parent_id=None).all()
        if request.method == 'GET':
            return render_template('admin/history_correction.html', season=season, matches=matches)
        reason = request.form.get('reason', '').strip()
        if not reason:
            raise SeasonError('专项纠错必须填写原因', 400)
        historical_context(season)
        match = a.Match.query.get_or_404(request.form.get('match_id', type=int))
        if root_match(match).id != match.id or match.tournament_id != tid:
            raise SeasonError('目标不属于该届正式对阵', 400)
        verify_match_ownership(match)
        if not match.scoring_rule or match.settlement_state == 'legacy':
            raise SeasonError('缺少已确认的历史规则基准，须先人工核对')
        before = audit_state(match)
        token = _correction.set(tid)
        try:
            for side in ('a', 'b'):
                raw = request.form.get('score_' + side)
                diff = request.form.get('diff_' + side)
                if raw not in (None, '') or diff not in (None, ''):
                    set_score(match, side, raw if raw not in (None, '') else getattr(match, 'player_' + side + '_score'),
                              diff if diff not in (None, '') else getattr(match, 'player_' + side + '_difficulty'),
                              getattr(match, 'score_origin_' + side) or 'corrected')
            action = request.form.get('action')
            if request.form.get('confirm_settlement') == '1' and match.settlement_state != 'settled':
                final_score(match, 'a')
                final_score(match, 'b')
                apply_outcome(match, action)
                match.settlement_state = 'settled'
                match.settled_at = datetime.utcnow()
                match.finished_at = match.finished_at or datetime.utcnow()
            elif action and match.settlement_state == 'settled':
                apply_outcome(match, action)
            from scoring import recalculate_all_scores
            recalculate_all_scores(tid)
            after = audit_state(match)
            a.db.session.add(a.ScoreLog(match_id=match.id, modified_by=current_user.id,
                old_result=json_dump(before), new_result=json_dump(after), reason=reason))
            _log('historical_correction', {'season': tid, 'match': match.id, 'operator': current_user.id,
                                          'reason': reason, 'before': before, 'after': after})
            a.db.session.commit()
        finally:
            _correction.reset(token)
        return jsonify({'corrected': match.id, 'tournament_id': tid})


    @app.route('/admin/adjustments/<int:adjustment_id>/revoke', methods=['POST'], endpoint='p0_revoke_adjustment')
    @login_required
    def revoke_adjustment(adjustment_id):
        a = ns()
        if not current_user.is_admin():
            abort(403)
        adjustment = a.ScoreAdjustment.query.get_or_404(adjustment_id)
        require_writable(adjustment.tournament_id)
        reason = request.form.get('reason', '').strip()
        if not reason:
            raise SeasonError('撤销调整必须填写原因', 400)
        if not adjustment.revoked:
            from scoring import get_member_score_breakdown
            before = get_member_score_breakdown(adjustment.member_id)
            adjustment.revoked = True
            adjustment.revoked_by = current_user.id
            adjustment.revoke_reason = reason
            a.db.session.flush()
            from scoring import recalculate_all_scores
            recalculate_all_scores(adjustment.tournament_id)
            after = get_member_score_breakdown(adjustment.member_id)
            _log('revoke_adjustment', {
                'id': adjustment.id, 'delta': adjustment.delta,
                'tournament': adjustment.tournament_id,
                'member': adjustment.member_id, 'reason': reason,
                'operator': current_user.id, 'before': before, 'after': after,
            })
            a.db.session.commit()
        return jsonify({'revoked': adjustment.id})

    @app.route('/static/uploads/<path:filename>', endpoint='p0_media')
    def media(filename):
        from pathlib import Path
        from flask import send_from_directory
        root = Path(current_app.config['UPLOAD_ROOT']).resolve()
        target = (root / filename).resolve()
        if not target.is_relative_to(root):
            abort(404)
        return send_from_directory(root, filename)


def audit_state(match):
    a = ns()
    return {'match': {key: str(getattr(match, key)) if getattr(match, key) is not None else None for key in (
        'id', 'tournament_id', 'player_a_score', 'player_b_score', 'player_a_difficulty', 'player_b_difficulty',
        'player_a_multiplier', 'player_b_multiplier', 'scoring_rule', 'winner_id', 'status', 'settlement_state',
        'forfeit_side', 'forfeit_reason', 'score_origin_a', 'score_origin_b')},
        'teams': {str(t.id): t.team_score for t in a.Team.query.filter_by(tournament_id=match.tournament_id).all()},
        'members': {str(m.id): m.final_score for m in a.TeamMember.query.join(a.Team).filter(a.Team.tournament_id == match.tournament_id).all()}}


def apply_outcome(match, action):
    if match.forfeit_side:
        # Preserve forfeiture provenance when confirming or correcting raw scores.
        forfeited = match.forfeit_side
        match.status = 'forfeit_' + forfeited
        match.winner_id = match.player_b_id if forfeited == 'a' else match.player_a_id
        match.player_a_result = 'forfeit' if forfeited == 'a' else 'win'
        match.player_b_result = 'forfeit' if forfeited == 'b' else 'win'
    elif action in ('win_a', 'win_b', 'draw'):
        match.status = 'finished'
        match.winner_id = match.player_a_id if action == 'win_a' else match.player_b_id if action == 'win_b' else None
        match.player_a_result = 'win' if action == 'win_a' else 'lose' if action == 'win_b' else 'draw'
        match.player_b_result = 'win' if action == 'win_b' else 'lose' if action == 'win_a' else 'draw'
    else:
        raise SeasonError('比赛结果无效', 400)


@event.listens_for(Session, 'before_flush')
def protect_audit_evidence(session, flush_context, instances):
    from flask import has_app_context
    if not has_app_context():
        return
    a = ns()
    if any(isinstance(obj, (a.ScoreLog, a.ScoreAdjustment, a.SeasonContextVersion)) for obj in session.deleted):
        raise SeasonError('成绩审计凭证不可删除')
    for obj in session.dirty:
        if isinstance(obj, a.ScoreAdjustment):
            state = inspect(obj)
            immutable = ('tournament_id', 'member_id', 'actor_id', 'delta', 'reason', 'created_at')
            if any(state.attrs[name].history.has_changes() for name in immutable):
                raise SeasonError('人工调整记录的赛季、成员、操作人、分值和原因不可改写')
        if isinstance(obj, (a.ScoreLog, a.SeasonContextVersion)):
            raise SeasonError('成绩审计凭证不可改写')
