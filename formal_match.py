"""Shared semantics for formal matches, schedule segments and outcomes.

The application stores schedule segments as child ``Match`` rows.  Only the
root row is the formal scoring unit; children carry scheduling information and
must never create a second result, win/loss record, or score contribution.
"""

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
    if not has_outcome(match):
        return None
    if match.winner_id is None:
        return 'draw'
    return 'win' if match.winner_id == player_id else 'loss'


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

