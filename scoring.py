"""Season-scoped scoring. Services never commit; callers own business + audit transactions."""
import sys
from decimal import Decimal

from formal_match import (FORMAL_MATCH_TYPE, OUTCOME_STATUSES, formal_query,
                          is_formal, outcome_for_player)


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


def get_player_stats(player_id, tournament_id=None, scope='current'):
    """Return full-scope record totals and the most recent formal matches.

    ``scope=current`` is the default and resolves to the unique active season.
    ``scope=all`` intentionally removes the season filter.  Segment rows and
    exhibition rows are never included, so a split match is counted once.
    """
    Match, db = _app_ns('Match', 'db')
    if scope not in ('current', 'all'):
        raise ValueError('invalid player-stat scope')
    season_id = None
    if scope == 'current':
        from season_safety import active_season
        season = active_season(required=False)
        if season is None:
            return {'wins': 0, 'losses': 0, 'draws': 0, 'total_matches': 0,
                    'season_scope': 'current', 'season_id': None, 'matches': []}
        season_id = season.id if tournament_id is None else tournament_id
    elif tournament_id is not None:
        season_id = tournament_id
    query = formal_query(Match, db, season_id).filter(
        db.or_(Match.player_a_id == player_id, Match.player_b_id == player_id))
    all_matches = query.order_by(Match.created_at.desc()).all()
    wins = losses = draws = 0
    for match in all_matches:
        result = outcome_for_player(match, player_id)
        if result == 'win':
            wins += 1
        elif result == 'loss':
            losses += 1
        elif result == 'draw':
            draws += 1
    return {'wins': wins, 'losses': losses, 'draws': draws,
            'total_matches': wins + losses + draws,
            'season_scope': scope, 'season_id': season_id,
            'matches': all_matches[:50]}


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
