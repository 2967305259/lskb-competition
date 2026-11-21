from pathlib import Path

import app as a
from conftest import login


def test_carousel_admin_controls_home_source_and_media(application, client):
    images = Path(application.config['IMAGE_DIR'])
    for filename in ('10.png', '2.png'):
        (images / filename).write_bytes(b'fallback-image')
    (images / 'banners').mkdir()
    (images / 'banners' / 'cover.png').write_bytes(b'database-image')
    (Path(application.config['UPLOAD_ROOT']) / 'preview.mp4').write_bytes(b'video-preview')
    admin_path = '/admin/carousel'
    image_url = '/carousel-media/banners/cover.png'
    video_url = '/static/uploads/preview.mp4'
    login(client, application.test_ids['admin'])

    home = client.get('/').get_data(as_text=True)
    assert home.index('/carousel-media/1.png') < home.index('/carousel-media/2.png') < home.index('/carousel-media/10.png')
    assert '首页来源：素材目录（image/）' in client.get(admin_path).get_data(as_text=True)

    image = {'file_path': 'image/banners/cover.png', 'media_type': 'image', 'sort_order': '12',
             'title': '比赛封面', 'link_url': '/rules'}
    video = {'file_path': '/static/uploads/preview.mp4', 'media_type': 'video', 'sort_order': '2',
             'title': '赛事视频', 'link_url': 'https://example.org/details'}
    assert client.post(admin_path + '/add', data=image).status_code == 302
    home = client.get('/').get_data(as_text=True)
    assert f'<img src="{image_url}"' in home and '/carousel-media/1.png' not in home
    assert f'<img src="{image_url}"' in client.get(admin_path).get_data(as_text=True)

    assert client.post(admin_path + '/add', data=video).status_code == 302
    home = client.get('/').get_data(as_text=True)
    assert home.index(f'<video src="{video_url}"') < home.index(f'<img src="{image_url}"')
    assert 'href="/rules"' in home and '>赛事视频</a>' in home
    assert 'href="https://example.org/details"' in home
    assert '首页来源：当届数据库条目' in client.get(admin_path).get_data(as_text=True)
    assert client.get(image_url).data == b'database-image'
    preview = client.get(video_url, headers={'Range': 'bytes=0-4'})
    assert preview.status_code == 206 and preview.data == b'video'

    with application.app_context():
        image_id = a.CarouselItem.query.filter_by(media_type='image').one().id
        video_id = a.CarouselItem.query.filter_by(media_type='video').one().id
    assert client.post(f'{admin_path}/{image_id}/edit', data=dict(image, sort_order='2', is_active='1',
                       title='已编辑封面', link_url='javascript:alert(1)')).status_code == 302
    home = client.get('/').get_data(as_text=True)
    assert home.index(f'<img src="{image_url}"') < home.index(f'<video src="{video_url}"')
    assert '已编辑封面' in home and 'javascript:alert(1)' not in home

    for item_id, fields in ((image_id, image), (video_id, video)):
        assert client.post(f'{admin_path}/{item_id}/edit', data=fields).status_code == 302
    assert '当前全部禁用，首页隐藏轮播' in client.get(admin_path).get_data(as_text=True)
    assert 'id="homeCarousel"' not in client.get('/').get_data(as_text=True)
    assert client.post(f'{admin_path}/{image_id}/delete').status_code == 302
    assert 'id="homeCarousel"' not in client.get('/').get_data(as_text=True)
    assert client.post(f'{admin_path}/{video_id}/delete').status_code == 302
    assert '首页来源：素材目录（image/）' in client.get(admin_path).get_data(as_text=True)
    assert '/carousel-media/1.png' in client.get('/').get_data(as_text=True)
    assert client.get(image_url).data == b'database-image'


def test_carousel_media_cannot_serve_from_sibling_directory(application, client):
    images = Path(application.config['IMAGE_DIR'])
    sibling = images.with_name(images.name + '-sibling')
    sibling.mkdir()
    (sibling / 'outside.png').write_bytes(b'outside')
    assert client.get('/carousel-media/../image-sibling/outside.png').status_code == 404
    (images / 'outside.png').symlink_to(sibling / 'outside.png')
    assert client.get('/carousel-media/outside.png').status_code == 404
