import pytest

from app import create_app


pytestmark = pytest.mark.maintenance


def test_prepare_reports_storage_paths_and_reuses_read_only_media(tmp_path):
    database = tmp_path / 'database' / 'competition.db'
    images = tmp_path / 'media'
    uploads = tmp_path / 'uploads'
    app = create_app('testing', {
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{database}',
        'STATE_DIR': str(tmp_path / 'state'),
        'BACKUP_DIR': str(tmp_path / 'backups'),
        'IMAGE_DIR': str(images),
        'UPLOAD_ROOT': str(uploads),
        'ADMIN_USERNAME': '',
        'SUPER_ADMIN_USERNAME': '',
    })
    runner = app.test_cli_runner()
    paths = [database.parent, images, uploads, *(uploads / name for name in ('avatars', 'teams', 'screenshots'))]
    first = runner.invoke(args=['prepare'])
    assert first.exit_code == 0, first.output
    for path in paths:
        assert path.is_dir()
        assert any('已创建' in line and line.endswith(f'：{path}') for line in first.output.splitlines())
    assert database.is_file()

    (images / 'cover.png').write_bytes(b'competition-cover')
    images.chmod(0o555)
    try:
        second = runner.invoke(args=['prepare'])
        assert second.exit_code == 0, second.output
        for path in paths:
            assert any(line.endswith(f'已存在：{path}') for line in second.output.splitlines())
        client = app.test_client()
        assert '/carousel-media/cover.png' in client.get('/').get_data(as_text=True)
        assert client.get('/carousel-media/cover.png').data == b'competition-cover'
    finally:
        images.chmod(0o755)


def test_prepare_identifies_unusable_upload_path(application, tmp_path):
    blocker = tmp_path / 'occupied'
    blocker.write_bytes(b'existing file')
    application.config['UPLOAD_ROOT'] = str(blocker)
    result = application.test_cli_runner().invoke(args=['prepare'])
    assert result.exit_code != 0
    assert f'无法准备上传目录 {blocker}' in result.output
    assert blocker.read_bytes() == b'existing file'
