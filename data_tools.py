"""Backup inventory and season-scoped spreadsheet exports."""
import json
import re
from datetime import datetime
from pathlib import Path

from flask import current_app
from openpyxl import Workbook

from backup import verify_backup
from formal_match import formal_query, has_outcome, is_settled, summarize_formal_matches
from scoring import get_member_score_breakdown
from season_safety import SeasonError, final_score, verify_match_ownership

BATCH_NAME = re.compile(r'\d{8}T\d{12}Z-[0-9a-f]{8}')
INVENTORY_ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError)


def backup_inventory():
    """List finalized batches with a valid manifest and file sizes."""
    root = Path(current_app.config['BACKUP_DIR']).resolve()
    try:
        paths = sorted(root.iterdir(), reverse=True) if root.is_dir() else []
    except OSError:
        current_app.logger.exception('无法读取备份目录')
        return []
    batches = []
    for path in paths:
        try:
            batches.append(_inventory_entry(root, path.name))
        except INVENTORY_ERRORS:
            continue
    return batches


def _inventory_entry(root, batch):
    if not BATCH_NAME.fullmatch(batch):
        raise ValueError('invalid batch ID')
    path = root / batch
    manifest_path = path / 'manifest.json'
    if path.is_symlink() or not path.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('invalid batch directory')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    payload = {'sqlite': 'database.db', 'mysql': 'database.sql'}.get(manifest['database_type'])
    files = manifest['files']
    if (manifest['batch'] != batch or manifest['complete'] is not True
            or payload is None or not isinstance(files, dict) or set(files) != {payload, 'media.tar.gz'}):
        raise ValueError('incomplete batch')
    datetime.fromisoformat(manifest['created_at'])
    for name, expected in files.items():
        item = path / name
        if (type(expected['bytes']) is not int or expected['bytes'] <= 0
                or item.is_symlink() or not item.is_file() or item.stat().st_size != expected['bytes']):
            raise ValueError('invalid backup file')
    return manifest


def backup_download_file(batch, filename):
    """Resolve a whitelisted file after rechecking the entire backup."""
    root = Path(current_app.config['BACKUP_DIR']).resolve()
    manifest = _inventory_entry(root, batch)
    if filename not in {'manifest.json', *manifest['files']}:
        raise ValueError('invalid filename')
    verify_backup(root / batch)
    season = manifest.get('season_number')
    prefix = f'season-{season}-' if type(season) is int else ''
    return root / batch / filename, f'{prefix}{batch}-{filename}'


def append_text_safe(sheet, values):
    """Store user-controlled strings as literal text in Excel."""
    sheet.append(values)
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = 's'


def build_season_workbook(tournament, rankings, advancement):
    from app import Match, Team, TeamMember, db, get_labels, get_roguelike_name, get_team_size

    workbook = Workbook()
    registration = workbook.active
    registration.title = '报名信息'
    teams = sorted((row['team'] for row in rankings), key=lambda team: team.id)
    team_size = get_team_size()
    track_label = get_labels()['track']
    append_text_safe(registration, ['队伍名称', '队长昵称']
                     + [f'成员{i + 1}' for i in range(team_size)]
                     + [track_label + '选择', '参赛宣言', '个人积分'])
    for team in teams:
        members = TeamMember.query.filter_by(team_id=team.id).order_by(TeamMember.id).all()
        names = [member.user.nickname for member in members[:team_size]]
        names += [''] * (team_size - len(names))
        tracks = [f'{m.user.nickname}: {get_roguelike_name(m.roguelike)}' for m in members if m.roguelike]
        declarations = [f'{m.user.nickname}: {m.declaration}' for m in members if m.declaration]
        scores = [f'{m.user.nickname}: {m.final_score}' for m in members]
        append_text_safe(registration, [
            team.team_name, team.captain.nickname, *names,
            '\n'.join(tracks), '\n'.join(declarations), '\n'.join(scores),
        ])

    match_sheet = workbook.create_sheet('比赛结果')
    append_text_safe(match_sheet, ['届数', '整场 ID', track_label, 'A 队伍', 'A 选手',
                     'A 原始分', 'A 难度', 'A 倍率', 'A 已结算积分',
                     'B 队伍', 'B 选手', 'B 原始分', 'B 难度', 'B 倍率', 'B 已结算积分',
                     '比赛状态', '胜负结果', '结算状态', '审核状态', '规则快照版本'])
    match_query = formal_query(Match, db, tournament.id)
    for match in match_query.order_by(Match.id):
        verify_match_ownership(match)
        settled = is_settled(match)
        if match.settlement_state == 'settled' and not settled:
            raise SeasonError(f'比赛 {match.id} 的结算状态与成绩不一致，请先核对')
        outcome = None
        if has_outcome(match):
            outcome = match.winner.nickname if match.winner else '平局'
        append_text_safe(match_sheet, [
            tournament.season_number, match.id, get_roguelike_name(match.roguelike),
            match.team_a.team_name, match.player_a.nickname, match.player_a_score,
            match.player_a_difficulty, match.player_a_multiplier, final_score(match, 'a') if settled else None,
            match.team_b.team_name, match.player_b.nickname, match.player_b_score,
            match.player_b_difficulty, match.player_b_multiplier, final_score(match, 'b') if settled else None,
            match.status, outcome, match.settlement_state, match.review_status, match.context_version,
        ])

    team_sheet = workbook.create_sheet('队伍积分榜')
    team_sheet.append(['名次', '队伍', '队长', '队伍积分', '晋级'])
    for row in rankings:
        team = row['team']
        append_text_safe(team_sheet, [row['rank'], team.team_name, team.captain.nickname,
                                      row['points'], '是' if team.id in advancement['team_ids'] else '否'])
    # Keep the existing five standings columns and the filter scope intact.
    team_table = team_sheet.dimensions
    totals = summarize_formal_matches(match_query, Match)
    for index, (label, value) in enumerate((
        ('配置晋级名额', advancement['quota']), ('实际晋级队伍', advancement['count']),
        ('正式场次', totals['formal']), ('已判胜负', totals['decided']), ('已结算', totals['settled']),
    ), start=1):
        team_sheet.cell(index, 7, label)
        team_sheet.cell(index, 8, value)

    member_sheet = workbook.create_sheet('队员积分榜')
    member_sheet.append(['队员', '队伍', '比赛累计', '有效调整', '最终积分', '对账状态'])
    members = (TeamMember.query.join(Team).filter(Team.tournament_id == tournament.id)
               .order_by(TeamMember.final_score.desc(), TeamMember.id))
    for member in members:
        try:
            breakdown = get_member_score_breakdown(member.id)
        except SeasonError:
            breakdown = {'match_score': None, 'adjustment_score': None, 'final_score': None}
        stored = member.final_score
        append_text_safe(member_sheet, [
            member.user.nickname, member.team.team_name, breakdown['match_score'],
            breakdown['adjustment_score'], stored,
            '一致' if breakdown['final_score'] is not None and stored == breakdown['final_score'] else '待核对',
        ])

    for sheet in workbook:
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = team_table if sheet is team_sheet else sheet.dimensions
        for column in sheet.columns:
            width = max((len(str(cell.value)) for cell in column if cell.value is not None), default=0)
            sheet.column_dimensions[column[0].column_letter].width = min(44, max(14, width + 4))
    return workbook
