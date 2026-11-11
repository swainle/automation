import base64
import csv
import json
from io import BytesIO
from unittest.mock import Mock, patch

from PIL import Image
import pytest

from automation.pdd.spider.product_extension import ExtensionProduct
from automation.pdd.spider.status import update_stage
from automation.pdd.spider.artifacts import write_json


@pytest.mark.parametrize('phone_screenshot', [True, False])
@pytest.mark.parametrize('legacy_main', [True, False])
def test_plugin_product_keeps_sku_comment_formats_images_and_request_information(tmp_path, phone_screenshot, legacy_main):
    task = {'directory': str(tmp_path), 'rank': 1, 'row_json': json.dumps({'排行': 1, '标题': '商品', '产品ID': '123',
            '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=123'})}
    folder = tmp_path / '榜01'
    update_stage(folder, 'rank', 'completed')
    if phone_screenshot:
        Image.new('RGB', (100, 200), 'green').save(folder / '产品页截图.jpg')
    phone_image = (folder / '产品页截图.jpg').read_bytes() if phone_screenshot else None
    if legacy_main:
        (folder / '主图').mkdir()
        for index in range(1, 4):
            Image.new('RGB', (10, 10), 'blue').save(folder / f'主图/主图_{index:02d}.jpg')
        write_json(folder / '图片清单.json', [
            {'image_type': 'main', 'position': index, 'source_url': f'https://images.example/old-{index}.jpg',
             'local_path': f'主图/主图_{index:02d}.jpg', 'state': 'completed'} for index in range(1, 4)])
        update_stage(folder, 'main_images', 'completed', count=3)
    screenshot = BytesIO()
    Image.new('RGB', (390, 844), 'red').save(screenshot, format='JPEG')
    phase = 0
    runtime = Mock()
    def call(action, params):
        nonlocal phase
        if action == 'product_open':
            return {'tab_id': 9}
        if action == 'product_close':
            return {'closed': True}
        if action == 'product_screenshot':
            return {'format': 'jpeg', 'data': base64.b64encode(screenshot.getvalue()).decode()}
        if action == 'product_reviews_begin':
            phase += 1
            return {'phase': phase}
        if action == 'product_network':
            if params['kind'] == 'info':
                return {'responses': [{'url': 'https://mobile.yangkeduo.com/api/goods', 'status': 200,
                    'type': 'Fetch', 'body': {'goods_id': '123'}, 'data': {'goods_id': 123, 'goods_name': '商品'}}]}
            return {'received': True, 'exhausted': True, 'comments': [{'review_id': str(phase), 'name': '用户', 'comment': '评论', 'time': 0}]}
        operation = params['operation']
        return {'info': {'sold_out': False, 'url': 'https://mobile.yangkeduo.com/goods.html'},
            'images': {'urls': ['https://images.example/2024-04-27/main.jpg'], 'verification': 'counter-drag-v1', 'expected': 1, 'visited_pages': [1]},
            'sku_dimensions': {'dimensions': [{'index': 0, 'name': '口味', 'options': [{'index': 0, 'text': '鸡肉\n快要抢光'}]}]},
            'sku_select': {'image': 'https://images.example/sku.jpg', 'price': '￥2.50'},
            'sku_close': {'closed': True}, 'details': {'urls': ['https://images.example/detail.jpg']},
            'reviews_enter': {'opened': True}, 'reviews_ready': {'ready': True}, 'reviews_tags': {'names': ['难吃']},
            'reviews_tag': {'clicked': True}, 'reviews_scroll': {'scrolled': True}}[operation]
    runtime.ranking_request.side_effect = call
    def download(url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (20, 30), 'blue').save(path, format='JPEG')
        return True
    with patch('automation.pdd.spider.product_extension.download', side_effect=download):
        state, data = ExtensionProduct(runtime, wait=lambda seconds: None).collect(task)
    assert state == 'completed'
    assert data['info']['goods_id'] == '123'
    assert data['status']['listing_date'] == '2024-04-27'
    with (folder / 'SKU.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == ['序号', '商品名称', '卷后价', 'SPU1', '图片路径']
    assert rows[1][:4] == ['1', '鸡肉', '2.50', '鸡肉']
    assert data['negative_comments'][0]['negative_tags'] == ['难吃']
    if phone_screenshot:
        assert (folder / '产品页截图.jpg').read_bytes() == phone_image
    else:
        assert not (folder / '产品页截图.jpg').exists()
    assert all(call.args[0] != 'product_screenshot' for call in runtime.ranking_request.call_args_list)
    assert {record['image_type'] for record in data['images']} == {'main', 'sku', 'detail', 'detail_merged'}
    assert sum(record['image_type'] == 'main' for record in data['images']) == 1
    assert any(call.args[0] == 'product_dom' and call.args[1].get('operation') == 'images'
               for call in runtime.ranking_request.call_args_list)
    runtime.open_browser.assert_not_called()
    assert runtime.ranking_request.call_args.args == ('product_close', {'tab_id': 9})
