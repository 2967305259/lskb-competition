"""Account privacy, personal copies and authorization for uploaded media."""
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import secrets

from flask import abort, current_app, request, send_from_directory, send_file, url_for
from flask_login import current_user

from backup import maintenance_lock
from formal_match import outcome_for_player
from season_safety import SeasonError, json_dump, match_scope_of, ns, root_match

EVIDENCE_LINE = re.compile(r'^\[截图\][^\r\n]*/static/uploads/screenshots/([A-Za-z0-9_.-]+)\s*$')
STAMPED_EVIDENCE = re.compile(r'screenshot_(\d{1,10})_(\d{1,10})_[0-9a-f]{32}\.[a-z0-9]{2,5}')


def screenshot_names(note):
    return list(dict.fromkeys(match[1] for line in (note or '').splitlines()
                             if (match := EVIDENCE_LINE.fullmatch(line))))


def can_read_evidence(filename):
    if not current_user.is_authenticated:
        return False
    a = ns()
    if stamp := STAMPED_EVIDENCE.fullmatch(filename):
        match = a.db.session.get(a.Match, int(stamp[1]))
        if match is None or filename not in screenshot_names(match.match_note):
            return False
        try:
            root = root_match(match)
        except SeasonError:
            return False
        participants = (root.player_a_id, root.player_b_id)
        if int(stamp[2]) not in participants:
            return False
        return current_user.is_admin() or (current_user.id in participants and match_scope_of(root) != 'orphan')
    # Legacy filenames may lack a trustworthy owning match and are reviewed by admins.
    if not current_user.is_admin():
        return False
    matches = a.Match.query.with_entities(a.Match.match_note).filter(a.Match.match_note.contains(filename, autoescape=True))
    return any(filename in screenshot_names(note) for (note,) in matches)


def can_read_portrait(folder, filename):
    a = ns()
    if folder == 'teams':
        return (a.Team.query.join(a.Tournament).filter(a.Team.team_photo == filename,
                a.Tournament.is_active.is_not(None)).first() is not None)
    owner = a.User.query.filter_by(avatar=filename, anonymized_at=None)
    if current_user.is_authenticated:
        if current_user.is_admin() and owner.first() is not None:
            return True
        if owner.filter(a.User.id == current_user.id).first() is not None:
            return True
    ids = owner.with_entities(a.User.id)
    return (a.TeamMember.query.join(a.Team).join(a.Tournament).filter(
        a.TeamMember.user_id.in_(ids.statement), a.Tournament.is_active.is_not(None)).first() is not None
        or a.Team.query.join(a.Tournament).filter(a.Team.captain_id.in_(ids.statement),
                                                a.Tournament.is_active.is_not(None)).first() is not None)


def protected_upload(filename):
    path = PurePosixPath(filename)
    if len(path.parts) not in (1, 2) or path.is_absolute() or '\\' in filename or '\0' in filename or '..' in path.parts:
        return '', 404
    folder, name = (path.parts if len(path.parts) == 2 else ('', path.name))
    root = Path(current_app.config['UPLOAD_ROOT']).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root / folder) or not target.is_file():
        return '', 404
    if folder == 'screenshots':
        permitted = can_read_evidence(name)
    elif folder in ('avatars', 'teams'):
        permitted = can_read_portrait(folder, name)
    elif folder in ('', 'carousel'):
        permitted = can_read_carousel(str(path))
    else:
        permitted = False
    if not permitted:
        return '', 404
    response = send_from_directory(root, filename)
    response.cache_control.private = True
    response.cache_control.no_store = True
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def can_read_carousel(path):
    a = ns()
    if PurePosixPath(path).suffix.lower() not in a.SUPPORTED_MEDIA_EXTENSIONS:
        return False
    entries = a.CarouselItem.query.filter_by(file_path='/static/uploads/' + path)
    if current_user.is_authenticated and current_user.is_admin():
        return entries.first() is not None
    return entries.join(a.Tournament).filter(a.Tournament.is_active.is_(True),
                                             a.CarouselItem.is_active.is_(True)).first() is not None


def _target_is_referenced(user):
    a = ns()
    uid = user.id
    checks = (
        a.Team.query.filter_by(captain_id=uid), a.TeamMember.query.filter_by(user_id=uid),
        a.Match.query.filter(a.db.or_(a.Match.player_a_id == uid, a.Match.player_b_id == uid, a.Match.winner_id == uid)),
        a.Invitation.query.filter_by(user_id=uid), a.JoinRequest.query.filter_by(user_id=uid),
        a.ScoreLog.query.filter_by(modified_by=uid), a.SystemLog.query.filter_by(user_id=uid),
        a.ScoreAdjustment.query.filter(a.db.or_(a.ScoreAdjustment.actor_id == uid, a.ScoreAdjustment.revoked_by == uid)),
        a.ScoreRuleSet.query.filter_by(published_by_id=uid),
        a.SeasonTransition.query.filter_by(actor_id=uid),
    )
    return any(query.first() is not None for query in checks)


def delete_unreferenced_account(user):
    a = ns()
    if user.id == current_user.id or user.is_super_admin() or (user.is_admin() and not current_user.is_super_admin()):
        abort(403)
    if user.anonymized_at or _target_is_referenced(user):
        raise SeasonError('该账号已有赛事或审计引用，请由超级管理员按申请执行匿名化')
    uid = user.id
    a.TimePreference.query.filter_by(player_id=uid).delete()
    a.db.session.delete(user)
    a.log_system_action('delete_user', json_dump({'subject_id': uid}))
    a.db.session.commit()


