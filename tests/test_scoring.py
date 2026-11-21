from decimal import Decimal
from types import SimpleNamespace

import pytest

from season_safety import SeasonError, final_score, multiplier


RULE = {'minimum': 10, 'maximum': 12, 'step': '0.123456'}


@pytest.mark.parametrize(('difficulty', 'expected'), [(10, '1.0000'), (12, '1.2469')])
def test_multiplier_includes_difficulty_boundaries(difficulty, expected):
    assert multiplier(RULE, difficulty) == Decimal(expected)


@pytest.mark.parametrize('difficulty', [9, 13])
def test_multiplier_rejects_difficulty_outside_saved_rule(difficulty):
    with pytest.raises(SeasonError, match='难度超出') as error:
        multiplier(RULE, difficulty)
    assert error.value.status == 400


@pytest.mark.parametrize('step', ['-0.1', 'NaN', 'Infinity', '9999'])
def test_multiplier_rejects_invalid_steps(step):
    with pytest.raises(SeasonError) as error:
        multiplier(dict(RULE, step=step), RULE['maximum'])
    assert error.value.status == 400


@pytest.mark.parametrize(('raw', 'expected'), [(19, 21), (-19, -21), (0, 0)])
def test_final_score_truncates_toward_zero(raw, expected):
    match = SimpleNamespace(id=1, scoring_rule='{}', player_a_score=raw, player_a_multiplier=Decimal('1.125'))
    assert final_score(match, 'a') == expected


@pytest.mark.parametrize(('raw', 'rate'), [(None, Decimal('1.125')), (0, None)])
def test_final_score_rejects_missing_settlement_evidence(raw, rate):
    match = SimpleNamespace(id=1, scoring_rule='{}', player_a_score=raw, player_a_multiplier=rate)
    with pytest.raises(SeasonError, match='结算资料/历史倍率缺失'):
        final_score(match, 'a')
