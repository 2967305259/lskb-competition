"""Import, preview and publish the independent calculator's v3 configuration."""

from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

from flask import current_app, request
from flask_login import current_user
from itsdangerous import BadData, URLSafeTimedSerializer

from formal_match import is_formal
from score_calculator import SECTION_KEYS
from season_safety import SeasonError, json_dump, ns, require_writable


MAX_CONFIG_BYTES = 64 * 1024
FORMAT_VERSION = 3
CALCULATION_VERSION = 1
DEFAULT_CONFIGURATION = Path(__file__).resolve().parent / 'app/static/calculator/default-config.json'


def _fail(path, message):
    raise SeasonError(f'{path}：{message}', 400)


def _object(value, path):
    if not isinstance(value, dict):
        _fail(path, '须为 JSON 对象')
    return value


def _list(value, path):
    if not isinstance(value, list):
        _fail(path, '须为 JSON 数组')
    return enumerate(value)


def _text(value, path, *, required=True):
    if not isinstance(value, str) or (required and not value.strip()):
        _fail(path, '须为非空字符串' if required else '须为字符串')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        _fail(path, '包含无法编码的字符')


def _number(value, path, limit=2**53 - 1):
    if type(value) not in (int, float) or abs(value) > limit or not isfinite(value):
        _fail(path, f'须为有限数字，绝对值不超过 {limit}')
    return value


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('配置文件', f'有重复的 JSON 字段 {key}')
        result[key] = value
    return result


def _finite_float(raw):
    value = float(raw)
    if not isfinite(value):
        _fail('配置文件', '包含非有限数字')
    return value


def _validate_time(basic):
    for field in ('scoreLabel', 'scoreHint', 'timeLabel'):
        _text(basic.get(field, '' if field == 'scoreHint' else None), f'basic.{field}',
              required=field != 'scoreHint')
    for index, value in _list(basic.get('timeRules'), 'basic.timeRules'):
        path = f'basic.timeRules[{index}]'
        rule = _object(value, path)
        start = _number(rule.get('min'), f'{path}.min')
        end = _number(rule.get('max'), f'{path}.max')
        if start > end:
            _fail(path, '最短用时须小于或等于最长用时')
        _number(rule.get('score'), f'{path}.score', 100000)
    overflow = _object(basic.get('timeOverflow'), 'basic.timeOverflow')
    _number(overflow.get('perHour'), 'basic.timeOverflow.perHour', 100000)
    if overflow.get('rounding') not in ('ceil', 'exact'):
        _fail('basic.timeOverflow.rounding', '须为 ceil 或 exact')


def _validate_items(config):
    items = []
    total_groups = 0
    for key in SECTION_KEYS:
        section = _object(config.get(key), key)
        _text(section.get('title'), f'{key}.title')
        if section.get('kind') != ('penalty' if key == 'penalty' else 'bonus'):
            _fail(f'{key}.kind', '分区类别与独立版的格式不符')
        for group_index, value in _list(section.get('groups'), f'{key}.groups'):
            path = f'{key}.groups[{group_index}]'
            group = _object(value, path)
            _text(group.get('title'), f'{path}.title', required=False)
            total_groups += 1
            for item_index, entry in _list(group.get('items'), f'{path}.items'):
                item_path = f'{path}.items[{item_index}]'
                item = _object(entry, item_path)
                for field in ('id', 'name'):
                    _text(item.get(field), f'{item_path}.{field}')
                kind = item.get('type')
                if kind not in ('check', 'number', 'fixed', 'threshold'):
                    _fail(f'{item_path}.type', '须为 check、number、fixed 或 threshold')
                _number(item.get('score'), f'{item_path}.score', 100000)
                if 'unit' in item:
                    _text(item['unit'], f'{item_path}.unit', required=False)
                if 'min' in item:
                    _number(item['min'], f'{item_path}.min')
                if kind == 'threshold':
                    _number(item.get('threshold'), f'{item_path}.threshold')
                items.append((item_path, item))
    if len(items) + total_groups + len(config['basic']['timeRules']) > 1000:
        _fail('配置文件', '分组、计分条目和用时区间合计最多 1000 项')
    return items


