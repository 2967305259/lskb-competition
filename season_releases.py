"""Versioned, portable configuration for one season."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from sqlite3 import Error as SQLiteError
from subprocess import TimeoutExpired

from flask import current_app, request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backup import create_backup, digest, prune_backups
from migrations import SCHEMA_VERSION
from season_safety import (SCORE_CONTRACT, SeasonError, active_season, json_dump, ns,
                           parse_track_difficulty, save_context)

FORMAT_VERSION = 1
SCORING = {'policy': SCORE_CONTRACT, 'raw_fields': ['player_a_score', 'player_b_score'],
           'multiplier': '1 + (difficulty - minimum) * step', 'rounding': 'truncate_toward_zero',
           'team_total': '(match_points + active_adjustments) * team_multiplier'}
WINDOW_FIELDS = ('registration_start', 'registration_end', 'tournament_start', 'tournament_end')
ENVIRONMENT_FIELDS = ('EVENT_TIMEZONE', 'TEAM_POSITIONS', 'TRACK_LABEL', 'MEMBER_FIELD1_LABEL',
                      'MEMBER_FIELD2_LABEL', 'SCHEDULE_SLOT_TIMES', 'SCHEDULE_SLOT_MINUTES',
                      'DEFAULT_DIFFICULTY_MIN', 'DEFAULT_DIFFICULTY_MAX', 'DEFAULT_DIFFICULTY_STEP',
                      'BRAND_PRIMARY_COLOR')
TRACK_FIELDS = ('key', 'name', 'is_active', 'max_per_team', 'sort_order',
                'difficulty_min', 'difficulty_max', 'difficulty_step')
RULE_FIELDS = ('title', 'content', 'roguelike_key', 'sort_order', 'is_published')
CAROUSEL_FIELDS = ('media_type', 'file_path', 'title', 'link_url', 'sort_order', 'is_active')
MEDIA_ROOTS = {'image': 'IMAGE_DIR', 'uploads': 'UPLOAD_ROOT'}


def _seal(payload):
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return {'sha256': sha256(content.encode('utf-8')).hexdigest(), 'payload': payload}


def _require(condition, message):
    if not condition:
        raise SeasonError(message, 400)


def _fields(value, names, label):
    _require(isinstance(value, dict) and value.keys() == set(names), f'{label}字段缺失或不兼容')


def _text(value, limit, label, *, nullable=False, required=False):
    if value is None and nullable:
        return
    _require(isinstance(value, str) and len(value) <= limit and '\x00' not in value
             and (not required or bool(value.strip())), f'{label}内容缺失或不兼容')


def _integer(value, label, minimum=0, maximum=2**31 - 1):
    _require(type(value) is int and minimum <= value <= maximum, f'{label}不是有效整数')


def _boolean(value, label):
    _require(type(value) is bool, f'{label}须为布尔值')


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _identity(value):
    _fields(value, ('event', 'name', 'season'), '赛事标识')
    _text(value['event'], 200, '赛事标识', required=True)
    _text(value['name'], 100, '赛事名称', required=True)
    _integer(value['season'], '届次', 1)


def _origin(value):
    if value is not None:
        _fields(value, ('identity', 'version', 'sha256'), '来源版本')
        _identity(value['identity'])
        _integer(value['version'], '来源版本号', 1)
        _require(_hash(value['sha256']), '来源校验值无效')


def _media_ref(file_path):
    for prefix, root in (('/static/uploads/', 'uploads'), ('/carousel-media/', 'image'), ('image/', 'image')):
        if file_path.startswith(prefix):
            return root, file_path.removeprefix(prefix)
    return 'image', file_path


def _safe_relative(path):
    return not re.search(r'[\x00-\x1f\x7f\\]', path) and all(part not in ('', '.', '..') for part in path.split('/'))


def _media_hash(root, path):
    if not _safe_relative(path):
        return None
    base = Path(current_app.config[MEDIA_ROOTS[root]]).resolve()
    try:
        target = (base / path).resolve()
        if target.is_relative_to(base) and target.is_file():
            return digest(target)
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def _media_refs(carousel):
    if carousel:
        return sorted({_media_ref(item['file_path']) for item in carousel})
    return [('image', item['file']) for item in ns().load_carousel_media()]


def _environment():
    return {field: current_app.config[field] for field in ENVIRONMENT_FIELDS}


def _rows(query, model, fields):
    return [{field: getattr(row, field) for field in fields}
            for row in query.order_by(model.sort_order, model.id)]


def _capture(season):
    a = ns()
    setting = a.Setting.get_instance()
    carousel = _rows(season.carousel_items, a.CarouselItem, CAROUSEL_FIELDS)
    config = {
        'team_size': a.get_team_size(), 'allow_incomplete_teams': bool(setting.allow_incomplete_teams),
        'advance_count': setting.advance_count or 0, 'bonus_score': season.bonus_score,
        'windows': {field: getattr(season, field).isoformat() if getattr(season, field) else None
                    for field in WINDOW_FIELDS},
        'window_times_utc': season.window_times_utc or not any(getattr(season, field) for field in WINDOW_FIELDS),
        'tracks': sorted(_rows(a.Roguelike.query, a.Roguelike, TRACK_FIELDS),
                         key=lambda track: (track['sort_order'] or 0, track['key'])),
        'rules': _rows(a.Rule.query, a.Rule, RULE_FIELDS),
        'carousel': carousel,
        'media': [{'root': root, 'path': path, 'sha256': _media_hash(root, path)} for root, path in _media_refs(carousel)],
    }
    return {'format_version': FORMAT_VERSION, 'application_version': current_app.config['APP_VERSION'],
            'schema_version': SCHEMA_VERSION, 'score_contract': SCORING,
            'identity': {'event': current_app.config['EVENT_NAME'], 'name': season.name, 'season': season.season_number},
            'environment': _environment(), 'configuration': config, 'publication': None,
            'source': json.loads(season.release_source) if season.release_source else None}


def _validate_settings(config):
    _integer(config['team_size'], '队伍人数', 1)
    _boolean(config['allow_incomplete_teams'], '未满员开赛设置')
    _integer(config['advance_count'], '晋级名额')
    _integer(config['bonus_score'], '队伍倍率')
    _boolean(config['window_times_utc'], '时间口径')
    windows = config['windows']
    _fields(windows, WINDOW_FIELDS, '计划时间')
    parsed = {}
    for field, value in windows.items():
        if value is None:
            continue
        _text(value, 32, field, required=True)
        try:
            date = datetime.fromisoformat(value)
        except ValueError as error:
            raise SeasonError(f'{field} 日期无效', 400) from error
        _require(date.tzinfo is None and date.year >= 1000 and not date.microsecond and date.isoformat() == value,
                 f'{field} 时间格式不兼容')
        parsed[field] = date
    issues = []
    if parsed and not config['window_times_utc']:
        issues.append('计划日期时区待核对')
    for start, end, title in (('registration_start', 'registration_end', '报名'),
                              ('tournament_start', 'tournament_end', '比赛')):
        if start in parsed and end in parsed and parsed[start] >= parsed[end]:
            issues.append(f'{title}结束时间须晚于开始时间')
    return issues


def _validate_tracks_rules(config):
    issues = []
    keys = set()
    folded_keys = set()
    for track in config['tracks']:
        _fields(track, TRACK_FIELDS, '赛道')
        _text(track['key'], 30, '赛道标识', required=True)
        _text(track['name'], 50, '赛道名称', required=True)
        _boolean(track['is_active'], '赛道启用状态')
        _integer(track['max_per_team'], '赛道每队人数上限', 1)
        _integer(track['sort_order'], '赛道排序', -(2**31))
        _integer(track['difficulty_min'], '最低难度')
        _integer(track['difficulty_max'], '最高难度')
        _require(type(track['difficulty_step']) in (int, float), '难度阶级须为数值')
        try:
            parse_track_difficulty(track)
        except (SeasonError, OverflowError) as error:
            issues.append(f"赛道「{track['key']}」的难度或倍率不兼容：{error}")
        if track['key'].casefold() in folded_keys:
            issues.append(f"赛道标识重复：{track['key']}")
        keys.add(track['key'])
        folded_keys.add(track['key'].casefold())
        if track['is_active'] and len(track['key']) > 20:
            issues.append(f"赛道标识超过比赛字段上限：{track['key']}")
    if not any(track['is_active'] for track in config['tracks']):
        issues.append('缺少启用赛道')
    for rule in config['rules']:
        _fields(rule, RULE_FIELDS, '规则')
        _text(rule['title'], 200, '规则标题', required=True)
        _text(rule['content'], 65535, '规则正文')
        _require(len(rule['content'].encode('utf-8')) <= 65535, '规则正文超出数据库容量')
        _text(rule['roguelike_key'], 30, '规则赛道标识', nullable=True, required=True)
        _integer(rule['sort_order'], '规则排序', -(2**31))
        _boolean(rule['is_published'], '规则发布状态')
        if rule['roguelike_key'] is not None and rule['roguelike_key'] not in keys:
            issues.append(f"规则「{rule['title']}」引用缺失赛道：{rule['roguelike_key']}")
        if rule['is_published'] and not rule['content'].strip():
            issues.append(f"已发布规则缺少正文：{rule['title']}")
    if not any(rule['is_published'] for rule in config['rules']):
        issues.append('缺少已发布的赛事规则')
    return issues


def _validate_media(config):
    issues = []
    refs = []
    for item in config['carousel']:
        _fields(item, CAROUSEL_FIELDS, '轮播条目')
        _require(item['media_type'] in ('image', 'video'), '轮播媒体类型不兼容')
        _text(item['file_path'], 500, '轮播素材路径', required=True)
        _text(item['title'], 200, '轮播标题', nullable=True)
        _text(item['link_url'], 500, '轮播链接', nullable=True)
        _integer(item['sort_order'], '轮播排序', -(2**31))
        _boolean(item['is_active'], '轮播启用状态')
        if item['link_url'] and not ns()._carousel_link(item['link_url']):
            issues.append(f"轮播链接不兼容：{item['title'] or item['file_path']}")
    for media in config['media']:
        _fields(media, ('root', 'path', 'sha256'), '素材')
        _require(media['root'] in ('image', 'uploads'), '素材目录不兼容')
        _text(media['path'], 500, '素材路径', required=True)
        _require(_safe_relative(media['path']), '素材路径不兼容')
        _require(media['sha256'] is None or _hash(media['sha256']), '素材校验值无效')
        reference = media['root'], media['path']
        refs.append(reference)
        actual = _media_hash(*reference)
        label = '/'.join(reference)
        if actual is None or media['sha256'] is None:
            issues.append(f'素材缺失或未记录校验值：{label}')
        elif actual != media['sha256']:
            issues.append(f'素材校验值不同：{label}')
    expected = _media_refs(config['carousel'])
    if refs != expected:
        missing = sorted(set(expected) - set(refs))
        extra = sorted(set(refs) - set(expected))
        issues.extend(f'素材清单缺少：{root}/{path}' for root, path in missing)
        issues.extend(f'目标缺少引用或素材：{root}/{path}' for root, path in extra)
        if not missing and not extra:
            issues.append('素材清单顺序或重复引用不兼容')
    return issues


def validate_manifest(manifest, *, imported=False):
    _fields(manifest, ('sha256', 'payload'), '发布包')
    payload = manifest['payload']
    try:
        _require(_hash(manifest['sha256']) and manifest['sha256'] == _seal(payload)['sha256'], '发布包校验值不匹配')
    except (UnicodeError, ValueError, RecursionError, TypeError) as error:
        if isinstance(error, SeasonError):
            raise
        raise SeasonError('发布包内容格式无效', 400) from error
    _fields(payload, ('format_version', 'application_version', 'schema_version', 'score_contract',
                      'identity', 'environment', 'configuration', 'publication', 'source'), '发布包内容')
    _require(type(payload['format_version']) is int and payload['format_version'] == FORMAT_VERSION, '发布包格式版本不兼容')
    _require(type(payload['schema_version']) is int and 5 <= payload['schema_version'] <= SCHEMA_VERSION,
             '发布包数据库结构版本不兼容')
    _require(payload['score_contract'] == SCORING, '整场结算、取整或晋级合同不兼容')
    _text(payload['application_version'], 200, '应用版本', required=True)
    _identity(payload['identity'])
    _origin(payload['source'])
    publication = payload['publication']
    if imported or publication is not None:
        _fields(publication, ('version', 'publisher', 'published_at', 'context_version', 'stage', 'registration_open'), '发布记录')
        _integer(publication['version'], '发布版本号', 1)
        _integer(publication['context_version'], '规则快照版本号', 1)
        _text(publication['publisher'], 30, '发布人', required=True)
        _text(publication['published_at'], 40, '发布时间', required=True)
        _require(publication['stage'] in ('draft', 'registration', 'matching', 'running', 'finished'), '赛事阶段不兼容')
        _boolean(publication['registration_open'], '发布时的报名状态')
    config = payload['configuration']
    _fields(config, ('team_size', 'allow_incomplete_teams', 'advance_count', 'bonus_score', 'windows',
                     'window_times_utc', 'tracks', 'rules', 'carousel', 'media'), '赛事配置')
    for section in ('tracks', 'rules', 'carousel', 'media'):
        _require(isinstance(config[section], list), f'{section} 须为列表')
    environment = payload['environment']
    _fields(environment, ENVIRONMENT_FIELDS, '部署配置')
    issues = [f'部署配置不一致：{field}' for field, current in _environment().items()
              if current != environment[field]]
    return issues + _validate_settings(config) + _validate_tracks_rules(config) + _validate_media(config)


def published_releases():
    a = ns()
    return a.SeasonRelease.query.filter(a.SeasonRelease.version > 0)


def release_draft(season):
    return ns().SeasonRelease.query.filter_by(tournament_id=season.id, version=0).one_or_none()


def _set_draft(season, manifest):
    a = ns()
    draft = release_draft(season)
    if draft is None:
        draft = a.SeasonRelease(tournament_id=season.id, version=0)
        a.db.session.add(draft)
    draft.checksum = manifest['sha256']
    draft.manifest = json_dump(manifest)
    a.db.session.commit()


def _read_draft(season):
    draft = release_draft(season)
    if draft is None or draft.checksum != request.form.get('checksum'):
        raise SeasonError('草稿已变更，请刷新后重新确认')
    return draft, json.loads(draft.manifest)


def _check(issues):
    if issues:
        raise SeasonError('；'.join(issues), 400)


def _publish(season, draft, manifest):
    a = ns()
    payload = manifest['payload']
    _require(payload['publication'] is None, '请先应用导入配置，再从当前配置生成发布草稿')
    _check(validate_manifest(manifest))
    if _capture(season) != payload:
        raise SeasonError('当前配置或素材已变化，请重新生成草稿')
    latest = published_releases().filter_by(tournament_id=season.id).order_by(a.SeasonRelease.version.desc()).first()
    version = latest.version + 1 if latest else 1
    save_context(season)
    payload['publication'] = {'version': version, 'publisher': current_user.nickname,
                              'published_at': datetime.now(timezone.utc).isoformat(),
                              'context_version': season.snapshot_version, 'stage': season.status,
                              'registration_open': bool(a.Setting.get_instance().registration_open)}
    published = _seal(payload)
    draft.version = version
    draft.checksum = published['sha256']
    draft.manifest = json_dump(published)
    a.log_system_action('publish_season_release', json_dump({'season': season.id, 'version': version, 'sha256': draft.checksum}))
    a.db.session.commit()
    return f'已发布第 {version} 版赛事配置'


def can_apply_release(season):
    a = ns()
    setting = a.Setting.get_instance()
    return (season.status == 'registration' and not setting.registration_open
            and season.teams.first() is None and season.matches.first() is None)


def _import_configuration(season, config):
    a = ns()
    setting = a.Setting.get_instance()
    setting.team_size = config['team_size']
    setting.allow_incomplete_teams = config['allow_incomplete_teams']
    setting.advance_count = config['advance_count']
    season.bonus_score = config['bonus_score']
    season.window_times_utc = config['window_times_utc']
    for field, value in config['windows'].items():
        setattr(season, field, datetime.fromisoformat(value) if value else None)
    target_tracks = {track.key: track for track in a.Roguelike.query.all()}
    for cfg in config['tracks']:
        key = cfg['key']
        record = target_tracks.pop(key, None)
        if record is None:
            record = a.Roguelike(key=key)
            a.db.session.add(record)
        for field in TRACK_FIELDS[1:]:
            setattr(record, field, cfg[field])
    for record in target_tracks.values():
        record.is_active = False
    a.Rule.query.delete()
    a.CarouselItem.query.filter_by(tournament_id=season.id).delete()
    a.db.session.add_all(a.Rule(**row) for row in config['rules'])
    a.db.session.add_all(a.CarouselItem(tournament_id=season.id, **row) for row in config['carousel'])


def _apply(season, draft, manifest):
    a = ns()
    _check(validate_manifest(manifest, imported=True))
    if not can_apply_release(season):
        raise SeasonError('仅可向报名关闭且尚无队伍或比赛的新赛季应用配置')
    if a.Tournament.query.filter_by(is_active=False, context_snapshot=None).first():
        raise SeasonError('存在未核对的往届规则，请先保存人工确认的历史上下文')
    unresolved = a.Match.query.filter(a.Match.parent_id.is_(None), a.Match.scoring_rule.is_(None),
        a.db.or_(a.Match.player_a_score.is_not(None), a.Match.player_b_score.is_not(None))).first()
    if unresolved:
        raise SeasonError(f'比赛 {unresolved.id} 的历史倍率尚未核对，请先核对再应用全局配置')
    payload = manifest['payload']
    source = {'identity': payload['identity'], 'version': payload['publication']['version'], 'sha256': manifest['sha256']}
    season_id = season.id
    # Close the read transaction before the independent backup. The request holds the maintenance lock.
    a.db.session.rollback()
    try:
        batch = create_backup(a.db.engine, current_app.config, f'apply-season-release:{season_id}')
    except (OSError, RuntimeError, SQLiteError, SQLAlchemyError, TimeoutExpired) as error:
        current_app.logger.exception('发布包应用前备份失败')
        raise SeasonError('应用前备份失败，未修改赛事配置') from error
    try:
        _import_configuration(season, payload['configuration'])
        season.release_source = json_dump(source)
        a.log_system_action('apply_season_release', json_dump({'season': season_id, 'source': source, 'backup': batch.name}))
        a.db.session.delete(draft)
        a.db.session.commit()
    except IntegrityError as error:
        raise SeasonError('配置与目标数据库的赛道标识或规则约束冲突', 400) from error
    try:
        prune_backups(current_app.config)
    except OSError:
        current_app.logger.exception('导入完成，备份轮转失败')
    return '已应用赛事配置，报名保持关闭'


def release_markdown(manifest):
    payload = manifest['payload']
    publication = payload['publication']
    identity = payload['identity']
    config = payload['configuration']
    summary = {'赛事': identity['name'], '赛事标识': identity['event'], '届次': identity['season'],
               '发布版本': publication['version'], '发布人': publication['publisher'],
               '发布时间（UTC）': publication['published_at'], '规则快照版本': publication['context_version'],
               '应用版本': payload['application_version'], '数据库结构版本': payload['schema_version'],
               '队伍人数': config['team_size'], '队伍倍率': config['bonus_score'],
               '晋级名额': config['advance_count'], '校验值（SHA-256）': manifest['sha256']}
    lines = ['# 赛事发布包', '', *(f"    {key}　{' '.join(str(value).splitlines())}" for key, value in summary.items()), '',
             '## 配置清单', '', '正式比赛按整场确认结算，难度倍率乘原始成绩后向零取整。',
             '队员比赛分和有效人工调整求和后乘队伍倍率。并列名次占位跳号，晋级线同分全进。', '',
             '计划时间按 UTC 保存，显示时区见部署设置。素材清单路径相对于素材目录或上传目录。', '',
             *('    ' + line for line in json.dumps(payload, ensure_ascii=False, indent=2).splitlines())]
    return '\n'.join(lines)


def _uploaded_manifest():
    upload = request.files.get('package')
    if upload is None:
        raise SeasonError('请选择赛事发布包 JSON 文件', 400)
    try:
        return json.load(upload.stream)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise SeasonError('无法读取发布包 JSON', 400) from error


def change_release(action):
    a = ns()
    season = active_season()
    if season.id != request.form.get('season_id', type=int):
        raise SeasonError('活跃赛季已变更，请刷新后重试')
    if action == 'capture':
        _set_draft(season, _seal(_capture(season)))
        return '已生成当前配置草稿，请检查校验结果'
    if action in ('upload', 'copy'):
        if action == 'upload':
            manifest = _uploaded_manifest()
        else:
            release = published_releases().filter_by(id=request.form.get('release_id', type=int)).first_or_404()
            manifest = json.loads(release.manifest)
        validate_manifest(manifest, imported=True)
        _set_draft(season, manifest)
        return '已载入配置草稿，请检查兼容性和素材'
    draft, manifest = _read_draft(season)
    if action == 'publish':
        return _publish(season, draft, manifest)
    if action == 'apply':
        return _apply(season, draft, manifest)
    if action == 'discard':
        a.db.session.delete(draft)
        a.db.session.commit()
        return '已清除当前草稿'
    raise SeasonError('未知发布包操作', 400)
