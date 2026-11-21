"""Private operator handoff for an identified tournament season."""
from datetime import datetime
from hashlib import sha256
import json

from flask import current_app, url_for
from flask_login import current_user

from data_tools import INVENTORY_ERRORS, backup_download_file, backup_inventory
from management import data_report
from migrations import SCHEMA_VERSION
from season_health import get_season_todos
from season_safety import SeasonError, json_dump, ns, unfinished_matches

NOTE_FIELDS = {
    'operations': '管理员操作说明', 'refereeing': '裁判操作说明',
    'known_issues': '已知问题', 'temporary_config': '临时配置', 'disputes': '争议处理记录',
}
ISSUE_LABELS = {
    'missing_season': '缺少比赛届次', 'ownership_conflict': '比赛与队伍届次冲突',
    'missing_historical_rule': '缺少历史计分规则', 'legacy_settlement_unknown': '旧赛果结算状态待核对',
    'scored_segment_needs_explicit_merge': '有成绩的分时赛段待人工合并',
}
NEXT_STEPS = (
    '按清单核对发布版本、历史待核对项及未结算整场。',
    '下载完整备份批次，按清单记录的应用与 schema 版本在隔离库恢复。',
    '在赛事设置创建下一届，视需要继承或应用已发布配置。',
    '核对下一届规则与赛道、时间、素材及晋级名额，处理仪表盘待办后手动开放报名。',
)


def _handoff_row(season):
    return ns().SeasonHandoff.query.filter_by(tournament_id=season.id).one_or_none()


def _backup_reference(batch):
    files = {name: url_for('admin.download_backup', batch=batch['batch'], filename=name)
             for name in (*batch['files'], 'manifest.json')}
    keys = ('batch', 'created_at', 'code_version', 'schema_version', 'database_type', 'reason')
    return {key: batch.get(key) for key in keys} | {'files': files}


def _release_reference(season):
    a = ns()
    latest = (a.SeasonRelease.query.filter_by(tournament_id=season.id).filter(a.SeasonRelease.version > 0)
              .order_by(a.SeasonRelease.version.desc()).first())
    if latest is None:
        return None
    contents = json.loads(latest.manifest)['payload']
    return {'version': latest.version, 'sha256': latest.checksum, 'application_version': contents['application_version'],
            'schema_version': contents['schema_version'], 'rule_context_version': contents['publication']['context_version'],
            'files': {ext: url_for('admin.download_release_pack', release_id=latest.id, file_format=ext) for ext in ('json', 'md')}}


def _reconciliation(season):
    a = ns()
    report = data_report()
    team_ids = {team_id for (team_id,) in a.Team.query.with_entities(a.Team.id).filter_by(tournament_id=season.id)}
    matches = [row for row in report['matches'] if row['season'] in (None, season.id)
               or season.id in row['candidate_seasons_from_teams']]
    relevant_members = [row for row in report['member_totals'] if row['team_id'] in team_ids]
    suspect_members = [row['id'] for row in relevant_members if row['breakdown'] is None
                       or row['stored_total'] != row['breakdown']['final_score']]
    return {
        'missing_rules_snapshot': any(row['id'] == season.id for row in report['contexts_missing']),
        'matches': [{'match_id': row['id'], 'season_id': row['season'], 'issues': row['issues'],
                     'messages': [ISSUE_LABELS.get(code, code) for code in row['issues']]} for row in matches],
        'unassigned_team_ids': [row['id'] for row in report['orphan_teams']],
        'member_ids_to_review': suspect_members,
        'command': "FLASK_SKIP_DOTENV=1 python -m flask --app 'app:create_app()' data-report",
        'correction_url': url_for('p0_correct_history', tid=season.id) if season.is_active is False else None,
    }


def _export_audit(season):
    a = ns()
    records = (a.SystemLog.query.filter_by(action='download_season_excel')
               .filter(a.SystemLog.detail.contains('"season_id": ' + str(season.id))).order_by(a.SystemLog.id.desc()))
    downloads = []
    for record in records:
        try:
            detail = json.loads(record.detail)
        except (TypeError, ValueError):
            continue
        if detail.get('season_id') == season.id:
            downloads.append({'audit_id': record.id, 'operator_id': record.user_id, 'at_utc': record.created_at.isoformat() + 'Z'})
    return downloads