def parse_configuration(raw):
    if len(raw) > MAX_CONFIG_BYTES:
        _fail('配置文件', '大小不得超过 64 KiB')
    try:
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_keys,
                          parse_float=_finite_float, parse_constant=_finite_float)
    except (ValueError, RecursionError) as error:
        if isinstance(error, SeasonError):
            raise
        raise SeasonError('配置文件不是有效的 UTF-8 JSON', 400) from error
    data = _object(data, '配置文件')
    if data.get('app') != 'arknights-ir-score-calculator' or data.get('kind') != 'config':
        _fail('配置文件', '请上传独立计分器通过「导出配置」生成的 JSON')
    if type(data.get('version')) is not int or data['version'] != FORMAT_VERSION:
        _fail('version', '当前支持独立计分器 v3 配置')
    _text(data.get('exportedAt'), 'exportedAt')
    try:
        datetime.fromisoformat(data['exportedAt'])
    except ValueError:
        _fail('exportedAt', '须为独立计分器导出的日期时间')
    config = _object(data.get('config'), 'config')
    try:
        json_dump(config).encode('utf-8')
    except UnicodeEncodeError:
        _fail('config', '包含无法编码的字符')
    if type(config.get('version')) is not int or config['version'] != FORMAT_VERSION:
        _fail('config.version', '须与导出文件版本一致')
    _validate_time(_object(config.get('basic'), 'basic'))
    items = _validate_items(config)
    reserved = {item['id'] for _, item in items}
    seen = set()
    changes = []
    counter = 0
    for path, item in items:
        original = item['id']
        if original in seen:
            counter += 1
            while f'it-{counter}' in reserved:
                counter += 1
            new_id = f'it-{counter}'
            item['id'] = new_id
            reserved.add(new_id)
            changes.append({'path': path, 'name': item['name'], 'old': original, 'new': new_id})
        seen.add(original)
    return config, changes


def initial_configuration():
    return json.loads(DEFAULT_CONFIGURATION.read_text(encoding='utf-8'))


def configuration_from_form():
    upload = request.files.get('score_rules_file')
    pasted = request.form.get('score_rules_json', '')
    if upload and pasted.strip():
        _fail('配置文件', '请只选择上传文件或粘贴 JSON 其中一种方式')
    if upload:
        raw = upload.stream.read(MAX_CONFIG_BYTES + 1)
    elif pasted.strip():
        raw = pasted.encode('utf-8')
    else:
        _fail('配置文件', '请上传或粘贴独立版导出的配置 JSON')
    return parse_configuration(raw)


def latest_rule(tournament_id):
    rules = ns().ScoreRuleSet
    return rules.query.filter_by(tournament_id=tournament_id).order_by(rules.version.desc()).first()


def rule_for_match(match):
    if not is_formal(match):
        return None
    a = ns()
    result = a.db.session.get(a.MatchCalculatorResult, match.id)
    return result.rule_set if result is not None else latest_rule(match.tournament_id)


def _preview_signer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='calculator-rule-preview-v1')


def create_preview(season_id, purpose, version=0):
    config, changes = configuration_from_form()
    # ASCII encoding also preserves the payload through HTML form newline normalization.
    content = json.dumps(config, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    token = _preview_signer().dumps({
        'actor': current_user.id, 'season': season_id, 'purpose': purpose, 'version': version,
        'digest': sha256(content.encode('utf-8')).hexdigest(),
    })
    return {'config': config, 'changes': changes, 'content': content, 'token': token}


def confirmed_configuration(season_id, purpose, version=0):
    content = request.form.get('score_rules_payload', '')
    try:
        preview = _preview_signer().loads(request.form.get('score_rules_preview', ''), max_age=1800)
    except BadData as error:
        raise SeasonError('计分规则预览无效或已过期，请重新预览', 400) from error
    if preview['actor'] != current_user.id:
        raise SeasonError('计分规则预览属于另一位操作者', 403)
    if (preview['season'], preview['purpose'], preview['version']) != (season_id, purpose, version):
        raise SeasonError('赛季或规则版本已变化，请重新预览', 409)
    if not content or preview['digest'] != sha256(content.encode('utf-8')).hexdigest():
        raise SeasonError('计分规则内容与预览不一致，请重新预览', 400)
    return json.loads(content)


def record_publication(tournament_id, config, version, source):
    """Record one version under the request's existing maintenance lock."""
    a = ns()
    require_writable(tournament_id)
    rule = a.ScoreRuleSet(tournament_id=tournament_id, version=version, source=source,
                          calculation_version=CALCULATION_VERSION, config=json_dump(config),
                          published_by_id=current_user.id)
    a.db.session.add(rule)
    a.log_system_action('publish_calculator_rules', json_dump({
        'tournament_id': tournament_id, 'version': version,
        'calculation_version': CALCULATION_VERSION, 'source': source,
    }))
    return rule
