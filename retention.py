"""Preview and audit explicit retention cleanup under the application write lock."""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.exc import SQLAlchemyError

from backup import media_references
from season_safety import SeasonError, json_dump, match_scope_of, root_match

CLEANUP_ACTION = 'retention_cleanup'
TOKEN_TTL = 15 * 60
UPLOAD_URL = re.compile(r'(?:^|[^A-Za-z0-9_])static/uploads/((?:avatars|teams|screenshots)/[A-Za-z0-9_.-]+)')
MEDIA_FILENAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:png|jpe?g|gif|webp)', re.IGNORECASE)
SCREENSHOT_NAME = re.compile(
    r'screenshot_([1-9][0-9]{0,17})_([1-9][0-9]{0,17})_[A-Za-z0-9_-]+\.(?:png|jpe?g|webp)', re.IGNORECASE)


@dataclass(frozen=True)
class CleanupFile:
    path: Path
    identity: tuple


@dataclass
class CleanupPlan:
    root: Path
    files: list[CleanupFile]
    log_ids: list[int]
    last_cleanup_id: int | None

    @property
    def counts(self):
        return Counter(item.path.parent.name for item in self.files)

    @property
    def fingerprint(self):
        state = [str(self.root), [(str(item.path), item.identity) for item in self.files],
                 self.log_ids, self.last_cleanup_id]
        return sha256(json_dump(state).encode()).hexdigest()


def cleanup_options(form):
    screenshot = form.get('screenshots', '')
    if screenshot not in ('', 'older_seasons', 'approved'):
        raise SeasonError('请选择有效的截图清理条件', 400)
    options = {folder: form.get(folder) == '1' for folder in ('avatars', 'teams')}
    options['screenshots'] = screenshot
    if screenshot == 'older_seasons':
        recent = form.get('recent_seasons', type=int)
        if recent is None or recent < 1:
            raise SeasonError('请填写要保留的届数，至少保留一届', 400)
        options['recent_seasons'] = recent
    if form.get('logs') == '1':
        days = form.get('log_days', type=int)
        if days is None or days < 1:
            raise SeasonError('请填写要保留的日志天数，至少保留一天', 400)
        try:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        except OverflowError as exc:
            raise SeasonError('日志保留天数超出可用日期范围', 400) from exc
        options['log_days'] = days
        options['cutoff'] = cutoff.isoformat(timespec='microseconds')
    if not any(options.values()):
        raise SeasonError('请选择至少一项清理内容', 400)
    return options


def _protected_uploads(root):
    from app import CarouselItem, Match, Rule, ScoreLog, SeasonContextVersion, SystemLog, Tournament, db

    protected = {(base / path).resolve() for base, path in
                 media_references(db.session.connection(), current_app.config)}
    # References can also appear in rendered rules, links and frozen audit text.
    for column in (CarouselItem.link_url, Match.match_note, Match.legacy_payload, Match.review_reason,
                   Match.forfeit_reason, Rule.content, Tournament.context_snapshot, SeasonContextVersion.context,
                   ScoreLog.old_result, ScoreLog.new_result, ScoreLog.reason, SystemLog.detail):
        query = db.select(column).where(
            column.contains('static/uploads/') | column.contains('%', autoescape=True) |
            column.contains(r'static\/uploads\/', autoescape=True)).execution_options(yield_per=300)
        for value in db.session.scalars(query):
            decoded = unquote(value.replace(r'\/', '/'))
            protected.update((root / name).resolve() for name in UPLOAD_URL.findall(decoded))
    return protected


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _orphan_files(root, folder, protected):
    directory = root / folder
    if directory.is_symlink() or not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if not MEDIA_FILENAME.fullmatch(path.name):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and path not in protected:
            yield CleanupFile(path.relative_to(root), _identity(info))


def _screenshot_files(files, options):
    from app import Match, Tournament, db

    retained = set()
    if options['screenshots'] == 'older_seasons':
        seasons = db.session.scalars(db.select(Tournament.id).where(Tournament.is_active.is_not(None))
                                     .order_by(Tournament.season_number.desc())).all()
        retained = set(seasons[:options['recent_seasons']])
    eligible = {}
    for item in files:
        parsed = SCREENSHOT_NAME.fullmatch(item.path.name)
        if not parsed:
            continue
        match_id = int(parsed[1])
        if match_id not in eligible:
            match = db.session.get(Match, match_id)
            eligible[match_id] = set()
            if match is not None and match_scope_of(match) != 'orphan':
                root = root_match(match)
                eligible_match = (root.review_status == 'approved' if options['screenshots'] == 'approved'
                                  else not root.tournament.is_active and root.tournament_id not in retained)
                if eligible_match:
                    eligible[match_id] = {root.player_a_id, root.player_b_id}
        if int(parsed[2]) in eligible[match_id]:
            yield item


