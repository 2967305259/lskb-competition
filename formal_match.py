"""Shared semantics for formal matches, schedule segments and outcomes.

The application stores schedule segments as child ``Match`` rows.  Only the
root row is the formal scoring unit; children carry scheduling information and
must never create a second result, win/loss record, or score contribution.
"""
from sqlalchemy import and_, case, func

FORMAL_MATCH_TYPE = 'match'
OUTCOME_STATUSES = frozenset(('finished', 'forfeit_a', 'forfeit_b'))


def is_formal(match):
    """Return whether *match* is a root, non-exhibition scoring unit."""
    return match is not None and match.parent_id is None and match.match_type == FORMAL_MATCH_TYPE


def is_segment(match):
    return match is not None and match.parent_id is not None


def has_outcome(match):
    """An outcome is known even while a forfeited match awaits score settlement."""
    return bool(match is not None and match.status in OUTCOME_STATUSES)


def is_settled(match):
    """Only an explicitly settled formal match contributes to standings."""
    return bool(is_formal(match) and match.settlement_state == 'settled'
                and match.player_a_score is not None and match.player_b_score is not None
                and has_outcome(match))


def outcome_for_player(match, player_id):
    """Return ``win``, ``loss`` or ``draw`` for a participant, else ``None``."""
    if not is_formal(match) or player_id not in (match.player_a_id, match.player_b_id) or not has_outcome(match):
        return None
    if match.winner_id is None:
        return 'draw'
    return 'win' if match.winner_id == player_id else 'loss'


def summarize_formal_matches(query, Match):
    """Count three independent facts over a formal-match query."""
    decided = Match.status.in_(OUTCOME_STATUSES)
    settled = and_(decided, Match.settlement_state == 'settled',
                   Match.player_a_score.is_not(None), Match.player_b_score.is_not(None))
    criteria = {'decided': decided, 'settled': settled}
    criteria.update({state: Match.status == state for state in ('pending', 'scheduled', 'confirmed', 'submitted')})
    row = query.order_by(None).with_entities(
        func.count(Match.id), *(func.count(case((test, 1))) for test in criteria.values())).one()
    return dict(zip(('formal', *criteria), row))


def summarize_teams(matches):
    """Count each formal fixture once for each participating team."""
    records = {}
    for match in matches:
        for team_id, player_id in ((match.team_a_id, match.player_a_id), (match.team_b_id, match.player_b_id)):
            if team_id is None:
                continue
            counts = records.setdefault(team_id, empty_record())
            counts['formal'] += 1
            counts['settled'] += int(is_settled(match))
            outcome = outcome_for_player(match, player_id)
            if outcome:
                counts['decided'] += 1
                counts[outcome] += 1
    return records


def empty_record():
    return dict.fromkeys(('formal', 'decided', 'settled', 'win', 'draw', 'loss'), 0)


def formal_query(Match, db, tournament_id=None):
    """Build a query containing one row per formal match."""
    query = Match.query.filter(Match.parent_id.is_(None), Match.match_type == FORMAL_MATCH_TYPE)
    if tournament_id is not None:
        query = query.filter(Match.tournament_id == tournament_id)
    return query


def player_query(Match, db, player_id, tournament_id=None):
    query = formal_query(Match, db, tournament_id).filter(
        db.or_(Match.player_a_id == player_id, Match.player_b_id == player_id))
    return query

