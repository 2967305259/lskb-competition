"""Validate a player submission and record the server's calculation."""

from decimal import Decimal
import json

from flask_login import current_user

from formal_match import is_formal
from score_calculator import AMOUNT_PATTERN, calculate
from season_safety import ScoreInputError, json_dump, ns


def submission_required(match, binding):
    if not is_formal(match):
        return False
    if binding is not None:
        return True
    if not match.tournament.calculator_enabled:
        return False
    # Already-started manual matches keep the same submission mode for both sides.
    return all(getattr(match, f'player_{side}_score') is None
               or getattr(match, f'score_origin_{side}') == 'forfeit_zero'
               for side in ('a', 'b'))


def _unique_fields(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError('计分明细含重复字段')
    return data


def _invalid_numeric_field(raw):
    raise ValueError('计分数值须以字符串提交')


def read_scores(raw):
    if not raw or len(raw.encode('utf-8')) > 64 * 1024:
        raise ScoreInputError('calculator', '请填写计分明细，明细大小不得超过 64 KiB')
    try:
        scores = json.loads(raw, object_pairs_hook=_unique_fields,
                            parse_float=_invalid_numeric_field, parse_constant=_invalid_numeric_field)
    except (ValueError, RecursionError) as error:
        raise ScoreInputError('calculator', '计分明细格式无效，请重新填写') from error
    if not isinstance(scores, dict):
        raise ScoreInputError('calculator', '计分明细必须为 JSON 对象')
    return scores


def recalculate(rule_version, config, scores, submitted_version, submitted_total):
    if not submitted_version:
        raise ScoreInputError('calculator', '请使用本页计分器填写明细后再提交')
    if submitted_version != str(rule_version):
        raise ScoreInputError('calculator', '该整场适用的规则版本已更新，请核对当前预览后重新提交', 409)
    if len(submitted_total) > 32 or not AMOUNT_PATTERN.fullmatch(submitted_total):
        raise ScoreInputError('calculator', '计分器总分无效，请核对当前预览后重新提交')
    try:
        result = calculate(config, scores)
    except (ValueError, ArithmeticError) as error:
        raise ScoreInputError('calculator', f'计分明细无效：{error}') from error
    if result['total'] != Decimal(submitted_total):
        raise ScoreInputError('calculator', f"预览总分与服务端重算的 {result['total']} 不一致，请核对明细后重新提交")
    return result


def record_submission(match, side, binding, version, scores, result):
    """Join the existing score and screenshot transaction."""
    a = ns()
    if binding is None:
        binding = a.MatchCalculatorResult(match_id=match.id, tournament_id=match.tournament_id, rule_version=version)
        a.db.session.add(binding)
    setattr(binding, f'details_{side}', json_dump({'scores': scores, 'result': result}))
    a.db.session.add(a.ScoreLog(
        match_id=match.id, modified_by=current_user.id, reason='计分器提交',
        new_result=json_dump({
            'tournament_id': match.tournament_id, 'side': side, 'rule_version': version,
            'calculator_total': result['total'], 'raw_score': result['matchScore'],
            'difficulty': getattr(match, f'player_{side}_difficulty'),
            'multiplier': getattr(match, f'player_{side}_multiplier'),
        }),
    ))


def recorded_totals(binding):
    if binding is None:
        return {}
    totals = {}
    for side in ('a', 'b'):
        raw = getattr(binding, f'details_{side}')
        if raw:
            totals[side] = json.loads(raw)['result']['total']
    return totals
