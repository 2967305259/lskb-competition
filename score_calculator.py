"""Server-side calculation of normalized inputs with a validated v3 rule configuration."""

import re
from decimal import Decimal
from fractions import Fraction
from math import ceil, floor, isfinite


SECTION_KEYS = ('basic', 'levelBonus', 'extraBonus', 'penalty')
AMOUNT_PATTERN = re.compile(r'-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)')


def _input_number(value, field):
    if value == '':
        return Fraction(0)
    if not isinstance(value, str) or not AMOUNT_PATTERN.fullmatch(value) or not isfinite(float(value)):
        raise ValueError(f'{field} 必须是规范化的十进制数值或空字符串')
    return Fraction(Decimal(value))


def _rule_number(value):
    # JSON numbers reach both engines as decimal text before arithmetic.
    return Fraction(Decimal(str(value)))


def _rounded(value):
    cents = floor(value * 100 + Fraction(1, 2))
    return Decimal(f'{cents}e-2')


def _time_score(basic, value):
    elapsed = max(0, _input_number(value, 'time'))
    rules = basic['timeRules']
    if value == '' or not rules:
        return Fraction(0)
    for rule in rules:
        if _rule_number(rule['min']) <= elapsed <= _rule_number(rule['max']):
            return _rule_number(rule['score'])
    last_bound = max(_rule_number(rule['max']) for rule in rules)
    over = elapsed - last_bound
    if over <= 0:
        return Fraction(0)
    overflow = basic['timeOverflow']
    units = over if overflow['rounding'] == 'exact' else ceil(over)
    return units * _rule_number(overflow['perHour'])


def _item_score(item, value):
    score = _rule_number(item['score'])
    item_type = item['type']
    if item_type == 'check':
        if type(value) is not bool:
            raise ValueError(f"items.{item['id']} 必须是布尔值")
        return score if value else Fraction(0)
    if item_type == 'fixed':
        return score
    quantity = max(_rule_number(item.get('min', 0)), _input_number(value, f"items.{item['id']}"))
    if item_type == 'threshold':
        quantity = max(0, quantity - _rule_number(item['threshold']))
    return quantity * score


def calculate(config, scores):
    """Return two-place totals and the whole-number score before the difficulty multiplier."""
    if not isinstance(scores, dict) or scores.keys() - {'original', 'time', 'items'}:
        raise ValueError('成绩明细必须由 original、time 和 items 组成')
    items = scores.get('items', {})
    if not isinstance(items, dict):
        raise ValueError('items 必须是对象')
    original = _input_number(scores.get('original', ''), 'original')
    time = _time_score(config['basic'], scores.get('time', ''))
    sections = dict.fromkeys(SECTION_KEYS, Fraction(0))
    item_scores = {}
    input_ids = set()
    for key in SECTION_KEYS:
        for group_index, group in enumerate(config[key]['groups']):
            for item_index, item in enumerate(group['items']):
                item_type = item['type']
                value = False if item_type == 'check' else ''
                if item_type != 'fixed':
                    input_ids.add(item['id'])
                    value = items.get(item['id'], value)
                subtotal = _item_score(item, value)
                sections[key] += subtotal
                item_scores[f'{key}:{group_index}:{item_index}'] = _rounded(subtotal)
    unknown = items.keys() - input_ids
    if unknown:
        raise ValueError(f"items.{min(unknown)} 不是该规则中的可填写条目")

    basic_subtotal = time + sections['basic']
    bonus = basic_subtotal + sections['levelBonus'] + sections['extraBonus']
    penalty = sections['penalty']
    total = _rounded(original + bonus + penalty)
    return {
        'original': _rounded(original),
        'time': _rounded(time),
        'sections': {key: _rounded(value) for key, value in sections.items()},
        'basicSub': _rounded(basic_subtotal),
        'bonus': _rounded(bonus),
        'penalty': _rounded(penalty),
        'total': total,
        'matchScore': floor(Fraction(total) + Fraction(1, 2)),
        'itemScores': item_scores,
    }
