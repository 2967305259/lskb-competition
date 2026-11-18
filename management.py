"""Explicit initialization, schema preparation, backup and reviewed legacy reconciliation."""
from pathlib import Path
import hashlib
import json

import click
from flask import current_app

from backup import create_backup, maintenance_lock, prune_backups, restore_backup
from migrations import SCHEMA_VERSION, assert_schema_current, migrate_database
from season_safety import (active_season, save_context, json_dump, scoring_rule, set_score,
                           SeasonError, _reconcile, _correction, apply_outcome, get_default_difficulty)


def initialize_database():
    import app as a
    if a.Tournament.query.count():
        active_season()  # Explicit failure for no/multiple active; never invent a replacement.
        return False
    if a.Team.query.count() or a.Match.query.count():
        raise SeasonError('存在无赛季数据，请先运行 data-report')
    season = a.Tournament(name=current_app.config['TOURNAMENT_NAME'],
        season_number=current_app.config['SEASON_NUMBER'], bonus_score=current_app.config['DEFAULT_BONUS_SCORE'],
        is_active=True, status='registration')
    a.db.session.add(season)
    default_difficulty = get_default_difficulty()
    for key, name in [('water', '水月与深蓝之树'), ('sami', '探索者的银凇止境'),
                      ('sarkaz', '萨卡兹的无终奇语'), ('garden', '岁的界园志异')]:
        if not a.Roguelike.query.filter_by(key=key).first():
            a.db.session.add(a.Roguelike(key=key, name=name, is_active=True, **default_difficulty))
    a.Setting.get_instance()
    for prefix, role in (('ADMIN', 'admin'), ('SUPER_ADMIN', 'super_admin')):
        username = current_app.config[f'{prefix}_USERNAME']
        if username and not a.User.query.filter_by(username=username).first():
            password = current_app.config[f'{prefix}_PASSWORD']
            if not password:
                raise SeasonError(f'首次创建 {role} 账号须显式设置 {prefix}_PASSWORD')
            user = a.User(username=username, nickname=username, role=role)
            user.set_password(password)
            a.db.session.add(user)
    a.db.session.flush()
    save_context(season)
    a.db.session.add(a.SystemLog(action='initialize', detail=json_dump({'season': season.id})))
    a.db.session.commit()
    return True


def data_report():
    import app as a
    report = {'active_ids': [t.id for t in a.Tournament.query.filter_by(is_active=True).all()],
              'orphan_teams': [], 'matches': [], 'contexts_missing': [], 'member_totals': [],
              'team_totals': [], 'adjustments': [],
              'season_windows': [{'id':t.id, 'start':str(t.tournament_start), 'end':str(t.tournament_end)} for t in a.Tournament.query.order_by(a.Tournament.id)]}
    for team in a.Team.query.order_by(a.Team.id):
        report['team_totals'].append({'id': team.id, 'stored_total': team.team_score, 'tournament_id': team.tournament_id})
        if team.tournament_id is None:
            report['orphan_teams'].append({'id': team.id, 'name': team.team_name, 'created_at': str(team.created_at),
                'candidate_seasons': [t.id for t in a.Tournament.query.all()
                    if team.created_at and t.tournament_start and t.tournament_end and t.tournament_start <= team.created_at <= t.tournament_end]})
    for match in a.Match.query.order_by(a.Match.id):
        issues = []
        candidates = {t.tournament_id for t in (match.team_a, match.team_b) if t and t.tournament_id is not None}
        if match.tournament_id is None:
            issues.append('missing_season')
        if candidates and candidates != {match.tournament_id}:
            issues.append('ownership_conflict')
        scored = match.player_a_score is not None or match.player_b_score is not None
        if scored and not match.scoring_rule:
            issues.append('missing_historical_rule')
        if match.settlement_state == 'legacy':
            issues.append('legacy_settlement_unknown')
        if match.parent_id and scored:
            issues.append('scored_segment_needs_explicit_merge')
        if issues:
            report['matches'].append({'id': match.id, 'parent_id': match.parent_id, 'season': match.tournament_id,
                'candidate_seasons_from_teams': sorted(candidates), 'issues': issues,
                'created_at': str(match.created_at),
                'team_a': {'id': match.team_a_id, 'season': match.team_a.tournament_id if match.team_a else None},
                'team_b': {'id': match.team_b_id, 'season': match.team_b.tournament_id if match.team_b else None},
                'candidate_seasons_from_dates': [t.id for t in a.Tournament.query.all() if match.created_at and t.tournament_start and t.tournament_end and t.tournament_start <= match.created_at <= t.tournament_end],
                'raw_a': match.player_a_score, 'raw_b': match.player_b_score,
                'difficulty_a': match.get_player_a_difficulty(), 'difficulty_b': match.get_player_b_difficulty(),
                'status': match.status})
    for season in a.Tournament.query.order_by(a.Tournament.id):
        if not season.context_snapshot:
            report['contexts_missing'].append({'id': season.id, 'name': season.name, 'active': season.is_active})
    for member in a.TeamMember.query.order_by(a.TeamMember.id):
        try:
            from scoring import get_member_score_breakdown
            breakdown = get_member_score_breakdown(member.id)
        except Exception:
            breakdown = None
        report['member_totals'].append({'id': member.id, 'team_id': member.team_id,
            'stored_total': member.final_score, 'breakdown': breakdown,
            'note': '差额不等于人工调整，须按历史规则核对'})
    for adjustment in a.ScoreAdjustment.query.order_by(a.ScoreAdjustment.id):
        report['adjustments'].append({
            'id': adjustment.id, 'tournament_id': adjustment.tournament_id,
            'member_id': adjustment.member_id, 'actor_id': adjustment.actor_id,
            'delta': adjustment.delta, 'reason': adjustment.reason,
            'revoked': adjustment.revoked, 'revoked_by': adjustment.revoked_by,
            'revoke_reason': adjustment.revoke_reason,
            'created_at': str(adjustment.created_at),
        })
    report['digest'] = hashlib.sha256(json_dump(report).encode()).hexdigest()
    return report