def _transition_reference(season):
    transition = ns().SeasonTransition.query.filter_by(expected_season_id=season.id).one_or_none()
    if transition is None:
        return None
    return {'id': transition.id, 'new_season_id': transition.new_season_id, 'operator_id': transition.actor_id,
            'backup_batch': transition.backup_batch, 'forced_reason': transition.forced_reason,
            'unfinished_ids': json.loads(transition.unfinished_ids)}


def _next_season(season, transition):
    a = ns()
    if transition:
        following = a.db.session.get(a.Tournament, transition['new_season_id'])
    else:
        following = (a.Tournament.query.filter(a.Tournament.season_number > season.season_number)
                     .order_by(a.Tournament.season_number).first())
    if following is None:
        return None
    return {'season_id': following.id, 'season_number': following.season_number,
            'handoff_url': url_for('admin.season_handoff', season_id=following.id),
            'items': get_season_todos(following) if following.is_active else None}


def build_handoff(season):
    a = ns()
    state = _handoff_row(season)
    snapshot = json.loads(season.context_snapshot) if season.context_snapshot else None
    backs = [_backup_reference(batch) for batch in backup_inventory() if batch.get('season_number') == season.season_number]
    recoveries = json.loads(state.recoveries) if state else []
    available = {batch['batch'] for batch in backs}
    transition = _transition_reference(season)
    archived_url = url_for('main.history_detail', tournament_id=season.id) if season.is_active is False else None
    can_export = season.is_active or (archived_url and snapshot and snapshot.get('advance_count') is not None)
    return {
        'format_version': 1, 'generated_at_utc': datetime.utcnow().isoformat() + 'Z',
        'season': {'id': season.id, 'number': season.season_number, 'name': season.name,
                   'is_active': season.is_active, 'status': season.status,
                   'results_url': url_for('tournament.results') if season.is_active else archived_url},
        'deployment': {'application_version': current_app.config['APP_VERSION'], 'schema_version': SCHEMA_VERSION,
                       'image': current_app.config.get('DEPLOYMENT_IMAGE')},
        'rules': {'snapshot_version': season.snapshot_version or 0,
                  'titles': [rule['title'] for rule in snapshot['rules']] if snapshot else [],
                  'url': url_for('main.rules') if season.is_active else archived_url},
        'published_config': _release_reference(season), 'backups': backs, 'transition': transition,
        'configuration_source': json.loads(season.release_source) if season.release_source else None,
        'restores': [dict(entry, batch_available=entry['batch'] in available) for entry in recoveries],
        'exports': {'excel_url': url_for('admin.handoff_excel', season_id=season.id) if can_export else None,
                    'score_log_url': url_for('admin.export_score_logs'), 'excel_downloads': _export_audit(season)},
        'unfinished_matches': [{'id': match.id, 'status': match.status, 'settlement_state': match.settlement_state,
                                'url': url_for('tournament.match_detail', match_id=match.id)}
                               for match in unfinished_matches(season.id)],
        'adjustments': [{'id': adj.id, 'member_id': adj.member_id, 'delta': adj.delta, 'revoked': adj.revoked,
                         'actor_id': adj.actor_id, 'revoked_by': adj.revoked_by, 'reason': adj.reason,
                         'revoke_reason': adj.revoke_reason, 'at_utc': adj.created_at.isoformat() + 'Z'}
                        for adj in a.ScoreAdjustment.query.filter_by(tournament_id=season.id).order_by(a.ScoreAdjustment.id)],
        'reconciliation': _reconciliation(season), 'next_season': _next_season(season, transition),
        'next_steps': list(NEXT_STEPS),
        'notes': json.loads(state.notes) if state else {}, 'revision': state.revision if state else 0,
        'notes_updated_at_utc': state.updated_at.isoformat() + 'Z' if state else None,
    }


