"""Season-scoped scoring. Services never commit; callers own business + audit transactions."""
import sys
from decimal import Decimal

from formal_match import (FORMAL_MATCH_TYPE, OUTCOME_STATUSES, has_outcome, is_formal,
                          is_settled, player_query, summarize_formal_matches)


def _app_ns(*names):
    mod = sys.modules.get('app') or sys.modules['__main__']
    return [getattr(mod, name) for name in names]


def calculate_multiplier(difficulty, difficulty_min, difficulty_step):
    if difficulty is None:
        return Decimal('1.0000')
    return (Decimal(1) + (Decimal(difficulty) - Decimal(difficulty_min)) * Decimal(str(difficulty_step))).quantize(Decimal('.0001'))


def calculate_final_score(raw_score, multiplier):
    if raw_score is None:
        raise ValueError('未提交成绩不能作为 0 分结算')
    return int(Decimal(raw_score) * Decimal(str(multiplier)))


def get_team_points(team_id):
    Team, TeamMember = _app_ns('Team', 'TeamMember')
    team = Team.query.get(team_id)
    if team is not None and team.team_score is not None:
        return team.team_score, 0, 0
    return sum(m.final_score or 0 for m in TeamMember.query.filter_by(team_id=team_id)), 0, 0


def get_team_rankings(tournament_id=None):
    from season_safety import active_season
    Team, = _app_ns('Team')
    if tournament_id is None:
        season = active_season(required=False)
        if season is None:
            return []
        tournament_id = season.id
    rows = [{'team': team, 'points': get_team_points(team.id)[0], 'special': 0, 'minor': 0}
            for team in Team.query.filter_by(tournament_id=tournament_id).all()]
    rows.sort(key=lambda row: (-row['points'], row['team'].id))
    last_points, rank = None, 0
    for index, row in enumerate(rows, 1):
        if row['points'] != last_points:
            rank = index
            last_points = row['points']
        row['rank'] = rank
    return rows


def qualification(rankings, quota):
    """Use the score at the configured boundary, including every tie."""
    if not rankings or quota is None or quota <= 0:
        advanced = []
    else:
        boundary = rankings[min(quota, len(rankings)) - 1]['points']
        advanced = [row for row in rankings if row['points'] >= boundary]
    return {'quota': quota, 'count': len(advanced), 'rows': advanced,
            'team_ids': {row['team'].id for row in advanced}}


def player_record_query(player_id, tournament_id=None, scope='current'):
    """Use the same season and formal-match filters for totals and lists."""
    Match, Tournament, db = _app_ns('Match', 'Tournament', 'db')
    if scope not in ('current', 'all'):
        raise ValueError('invalid player-stat scope')
    if scope == 'current':
        from season_safety import active_season
        season = active_season(required=False)
        if season is None:
            return player_query(Match, db, player_id).filter(db.false()), None
        tournament_id = season.id if tournament_id is None else tournament_id
    query = player_query(Match, db, player_id, tournament_id)
    if scope == 'all':
        query = query.join(Tournament, Match.tournament_id == Tournament.id).filter(Tournament.is_active.is_not(None))
    return query, tournament_id


def get_player_stats(player_id, tournament_id=None, scope='current', *, recent_limit=50):
    """Aggregate all formal results before applying the separate recent-list limit."""
    Match, = _app_ns('Match')
    query, season_id = player_record_query(player_id, tournament_id, scope)
    counts = summarize_formal_matches(query, Match, player_id=player_id)
    matches = query.order_by(Match.created_at.desc(), Match.id.desc()).limit(recent_limit).all()
    return {'wins': counts['wins'], 'losses': counts['losses'], 'draws': counts['draws'],
            'total_matches': counts['decided'],
            'formal_matches': counts['formal'], 'decided_matches': counts['decided'],
            'settled_matches': counts['settled'],
            'season_scope': scope, 'season_id': season_id,
            'matches': matches}