def reconcile_plan(plan, operator):
    """The plan is a human-approved artifact, never a heuristic data fixer."""
    import app as a
    from scoring import recalculate_all_scores
    if not operator or not operator.is_super_admin() or plan.get('confirmed') is not True or not str(plan.get('reason', '')).strip():
        raise SeasonError('核对方案须由超级管理员明确确认并填写原因', 403)
    with maintenance_lock(current_app.config):
        report = data_report()
        if plan.get('report_digest') != report['digest']:
            raise SeasonError('核对报告已变化，请重新生成和确认')
        a.db.session.rollback()
        batch = create_backup(a.db.engine, current_app.config, 'legacy-reconciliation')
        token = _reconcile.set(True)
        before = data_report()
        try:
            for item in plan.get('teams', []):
                team = a.Team.query.get_or_404(item['id'])
                if team.tournament_id is not None:
                    raise SeasonError('不能覆盖已有队伍赛季归属')
                a.Tournament.query.get_or_404(item['tournament_id'])
                team.tournament_id = item['tournament_id']
            for item in plan.get('seasons', []):
                season = a.Tournament.query.get_or_404(item['id'])
                context = item['context']
                required = {'version', 'name', 'season_number', 'bonus_score', 'team_size', 'positions', 'labels', 'tracks', 'rules', 'score_contract'}
                if not required <= set(context) or context['name'] != season.name or context['season_number'] != season.season_number:
                    raise SeasonError('人工确认的赛事上下文不完整或名称/届数不一致')
                if season.context_snapshot:
                    raise SeasonError('不能覆盖已确认的历史上下文')
                season.context_snapshot = json_dump(context)
                season.snapshot_version = 1
                a.db.session.add(a.SeasonContextVersion(tournament_id=season.id, version=1, context=season.context_snapshot))
            affected = set()
            for item in plan.get('matches', []):
                match = a.Match.query.get_or_404(item['id'])
                if match.scoring_rule:
                    raise SeasonError('该比赛已有确认规则，修改结果须走专项纠错而非旧数据核对')
                tid = item['tournament_id']
                if match.tournament_id not in (None, tid):
                    raise SeasonError('不能覆盖已有比赛归属')
                match.tournament_id = tid
                if match.parent_id:
                    raise SeasonError('分段不能直接确认为独立结算单元')
                if any(team is None or team.tournament_id != tid for team in (match.team_a, match.team_b)):
                    raise SeasonError('比赛双方队伍与确认赛季不一致')
                rule = item['rule']
                if rule.get('track_key') != match.roguelike or rule.get('version') != 1 or rule.get('rounding') != 'truncate_toward_zero':
                    raise SeasonError('历史难度规则版本/赛道/取整方式不一致')
                match.scoring_rule = json_dump(rule)
                season = a.db.session.get(a.Tournament, tid)
                match.context_version = season.snapshot_version or None
                for side in ('a', 'b'):
                    set_score(match, side, item['score_' + side], item['difficulty_' + side], 'reviewed_legacy')
                if match.status in ('forfeit_a', 'forfeit_b'):
                    match.forfeit_side = match.status[-1]
                    match.forfeit_reason = item.get('forfeit_reason') or '历史弃权，核对依据：' + plan['reason']
                apply_outcome(match, item['outcome'])
                match.settlement_state = 'settled'
                match.settled_at = __import__('datetime').datetime.utcnow()
                affected.add(tid)
            for item in plan.get('segments', []):
                match = a.Match.query.get_or_404(item['id'])
                if match.parent_id != item['formal_match_id']:
                    raise SeasonError('分段与整场映射不一致')
                root = a.Match.query.get_or_404(item['formal_match_id'])
                if root.parent_id or root.tournament_id != match.tournament_id:
                    raise SeasonError('分段归属冲突')
                # Preserve the exact old payload permanently; no original record is deleted.
                payload = {key: getattr(match, key) for key in ('player_a_score', 'player_b_score', 'player_a_result',
                    'player_b_result', 'player_a_difficulty', 'player_b_difficulty', 'winner_id', 'match_note', 'status')}
                if match.legacy_payload:
                    raise SeasonError('该分段已完成核对，不能覆盖原始证据')
                match.legacy_payload = json_dump(payload)
                match.player_a_score = match.player_b_score = None
                match.player_a_result = match.player_b_result = None
                match.settlement_state = 'segment'
                a.db.session.add(a.ScoreLog(match_id=match.id, modified_by=operator.id, old_result=json_dump(payload),
                                           new_result=json_dump({'formal_match_id': root.id}), reason=plan['reason']))
                affected.add(root.tournament_id)
            a.db.session.flush()
            old_totals = {m.id: m.final_score for m in a.TeamMember.query.all()}
            old_team_totals = {team.id: team.team_score for team in a.Team.query.all()}
            for tid in affected:
                correction = _correction.set(tid)
                try:
                    recalculate_all_scores(tid)
                finally:
                    _correction.reset(correction)
            decisions = {item['member_id']: item for item in plan.get('total_decisions', [])}
            for member in a.TeamMember.query.all():
                if member.final_score != old_totals[member.id]:
                    decision = decisions.get(member.id)
                    if not decision or decision.get('before') != old_totals[member.id] or decision.get('after') != member.final_score:
                        raise SeasonError(f'成员 {member.id} 的累计变化尚未明确确认；不能把差额自动转为调整分')
            team_decisions = {item['team_id']: item for item in plan.get('team_total_decisions', [])}
            for team in a.Team.query.all():
                if team.team_score != old_team_totals[team.id]:
                    decision = team_decisions.get(team.id)
                    if not decision or decision.get('before') != old_team_totals[team.id] or decision.get('after') != team.team_score:
                        raise SeasonError(f'队伍 {team.id} 的累计变化尚未明确确认')
            a.db.session.add(a.SystemLog(user_id=operator.id, action='legacy_reconciliation',
                detail=json_dump({'reason': plan['reason'], 'plan': plan, 'before': before, 'after': data_report(), 'backup': batch.name})))
            a.db.session.commit()
        except Exception:
            a.db.session.rollback()
            raise
        finally:
            _reconcile.reset(token)


