"""Validated uploads and a read-only inventory of storage and references."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
from math import isfinite
from pathlib import Path, PurePosixPath
import re
import subprocess
from uuid import uuid4
import warnings

from flask import current_app
from flask_login import current_user
from PIL import Image, ImageSequence

from backup import digest, media_references
from season_safety import SeasonError, ns

MIB = 1024 * 1024
ASSET_FOLDERS = ('avatars', 'teams', 'screenshots', 'carousel')
IMAGE_FORMATS = {'png': 'PNG', 'jpg': 'JPEG', 'jpeg': 'JPEG', 'gif': 'GIF', 'webp': 'WEBP'}
IMAGE_MIMES = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif', 'WEBP': 'image/webp'}
VIDEO_MIMES = {'mp4': 'video/mp4', 'mov': 'video/quicktime', 'webm': 'video/webm'}
IMAGE_LIMITS = {'avatars': 4 * MIB, 'teams': 4 * MIB, 'screenshots': 4 * MIB, 'carousel': 8 * MIB}
VIDEO_LIMIT = 14 * MIB
MAX_SCREENSHOTS = 3
MAX_IMAGE_PIXELS = 16_000_000


def _image_type(data, ext):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != IMAGE_FORMATS[ext] or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError()
                kind = image.format
                image.verify()
            with Image.open(BytesIO(data)) as image:
                total = 0
                for index, frame in enumerate(ImageSequence.Iterator(image)):
                    total += frame.width * frame.height
                    if index >= 120 or total > 64_000_000:
                        raise ValueError()
                    frame.load()
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise SeasonError('图片内容无法解码、与扩展名不符或超出像素及帧数限制', 400) from error
    return IMAGE_MIMES[kind]


def _video_type(path, ext, header):
    is_mov = header[4:8] == b'ftyp' and header[8:12] == b'qt  '
    valid_header = {'mp4': header[4:8] == b'ftyp' and not is_mov, 'mov': is_mov,
                    'webm': header.startswith(b'\x1a\x45\xdf\xa3') and b'webm' in header}
    if not valid_header[ext]:
        raise SeasonError('视频容器与扩展名不符', 400)
    demuxer = 'webm' if ext == 'webm' else 'mov'
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-f', demuxer,
                                 '-select_streams', 'v:0', '-show_entries',
                                 'stream=codec_name,width,height:format=duration', '-of', 'json', str(path)],
                                check=True, capture_output=True, timeout=10)
        info = json.loads(result.stdout)
        stream, = info['streams']
        duration = float(info['format']['duration'])
        codecs = {'vp8', 'vp9', 'av1'} if ext == 'webm' else {'h264', 'hevc', 'av1', 'mpeg4'}
        if (stream['codec_name'] not in codecs or not isfinite(duration) or not 0 < duration <= 120
                or not 0 < stream['width'] * stream['height'] <= 3840 * 2160):
            raise ValueError()
        subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-nostdin', '-protocol_whitelist', 'file',
                        '-f', demuxer, '-threads', '1', '-i', str(path), '-map', '0:v:0',
                        '-frames:v', '1', '-f', 'null', '-'], check=True, capture_output=True, timeout=15)
    except FileNotFoundError as error:
        raise SeasonError('上传轮播视频需要在服务端安装 FFmpeg', 503) from error
    except (subprocess.SubprocessError, KeyError, TypeError, ValueError) as error:
        raise SeasonError('视频无法解码、编码不兼容或超出尺寸及时长限制', 400) from error
    return VIDEO_MIMES[ext]


def _upload_root():
    return Path(current_app.config['UPLOAD_ROOT']).resolve()


def _stored_files(root):
    if root.is_dir():
        for path in root.rglob('*'):
            if not path.is_symlink() and path.is_file():
                yield path


def _check_capacity(root, size):
    a = ns()
    own = a.UploadAsset.query.filter_by(uploader_id=current_user.id)
    user_bytes = sum(row.size_bytes for row in own if (root / row.relative_path).is_file())
    if user_bytes + size > current_app.config['UPLOAD_USER_MAX_MB'] * MIB:
        raise SeasonError('操作账号的上传已达到容量限制', 413)
    current_bytes = sum(path.stat().st_size for path in _stored_files(root))
    if current_bytes + size > current_app.config['UPLOAD_TOTAL_MAX_MB'] * MIB:
        raise SeasonError('当前实例的上传已达到容量限制', 413)


@contextmanager
def upload_batch():
    """Keep file creation, database references and metadata in the same request transaction."""
    a = ns()
    staged = []
    try:
        yield staged
        a.db.session.commit()
    except Exception:
        a.db.session.rollback()
        for path in reversed(staged):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                current_app.logger.exception('清理失败的上传文件失败')
        raise


def save_upload(file, folder, staged, *, media_type='image', match_id=None):
    a = ns()
    name = PurePosixPath(file.filename.replace('\\', '/')).name
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)[:255]
    ext = PurePosixPath(file.filename.replace('\\', '/')).suffix.removeprefix('.').lower()
    allowed = VIDEO_MIMES if media_type == 'video' and folder == 'carousel' else IMAGE_FORMATS
    if ext not in allowed or (folder == 'screenshots' and ext == 'gif'):
        raise SeasonError('上传文件类型不受支持', 400)
    limit = VIDEO_LIMIT if media_type == 'video' else IMAGE_LIMITS[folder]
    data = file.stream.read(limit + 1)
    if not data:
        raise SeasonError('上传文件为空', 400)
    if len(data) > limit:
        raise SeasonError(f'单文件大小上限为 {limit // MIB} MiB', 413)
    root = _upload_root()
    directory = root / folder
    if directory.is_symlink():
        raise SeasonError('上传目录不可用，请联系管理员', 503)
    _check_capacity(root, len(data))
    # The match/user prefix preserves the existing evidence ownership and cleanup lookup.
    prefix = f'screenshot_{match_id}_{current_user.id}_' if match_id is not None else ''
    filename = prefix + uuid4().hex + '.' + ext
    path = directory / filename
    temporary = directory / ('.staging-' + uuid4().hex)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with temporary.open('xb') as stream:
            staged.append(temporary)
            stream.write(data)
        mime = _video_type(temporary, ext, data[:4096]) if media_type == 'video' else _image_type(data, ext)
        temporary.replace(path)
        staged.append(path)
    except OSError as error:
        current_app.logger.exception('素材写入失败')
        raise SeasonError('素材保存失败，请联系管理员检查存储', 503) from error
    a.db.session.add(a.UploadAsset(relative_path=f'{folder}/{filename}', original_name=name, mime_type=mime,
                                size_bytes=len(data), sha256=sha256(data).hexdigest(), uploader_id=current_user.id))
    return filename


def inventory():
    a = ns()
    from retention import _protected_uploads

    upload_root = _upload_root()
    image_root = Path(current_app.config['IMAGE_DIR']).resolve()
    conn = a.db.session.connection()
    references = {(root, root / relative) for root, relative in media_references(conn, current_app.config)}
    references.update((upload_root, path) for path in _protected_uploads(upload_root) if path.is_relative_to(upload_root))
    asset_records = {row.relative_path: row for row in a.UploadAsset.query.all()}
    files = []
    missing = []
    changed = []
    orphans = []
    duplicates = defaultdict(list)
    for layout, root in (('uploads', upload_root), ('image', image_root)):
        source_refs = {target for ref_root, target in references if ref_root == root}
        stored = set(_stored_files(root))
        # Top-level bundled media participates in the directory fallback.
        if layout == 'image':
            from app import _season_carousel_items, load_carousel_media
            if not _season_carousel_items(a.Tournament.get_active()):
                source_refs.update(root / item['file'] for item in load_carousel_media())
        for path in sorted(source_refs - stored):
            missing.append({'root': layout, 'path': str(path.relative_to(root)) if path.is_relative_to(root) else str(path)})
        for path in sorted(stored):
            relative = path.relative_to(root).as_posix()
            sha = digest(path)
            item = {'root': layout, 'path': relative, 'bytes': path.stat().st_size,
                    'sha256': sha, 'referenced': path in source_refs}
            metadata = asset_records.get(relative) if layout == 'uploads' else None
            if metadata:
                item.update(original_name=metadata.original_name, mime_type=metadata.mime_type,
                            uploaded_at=metadata.created_at.isoformat() + 'Z', uploader_id=metadata.uploader_id)
                if sha != metadata.sha256 or item['bytes'] != metadata.size_bytes:
                    changed.append({'root': layout, 'path': relative})
            if path not in source_refs:
                orphans.append({'root': layout, 'path': relative})
            duplicates[sha].append({'root': layout, 'path': relative})
            files.append(item)
    return {'version': 1, 'generated_at_utc': datetime.utcnow().isoformat() + 'Z',
            'files': files, 'missing': missing, 'orphans': orphans, 'changed': changed,
            'duplicates': [{'sha256': sha, 'files': entries} for sha, entries in sorted(duplicates.items()) if len(entries) > 1]}


def compare_inventory(current, baseline):
    def paths(report):
        if report['version'] != 1:
            raise ValueError('素材清单版本不兼容')
        return {(item['root'], item['path']): (item['sha256'], item['bytes']) for item in report['files']}
    before, after = paths(baseline), paths(current)
    return {name: [{'root': root, 'path': path} for root, path in sorted(entries)] for name, entries in (
        ('missing', before.keys() - after.keys()), ('added', after.keys() - before.keys()),
        ('changed', {key for key in before.keys() & after.keys() if before[key] != after[key]}))}
