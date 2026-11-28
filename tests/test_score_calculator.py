"""Pin the calculator's shared decimal rules against the original v3 export."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from score_calculator import calculate


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name('fixtures') / 'calculator-v3-config.json'
SHARED = ROOT / 'app/static/calculator/score-calculator.js'


@pytest.fixture
def config():
    exported = json.loads(FIXTURE.read_text(encoding='utf-8'))
    assert exported['app'] == 'arknights-ir-score-calculator'
    assert exported['kind'] == 'config'
    assert exported['version'] == exported['config']['version'] == 3
    return exported['config']


def test_single_file_keeps_the_shared_script_in_sync():
    subprocess.run(
        [sys.executable, str(ROOT / 'arknights-score-calculator/sync_shared.py'), '--check'],
        check=True,
    )


@pytest.mark.parametrize('scores', [
    {'original': '１。２３４'},
    {'time': 'Infinity'},
    {'items': {'lv-1': 'true'}},
    {'items': {'basic-relic': 1}},
    {'items': {'unknown': ''}},
    {'items': []},
    {'total': 999},
])
def test_server_rejects_invalid_canonical_inputs(config, scores):
    with pytest.raises(ValueError):
        calculate(config, scores)


def test_frontend_and_server_use_the_same_rules_and_rounding(config):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the JavaScript calculator comparison')

    custom = deepcopy(config)
    custom['basic']['timeRules'] = [
        {'min': 0, 'max': 0.3, 'score': 1.005},
        {'min': 0.3, 'max': 4, 'score': 8},
    ]
    custom['basic']['timeOverflow'] = {'rounding': 'exact', 'perHour': -0.5}
    custom['basic']['groups'][0]['items'][0].update(min=1.234, score=5)
    custom['extraBonus']['groups'].append({
        'title': '规则',
        'items': [
            {'id': 'fixed-example', 'type': 'fixed', 'name': '固定', 'score': -1.005},
            {'id': 'negative-number', 'type': 'number', 'name': '数量', 'score': 2, 'min': -0.75},
        ],
    })
    no_time = deepcopy(config)
    no_time['basic']['timeRules'] = []
    cases = [
        (config, {}, 0, 0),
        (config, {'items': {'basic-relic': '１。２３４'}}, '6.17', 6),
        (config, {'time': '２'}, 30, 30),
        (config, {'time': '2.0000000000000001'}, 0, 0),
        (config, {'time': '3.9999999999999999'}, 0, 0),
        (config, {'time': '４。０００００００００００００００１'}, -20, -20),
        (config, {'time': '5.0000000000000001'}, -40, -40),
        (config, {'time': '－１'}, 30, 30),
        (config, {'items': {'p-money': '100'}}, 0, 0),
        (config, {'items': {'p-money': '100.01'}}, '-0.1', 0),
        (config, {'items': {'basic-relic': '1.234', 'lv-1': True, 'ex-recruit-6': '2', 'p-money': '101'}},
         '296.17', 296),
        (config, {'original': '1.005'}, '1.01', 1),
        (config, {'original': '-1.005'}, '-1.00', -1),
        (config, {'original': '12.495'}, '12.50', 13),
        (config, {'original': '-12.505'}, '-12.50', -12),
        (config, {'original': '-12.51'}, '-12.51', -13),
        (no_time, {'time': '99'}, 0, 0),
        (custom, {'time': '0.3', 'items': {'basic-relic': '.1', 'negative-number': '-1'}}, '4.67', 5),
        (custom, {'time': '4.1'}, '5.12', 5),
    ]
    javascript = r"""
require(process.argv[1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const results = cases.map(([config, entry]) => {
  const scores = ScoreCalculator.readScores(config, path => {
    if (path === 'basic:original') return entry.original;
    if (path === 'basic:time') return entry.time;
    const [key, group, index] = path.split(':');
    return entry.items?.[config[key].groups[group].items[index].id];
  });
  return { scores, result: ScoreCalculator.calculate(config, scores) };
});
process.stdout.write(JSON.stringify(results));
"""
    run = subprocess.run(
        [node, '-e', javascript, str(SHARED)],
        input=json.dumps([(rules, inputs) for rules, inputs, _, _ in cases]),
        text=True, capture_output=True, check=True,
    )
    results = json.loads(run.stdout, parse_float=Decimal)

    assert results[1]['scores']['items']['basic-relic'] == '1.234'
    assert results[6]['scores']['time'] == '5.0000000000000001'
    assert results[-2]['scores']['items']['basic-relic'] == '1.234'
    for (rules, _, total, match_score), preview in zip(cases, results, strict=True):
        result = calculate(rules, preview['scores'])
        assert result == preview['result']
        assert result['total'] == Decimal(total)
        assert result['matchScore'] == match_score