def anonymize_account(user, reference):
    a = ns()
    if user.id == current_user.id or user.is_super_admin():
        abort(403)
    if user.anonymized_at is not None:
        raise SeasonError('该账号已匿名化')
    alias = f'注销选手-{user.id}'
    while a.User.query.filter_by(nickname=alias).first():
        alias = f'注销选手-{secrets.token_hex(6)}'
    disabled_username = 'closed-' + secrets.token_hex(6)
    while a.User.query.filter_by(username=disabled_username).first():
        disabled_username = 'closed-' + secrets.token_hex(6)
    uid = user.id
    user.username = disabled_username
    user.nickname = alias
    user.set_password(secrets.token_urlsafe(32))
    user.avatar = None
    user.role = 'player'
    user.status = 0
    user.tutorial_step = 0
    user.has_completed_onboarding = False
    user.anonymized_at = datetime.utcnow()
    a.TimePreference.query.filter_by(player_id=uid).delete()
    a.TeamMember.query.filter_by(user_id=uid).update({'declaration': ''})
    for model in (a.Invitation, a.JoinRequest):
        model.query.filter_by(user_id=uid, status='pending').update({'status': 'rejected'})
    a.log_system_action('anonymize_user', json_dump({'subject_id': uid, 'request_reference': reference}))
    a.db.session.commit()


def request_reference(value):
    value = (value or '').strip()
    if not value or len(value) > 200:
        raise SeasonError('请填写申请编号或办理依据，最多 200 字', 400)
    return value


def _personal_record(user):
    a = ns()
    uid = user.id
    memberships = a.TeamMember.query.filter_by(user_id=uid).order_by(a.TeamMember.id)
    matches = a.Match.query.filter(a.Match.parent_id.is_(None),
        a.db.or_(a.Match.player_a_id == uid, a.Match.player_b_id == uid)).order_by(a.Match.id)
    own_matches = []
    for match in matches:
        side = 'a' if match.player_a_id == uid else 'b'
        own_matches.append({
            'match_id': match.id, 'season_id': match.tournament_id, 'track': match.roguelike,
            'status': match.status, 'settlement_state': match.settlement_state, 'result': outcome_for_player(match, uid),
            'created_at': match.created_at, 'scheduled_time': match.scheduled_time,
            'score': getattr(match, f'player_{side}_score'), 'difficulty': getattr(match, f'player_{side}_difficulty'),
            'multiplier': getattr(match, f'player_{side}_multiplier'),
            'evidence': [url_for('p0_media', filename=f'screenshots/{name}') for name in screenshot_names(match.match_note)
                         if (stamp := STAMPED_EVIDENCE.fullmatch(name)) and int(stamp[1]) == match.id and int(stamp[2]) == uid],
        })
    def applications(model):
        return [{'team_id': row.team_id, 'status': row.status, 'created_at': row.created_at, 'updated_at': row.updated_at}
                for row in model.query.filter_by(user_id=uid).order_by(model.id)]
    return {
        'version': 1, 'generated_at_utc': datetime.utcnow().isoformat() + 'Z',
        'account': {'id': uid, 'username': user.username, 'nickname': user.nickname, 'role': user.role,
                    'created_at': user.created_at, 'avatar': url_for('p0_media', filename=f'avatars/{user.avatar}') if user.avatar else None},
        'memberships': [{'season_id': member.team.tournament_id, 'team_id': member.team_id, 'team': member.team.team_name,
                         'position': member.position, 'track': member.roguelike, 'squad': member.squad,
                         'operator': member.operator, 'declaration': member.declaration, 'final_score': member.final_score}
                        for member in memberships],
        'matches': own_matches, 'invitations': applications(a.Invitation), 'join_requests': applications(a.JoinRequest),
        'time_preferences': [{'day': row.day_of_week, 'start': row.start_time, 'end': row.end_time}
                             for row in a.TimePreference.query.filter_by(player_id=uid).order_by(a.TimePreference.id)],
        'score_adjustments': [{'season_id': adj.tournament_id, 'member_id': adj.member_id, 'delta': adj.delta,
                               'revoked': adj.revoked, 'created_at': adj.created_at}
                              for adj in a.ScoreAdjustment.query.join(a.TeamMember, a.ScoreAdjustment.member_id == a.TeamMember.id)
                              .filter(a.TeamMember.user_id == uid).order_by(a.ScoreAdjustment.id)],
    }


def download_personal_record(user, reference=None):
    a = ns()
    with maintenance_lock(current_app.config):
        payload = _personal_record(user)
        a.log_system_action('download_personal_data', json_dump({'subject_id': user.id, 'request_reference': reference}))
        a.db.session.commit()
    response = send_file(BytesIO((json_dump(payload) + '\n').encode('utf-8')), mimetype='application/json',
                         as_attachment=True, download_name=f'personal-data-{user.id}.json')
    response.cache_control.no_store = True
    return response


def record_download(action, detail):
    if request.method != 'HEAD':
        a = ns()
        a.log_system_action(action, json_dump(detail))
        a.db.session.commit()