def prepare_directory(directory, label):
    path = Path(directory).absolute()
    try:
        if path.is_dir():
            click.echo(f'{label}目录已存在：{path}')
            return
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise click.ClickException(f'无法准备{label}目录 {path}（{exc}）') from exc
    click.echo(f'已创建{label}目录：{path}')


def register_commands(app):
    @app.cli.command('prepare')
    def prepare():
        """Migrate once before workers; seed defaults only on an explicitly prepared empty instance."""
        import app as a
        with maintenance_lock(app.config):
            prepare_directory(app.config['IMAGE_DIR'], '轮播素材')
            uploads = Path(app.config['UPLOAD_ROOT'])
            prepare_directory(uploads, '上传')
            for name in ('avatars', 'teams', 'screenshots'):
                prepare_directory(uploads / name, '上传')
            batch = migrate_database(a.db.engine, app.config,
                prepare_sqlite_directory=lambda path: prepare_directory(path, 'SQLite 数据库'))
            initialize_database()
            assert_schema_current(a.db.engine)
            if batch:
                prune_backups(app.config)
            click.echo(f'schema={SCHEMA_VERSION}，迁移/初始化完成；已有赛道不会在重启时补回')
            click.echo(json_dump(data_report()))

    @app.cli.command('data-report')
    def report_command():
        """Report missing ownership/rules/outcomes without modifying any business row."""
        click.echo(json.dumps(data_report(), ensure_ascii=False, indent=2))

    @app.cli.command('reconcile')
    @click.option('--plan', type=click.Path(exists=True), required=True)
    @click.option('--operator', required=True)
    def reconcile_command(plan, operator):
        import app as a
        user = a.User.query.filter_by(username=operator).first()
        reconcile_plan(json.loads(Path(plan).read_text()), user)
        click.echo('已按人工确认方案核对，业务与审计统一提交')

    @app.cli.command('backup')
    def backup_command():
        import app as a
        a.db.session.remove()
        batch = create_backup(a.db.engine, app.config)
        prune_backups(app.config)
        click.echo(str(batch))

    @app.cli.command('restore-rehearsal')
    @click.option('--batch', type=click.Path(exists=True), required=True)
    @click.option('--destination-url', required=True)
    @click.option('--media-destination', type=click.Path(), required=True)
    @click.option('--code-version', default=None, help='必须与备份清单中的源代码版本匹配')
    def restore_command(batch, destination_url, media_destination, code_version):
        from sqlalchemy import create_engine
        # This factory does not initialize/modify the source database.
        engine = create_engine(destination_url)
        restore_backup(batch, engine, media_destination, code_version or app.config['APP_VERSION'])
        click.echo('隔离恢复与行数校验通过；请用该版本应用指向恢复目录做页面验证')