def get_member_score_breakdown(member_id):
    """Return the reconstructable score ledger for one member.

    ``final_score`` is never treated as an independent source of truth.  The
    displayed total is always ``settled match score + active adjustments``;
    this helper is also used by the admin audit page before and after changes.
    """
    from season_safety import final_score, SeasonError
    db, Match, TeamMember, ScoreAdjustment = _app_ns(
        'db', 'Match', 'TeamMember', 'ScoreAdjustment')
    member = db.session.get(TeamMember, member_id)
    if member is None or member.team is None or member.team.tournament_id is None:
        raise SeasonError('成员缺少有效赛季归属，不能计算调整分', 409)
    tournament_id = member.team.tournament_id
    match_score = 0
    matches = Match.query.filter_by(tournament_id=tournament_id).filter(db.or_(
        db.and_(Match.team_a_id == member.team_id, Match.player_a_id == member.user_id),
        db.and_(Match.team_b_id == member.team_id, Match.player_b_id == member.user_id),
    ))
    for match in matches:
        has_score = match.player_a_score is not None or match.player_b_score is not None
        if match.match_type != 'exhibition' and (
            (match.parent_id and (has_score or match.settlement_state == 'settled'))
            or (match.settlement_state == 'legacy' and (has_score or has_outcome(match)))
        ):
            raise SeasonError(f'比赛 {match.id} 的旧结算资料待核对')
        if is_formal(match) and match.settlement_state == 'settled':
            if not is_settled(match):
                raise SeasonError(f'比赛 {match.id} 结算与成绩状态冲突')
            for side in ('a', 'b'):
                if (getattr(match, 'team_' + side + '_id') == member.team_id and
                        getattr(match, 'player_' + side + '_id') == member.user_id):
                    match_score += final_score(match, side)
        elif match.match_type == 'exhibition' and match.status in OUTCOME_STATUSES:
            for side in ('a', 'b'):
                if (getattr(match, 'team_' + side + '_id') == member.team_id and
                        getattr(match, 'player_' + side + '_id') == member.user_id and
                        getattr(match, 'player_' + side + '_score') is not None):
                    match_score += final_score(match, side)
    adjustment_score = sum(row.delta for row in ScoreAdjustment.query.filter_by(
        tournament_id=tournament_id, member_id=member.id, revoked=False).all())
    return {'member_id': member.id, 'tournament_id': tournament_id,
            'match_score': match_score, 'adjustment_score': adjustment_score,
            'final_score': match_score + adjustment_score}


def create_score_adjustment(member_id, actor_id, reason, *, target_total=None, delta=None):
    """Create one immutable adjustment row without committing the transaction."""
    from season_safety import SeasonError
    db, TeamMember, ScoreAdjustment = _app_ns('db', 'TeamMember', 'ScoreAdjustment')
    reason = (reason or '').strip()
    if not reason:
        raise SeasonError('人工调整必须填写原因', 400)
    if target_total is None and delta is None:
        raise SeasonError('必须提供目标总分或调整分', 400)
    if target_total is not None and delta is not None:
        raise SeasonError('目标总分和调整分不能同时提供', 400)
    member = db.session.get(TeamMember, member_id)
    if member is None or member.team is None or member.team.tournament_id is None:
        raise SeasonError('成员缺少有效赛季归属，不能调整积分', 409)
    before = get_member_score_breakdown(member.id)
    if target_total is not None:
        try:
            delta = int(target_total) - before['final_score']
        except (TypeError, ValueError) as exc:
            raise SeasonError('目标总分必须是整数', 400) from exc
    else:
        try:
            delta = int(delta)
        except (TypeError, ValueError) as exc:
            raise SeasonError('调整分必须是整数', 400) from exc
    if not delta:
        return None, before, before
    adjustment = ScoreAdjustment(tournament_id=member.team.tournament_id,
                                 member_id=member.id, actor_id=actor_id,
                                 delta=delta, reason=reason)
    db.session.add(adjustment)
    db.session.flush()
    after = get_member_score_breakdown(member.id)
    return adjustment, before, after


