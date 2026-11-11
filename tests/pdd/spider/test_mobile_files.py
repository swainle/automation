from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from automation.pdd.spider import config, longshot
from automation.pdd.spider.mobile_files import cleanup_ranking, merge_details
from automation.pdd.spider.weekly_mobile import WeeklyProduct
from tests.pdd.spider.test_longshot import scene, frame


def test_cleanup_migrates_both_screenshots_and_preserves_public_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'OUTPUT_ROOT', tmp_path)
    root = tmp_path / '主榜/畅销榜/2026-11-06'
    wanted = {
        '榜单数据.xlsx', '榜单长图.jpg', '榜01/产品榜单截图.jpg', '榜01/产品截图.jpg',
        '榜01/SKU.csv', '榜01/差评.csv', '榜01/主图/001.jpg', '榜01/SKU/0001.jpg',
        '榜01/详情/0001.jpg', '榜01/详情/长图.jpg',
    }
    old = (wanted - {'榜01/产品榜单截图.jpg'}) | {
        '榜01/产品页截图.jpg', '榜01/图片清单.json', '榜01/SKU页面.xml', '榜单长图.meta.json',
        '榜单元素.xml', '榜单分屏/0001.png', '榜单数据.lock', '榜01/差评原始.json',
        '榜01/详情/临时.png', '其他目录/多余.txt',
    }
    for name in old:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    assert cleanup_ranking(root) == 9
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    assert actual == wanted
    assert (root / '榜01/产品榜单截图.jpg').read_bytes() == '榜01/产品截图.jpg'.encode()
    assert (root / '榜01/产品截图.jpg').read_bytes() == '榜01/产品页截图.jpg'.encode()
    assert cleanup_ranking(root) == 0
    assert (root / '榜01/产品截图.jpg').read_bytes() == '榜01/产品页截图.jpg'.encode()


def test_cleanup_refuses_root_and_external_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'OUTPUT_ROOT', tmp_path / 'output')
    external = tmp_path / '2026-11-06'
    external.mkdir()
    keep = external / 'keep.txt'
    keep.write_text('keep')
    for target in (external, tmp_path / 'output', tmp_path / 'output/主榜'):
        with pytest.raises(ValueError):
            cleanup_ranking(target)
    assert keep.read_text() == 'keep'


def test_cleanup_preserves_legacy_images_and_updates_sku_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'OUTPUT_ROOT', tmp_path)
    root = tmp_path / '主榜/畅销榜/2026-11-06'
    for name in ('主图/主图_01.jpg', 'SKU/SKU_001.jpg', '详情图片/详情_02.jpg', '详情图片/长图.jpg'):
        path = root / '榜01' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    table = root / '榜01/SKU.csv'
    table.write_text('price,图片路径\n19,SKU/SKU_001.jpg\n', encoding='utf-8-sig')
    cleanup_ranking(root)
    assert (root / '榜01/主图/001.jpg').read_bytes() == b'\xe4\xb8\xbb\xe5\x9b\xbe/\xe4\xb8\xbb\xe5\x9b\xbe_01.jpg'
    assert (root / '榜01/SKU/0001.jpg').is_file()
    assert (root / '榜01/详情/0002.jpg').is_file()
    assert (root / '榜01/详情/长图.jpg').is_file()
    assert 'SKU/0001.jpg' in table.read_text(encoding='utf-8-sig')
    assert not (root / '榜01/详情图片').exists()


def test_merge_details_sorts_number_and_scales_width(tmp_path):
    small, large = tmp_path / '0001.jpg', tmp_path / '0002.jpg'
    Image.new('RGB', (20, 10), 'red').save(small)
    Image.new('RGB', (40, 30), 'blue').save(large)
    output = tmp_path / '长图.jpg'
    merge_details([large, small], output)
    with Image.open(output) as image:
        assert image.size == (40, 50)
        assert image.getpixel((20, 5))[0] > 240
        assert image.getpixel((20, 40))[2] > 240
    output.write_bytes(b'previous')
    small.write_bytes(b'broken')
    with pytest.raises(OSError):
        merge_details([small, large], output)
    assert output.read_bytes() == b'previous'


def test_mobile_diagnostics_are_not_written(tmp_path):
    collector = WeeklyProduct(Mock(), Mock())
    collector.directory, collector.xml = tmp_path, '<App/>'
    collector.debug_json('SKU原始.json', [{'price': '1'}])
    collector.debug_xml('SKU页面.xml')
    assert list(tmp_path.iterdir()) == []


def test_merge_details_limits_jpeg_dimensions_without_changing_originals(tmp_path):
    paths = [tmp_path / f'{n:04d}.jpg' for n in (1, 2)]
    for path, color in zip(paths, ('red', 'blue')):
        Image.new('RGB', (20, 40000), color).save(path)
    originals = [path.read_bytes() for path in paths]
    output = tmp_path / '长图.jpg'
    merge_details(paths, output)
    with Image.open(output) as image:
        image.load()
        assert image.size == (16, 65500)
        assert image.getpixel((8, 5))[0] > 240
        assert image.getpixel((8, 65490))[2] > 240
    assert [path.read_bytes() for path in paths] == originals


def test_compact_longshot_writes_only_long_image_and_named_crops(tmp_path):
    world = scene()
    client = Mock()
    client.http.get.return_value.value = {'bundleId': longshot.ios.PDD_BUNDLE_ID}
    client.screenshot.side_effect = [frame(world, offset) for offset in (0, 90, 180, 270, 360, 450)]
    rows = [{'排行': index + 1, '标题': f'商品{index}'} for index in range(3)]
    layout = {'body_top': 50, 'tops': [50, 270, 490], 'end': 700, 'scale': 1}
    with patch.object(longshot, 'geometry', return_value=layout), \
            patch.object(longshot.ios, '_get_window_size', return_value=(240, 320)), \
            patch.object(longshot.time, 'sleep'):
        files = longshot.capture(client, rows, 'paired-xml', tmp_path, compact=True)
    expected = {'榜单长图.jpg', *(f'榜{n:02d}/产品榜单截图.jpg' for n in (1, 2, 3))}
    assert {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file()} == expected
    assert len(files) == 4


def test_weekly_details_produces_long_image(tmp_path):
    collector = WeeklyProduct(Mock(), Mock())
    collector.directory = tmp_path
    def capture(self):
        path = self.directory / '详情/0001.jpg'
        path.parent.mkdir()
        Image.new('RGB', (20, 30), 'red').save(path)
        self.images = [{'image_type': 'detail', 'local_path': '详情/0001.jpg'}]
    with patch('automation.pdd.spider.ios_product.IosProduct.details', capture):
        collector.details()
    with Image.open(tmp_path / '详情/长图.jpg') as image:
        assert image.size == (20, 30)


def test_completed_product_without_negative_reviews_keeps_csv_header(tmp_path):
    collector = WeeklyProduct(Mock(), Mock())
    collector.directory, collector.negative = tmp_path, []
    with patch('automation.pdd.spider.ios_product.IosProduct.comments'):
        collector.comments()
    assert (tmp_path / '差评.csv').read_text(encoding='utf-8-sig').strip() == '用户,评论内容,SKU,原因,评论日期'