def build_cleanup_plan(options):
    from app import SystemLog, db

    root = Path(current_app.config['UPLOAD_ROOT']).resolve()
    files = []
    if any(options[folder] for folder in ('avatars', 'teams', 'screenshots')):
        try:
            protected = _protected_uploads(root)
            for folder in ('avatars', 'teams'):
                if options[folder]:
                    files.extend(_orphan_files(root, folder, protected))
            if options['screenshots']:
                files.extend(_screenshot_files(_orphan_files(root, 'screenshots', protected), options))
        except (OSError, RuntimeError) as exc:
            raise SeasonError('无法核对素材目录与引用，请先检查存储配置') from exc
    log_ids = []
    if 'cutoff' in options:
        cutoff = datetime.fromisoformat(options['cutoff'])
        log_ids = db.session.scalars(db.select(SystemLog.id).where(SystemLog.created_at < cutoff)
                                    .order_by(SystemLog.id)).all()
    last_cleanup = db.session.scalar(db.select(db.func.max(SystemLog.id)).where(SystemLog.action == CLEANUP_ACTION))
    return CleanupPlan(root, files, log_ids, last_cleanup)


def _signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='retention-cleanup-v1')


def cleanup_token(options, plan, actor_id):
    return _signer().dumps({'options': options, 'fingerprint': plan.fingerprint, 'actor': actor_id})


def _unlink_candidate(root, file):
    # Anchor the operation to the selected directory and check the file again without following links.
    fd = os.open(root / file.path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.stat(file.path.name, dir_fd=fd, follow_symlinks=False)
        if _identity(info) != file.identity:
            return False
        os.unlink(file.path.name, dir_fd=fd)
        return True
    finally:
        os.close(fd)


def confirm_cleanup(token, actor_id):
    """Revalidate the preview, record the intent, and report the actual deletions."""
    from app import SystemLog, db, log_system_action

    try:
        preview = _signer().loads(token, max_age=TOKEN_TTL)
    except BadData as exc:
        raise SeasonError('清理预览无效或已过期，请重新预览', 400) from exc
    if preview['actor'] != actor_id:
        raise SeasonError('清理预览不属于当前操作者', 403)
    options = preview['options']
    plan = build_cleanup_plan(options)
    if plan.fingerprint != preview['fingerprint'] or not (plan.files or plan.log_ids):
        raise SeasonError('清理范围已变化或该预览已使用，请重新预览')

    deleted_logs = 0
    for offset in range(0, len(plan.log_ids), 500):
        ids = plan.log_ids[offset:offset + 500]
        deleted_logs += db.session.execute(db.delete(SystemLog).where(SystemLog.id.in_(ids))).rowcount
    report = {
        'state': 'pending_files' if plan.files else 'completed',
        'options': options,
        'planned_files': [str(item.path) for item in plan.files],
        'deleted_files': {},
        'deleted_system_logs': deleted_logs,
        'failed_files': [],
    }
    audit = log_system_action(CLEANUP_ACTION, json_dump(report))
    # Establish a durable audit before touching the filesystem.
    db.session.commit()
    if plan.files:
        audit_id = audit.id
        deleted_files = Counter()
        for item in plan.files:
            try:
                removed = _unlink_candidate(plan.root, item)
            except OSError:
                removed = False
                current_app.logger.exception('手动清理素材失败: %s', item.path)
            if removed:
                deleted_files[item.path.parent.name] += 1
            else:
                report['failed_files'].append(str(item.path))
        report['deleted_files'] = dict(deleted_files)
        report['state'] = 'partial' if report['failed_files'] else 'completed'
        audit.detail = json_dump(report)
        try:
            db.session.commit()
        except SQLAlchemyError as exc:
            current_app.logger.exception('清理结果写入失败，预写审计编号 %s', audit_id)
            raise SeasonError(f'清理已执行，结果记录失败。请按审计编号 {audit_id} 核对后重新预览。', 500) from exc
    return report