def recalculate_all_scores(tournament_id=None):
    from season_safety import active_season, correction_allowed, final_score, historical_context, SeasonError, verify_match_ownership
    from flask import has_request_context
    from flask_login import current_user
    db, Match, Team, TeamMember, Tournament, SystemLog = _app_ns('db', 'Match', 'Team', 'TeamMember', 'Tournament', 'SystemLog')
    active = active_season(required=False)
    if tournament_id is None:
        if active is None:
            return {'teams': 0, 'matches': 0, 'tournament_id': None}
        tournament_id = active.id
    season = db.session.get(Tournament, tournament_id)
    if season is None:
        raise SeasonError('赛季不存在', 404)
    if (active is None or active.id != tournament_id) and not correction_allowed(tournament_id):
        raise SeasonError('往届重算仅允许超管专项纠错', 403)
    bonus = season.bonus_score if season.is_active else historical_context(season)['bonus_score']
    team_ids = [t.id for t in Team.query.filter_by(tournament_id=tournament_id).all()]
    if team_ids:
        conflicting = Match.query.filter(
            db.or_(Match.team_a_id.in_(team_ids), Match.team_b_id.in_(team_ids)),
            db.or_(Match.tournament_id.is_(None), Match.tournament_id != tournament_id)).first()
        if conflicting:
            raise SeasonError(f'比赛 {conflicting.id} 的赛季归属与本届队伍冲突/缺失，请先人工核对；原积分未改动')
    # Validate every input before zeroing anything. Legacy outcomes and scored children require review.
    records = Match.query.filter_by(tournament_id=tournament_id).all()
    for match in records:
        if match.match_type == 'exhibition':
            continue  # Exhibition policy remains explicitly deferred; never silently invent settlement.
        if match.parent_id and (match.player_a_score is not None or match.player_b_score is not None or match.settlement_state == 'settled'):
            raise SeasonError(f'分段 {match.id} 有独立成绩，需人工核对整场归并方案')
        if match.settlement_state == 'legacy' and (match.player_a_score is not None or match.player_b_score is not None or match.status in ('finished', 'forfeit_a', 'forfeit_b')):
            raise SeasonError(f'旧比赛 {match.id} 缺少确认的历史结算基准')
    matches = [m for m in records if is_formal(m) and m.settlement_state == 'settled']
    totals = {}
    for match in matches:
        verify_match_ownership(match)
        if match.status not in ('finished', 'forfeit_a', 'forfeit_b'):
            raise SeasonError(f'比赛 {match.id} 结算与结果状态冲突')
        for side in ('a', 'b'):
            key = (getattr(match, 'team_' + side + '_id'), getattr(match, 'player_' + side + '_id'))
            totals[key] = totals.get(key, 0) + final_score(match, side)
    # Exhibition policy is deferred. Preserve its old finished/forfeit contribution semantics,
    # while requiring verified snapshots for any contribution that actually has a team member.
    # The existing exhibition creators use null teams, and therefore contribute nothing as before.
    for match in records:
        if match.match_type != 'exhibition' or match.status not in ('finished', 'forfeit_a', 'forfeit_b'):
            continue
        for side in ('a', 'b'):
            team_id = getattr(match, 'team_' + side + '_id')
            player_id = getattr(match, 'player_' + side + '_id')
            member = TeamMember.query.filter_by(team_id=team_id, user_id=player_id).first() if team_id else None
            if member is None:
                continue
            team = db.session.get(Team, team_id)
            if team.tournament_id != tournament_id:
                raise SeasonError('已有表演赛成绩存在跨届归属冲突，须先核对')
            raw = getattr(match, 'player_' + side + '_score')
            if raw is not None:
                key = (team_id, player_id)
                totals[key] = totals.get(key, 0) + final_score(match, side)
    members = TeamMember.query.join(Team).filter(Team.tournament_id == tournament_id).all()
    teams = Team.query.filter_by(tournament_id=tournament_id).all()
    rebuilt = {member.id: totals.get((member.team_id, member.user_id), 0) for member in members}
    # Adjustment rows are independent evidence and survive every rebuild.
    Adjustment = _app_ns('ScoreAdjustment')[0]
    for adjustment in Adjustment.query.filter_by(tournament_id=tournament_id, revoked=False).all():
        target = next((m for m in members if m.id == adjustment.member_id), None)
        if target is None:
            raise SeasonError('人工调整分缺少原成员记录')
        rebuilt[target.id] += adjustment.delta
    for member in members:
        member.final_score = rebuilt[member.id]
    for team in teams:
        team.team_score = sum(m.final_score for m in members if m.team_id == team.id) * bonus
    result = {'tournament_id': tournament_id, 'teams': len(teams), 'matches': len(matches)}
    db.session.add(SystemLog(user_id=current_user.id if has_request_context() and current_user.is_authenticated else None,
                             action='recalculate_scores', detail=__import__('json').dumps(result)))
    return result


def _update_team_scores(match):
    from season_safety import root_match
    return recalculate_all_scores(root_match(match).tournament_id)