def _checked_text(value, limit):
    text = (value or '').strip()
    if len(text) > limit or '\x00' in text:
        raise SeasonError(f'交接说明最多 {limit} 个字符', 400)
    return text


def update_handoff(season, form):
    a = ns()
    row = _handoff_row(season)
    if form.get('revision', type=int) != (row.revision if row else 0):
        raise SeasonError('交接记录已有更新，请刷新后重试')
    if row is None:
        row = a.SeasonHandoff(tournament_id=season.id, revision=0, notes='{}', recoveries='[]')
        a.db.session.add(row)
    operation = form.get('operation')
    if operation == 'notes':
        row.notes = json_dump({key: _checked_text(form.get(key), 2000) for key in NOTE_FIELDS})
        action, detail = 'update_handoff_notes', {}
    elif operation == 'recovery':
        batch = form.get('batch', '')
        result = form.get('result')
        reference = _checked_text(form.get('reference'), 500)
        if result not in ('passed', 'failed') or not reference:
            raise SeasonError('请选择演练结果并填写隔离恢复记录或凭证编号', 400)
        try:
            path, _ = backup_download_file(batch, 'manifest.json')
            raw_manifest = path.read_bytes()
            manifest = json.loads(raw_manifest)
            code_version, schema_version = manifest['code_version'], manifest['schema_version']
            if not isinstance(code_version, str) or type(schema_version) is not int:
                raise ValueError('invalid backup version')
        except INVENTORY_ERRORS as error:
            raise SeasonError('完整备份批次不可用', 400) from error
        if manifest.get('season_number') != season.season_number:
            raise SeasonError('备份批次不属于所选届次', 400)
        stamp = {'batch': batch, 'result': result, 'reference': reference, 'operator_id': current_user.id,
                 'at_utc': datetime.utcnow().isoformat() + 'Z', 'code_version': code_version,
                 'schema_version': schema_version, 'manifest_sha256': sha256(raw_manifest).hexdigest()}
        recoveries = json.loads(row.recoveries)
        recoveries.append(stamp)
        row.recoveries = json_dump(recoveries)
        action, detail = 'record_handoff_recovery', {'batch': batch, 'result': result, 'reference': reference}
    else:
        raise SeasonError('未知交接操作', 400)
    row.revision += 1
    row.updated_at = datetime.utcnow()
    a.log_system_action(action, json_dump(dict(detail, season_id=season.id, revision=row.revision)))
    a.db.session.commit()


def handoff_markdown(package):
    season = package['season']
    deployment = package['deployment']
    lines = ['# 赛事运营交接', '', f"    第 {season['number']} 届　{' '.join(season['name'].splitlines())}",
             f"    生成时间（UTC）　{package['generated_at_utc']}",
             f"    应用版本　{deployment['application_version']}　数据库结构　{deployment['schema_version']}",
             f"    交接说明版本　{package['revision']}",
             '', '## 接手步骤', '', *(f'{index}. {step}' for index, step in enumerate(package['next_steps'], 1))]
    for key, label in NOTE_FIELDS.items():
        value = package['notes'].get(key)
        if value:
            lines.extend(['', '## ' + label, '', *('    ' + line for line in value.splitlines())])
    sections = (
        ('赛事与规则', {key: package[key] for key in ('season', 'rules')}),
        ('部署与配置来源', {key: package[key] for key in ('deployment', 'published_config', 'configuration_source')}),
        ('备份与恢复', {key: package[key] for key in ('backups', 'restores', 'transition')}),
        ('结果与导出', package['exports']),
        ('未结算与调整审计', {key: package[key] for key in ('unfinished_matches', 'adjustments')}),
        ('历史待核对项', package['reconciliation']),
        ('下一届配置检查', package['next_season']),
    )
    for title, values in sections:
        lines.extend(['', '## ' + title, '',
                      *('    ' + line for line in json.dumps(values, ensure_ascii=False, indent=2).splitlines())])
    return '\n'.join(lines) + '\n'
