import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw
import pytest

from automation.pdd.spider.native_product import NativeProduct, PACKAGE, review_filters
from automation.pdd.spider.database import Store


def filter_fixture():
    image = Image.new('RGB', (300, 180), 'white')
    draw = ImageDraw.Draw(image)
    root = ET.Element('root')
    for index, (label, color) in enumerate([('全部(100)', (230, 40, 30)),
            ('质量好(20)', (255, 239, 240)), ('质量差(3)', (240, 240, 240)),
            ('图/视频(8)', (255, 239, 240))]):
        y = index * 40
        draw.rectangle((0, y, 139, y + 35), fill=color)
        ET.SubElement(root, 'node', {'package': PACKAGE, 'content-desc': label,
                      'clickable': 'true', 'bounds': f'[0,{y}][140,{y+35}]'})
    return root, image


def test_app_background_classifies_tags_and_excludes_general_filters():
    root, image = filter_fixture()
    filters = review_filters(root, image)
    assert [(tag['name'], tag['kind'], tag['count']) for tag in filters] == [
        ('质量好', 'positive', 20), ('质量差', 'negative', 3)]
    assert [tag['basis'] for tag in filters] == ['app_pink_tag', 'app_gray_tag']


def test_explicit_good_bad_filters_work_without_color_inference():
    root = ET.Element('root')
    for index, label in enumerate(('好评(8)', '差评(2)')):
        ET.SubElement(root, 'node', {'package': PACKAGE, 'text': label, 'clickable': 'true',
                      'bounds': f'[{index*100},0][{index*100+90},40]'})
    filters = review_filters(root, Image.new('RGB', (200, 50), 'white'))
    assert [tag['kind'] for tag in filters] == ['positive', 'negative']
    assert all(tag['basis'] == 'explicit_app_filter' for tag in filters)


def test_missing_negative_filter_is_not_reported_as_no_negative_reviews(tmp_path):
    collector = NativeProduct(Mock())
    assert collector._classified_reviews(tmp_path, [], 'negative') == []
    stage = json.loads((tmp_path / 'status.json').read_text(encoding='utf-8'))['stages']['negative_comments']
    assert stage['state'] == 'skipped'
    assert '未提供' in stage['reason'] and '不能据此认定' in stage['reason']


def test_comments_reads_only_negative_filters_without_normal_or_positive_collection(tmp_path):
    root = ET.Element('root')
    ET.SubElement(root, 'node', {'package': PACKAGE, 'text': '全部(200)'})
    collector = NativeProduct(Mock())
    row = {'ui_fingerprint': 'one', 'comment': '坏了'}
    with patch.object(collector, 'snapshot', return_value=('', root)), \
            patch.object(collector, '_review_top'), patch.object(collector, 'check_title'), \
            patch.object(collector, '_review_batch') as ordinary, \
            patch.object(collector, '_classified_reviews', return_value=[row]) as classified, \
            patch('automation.pdd.spider.native_product.review_filters', return_value=[]), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        rows, _ = collector.comments(tmp_path, '商品')
    assert rows == [row]
    ordinary.assert_not_called()
    classified.assert_called_once_with(tmp_path, [], 'negative')
    assert not (tmp_path / '评论.csv').exists()
    assert not (tmp_path / '好评.csv').exists()


def test_unselected_filter_does_not_classify_old_list():
    root, image = filter_fixture()
    collector = NativeProduct(Mock())
    collector.d.screenshot.return_value = image
    with patch.object(collector, '_review_top'), patch.object(collector, 'retry_click'), \
            patch.object(collector, 'snapshot', return_value=('', root)), \
            pytest.raises(RuntimeError, match='未确认评价筛选已选中'):
        collector._select_review_filter('质量好(20)')


def test_classified_rows_are_deduplicated_and_merge_overlapping_tags(tmp_path):
    collector = NativeProduct(Mock(), comment_limit=3)
    filters = [{'kind': 'positive', 'basis': 'app_pink_tag', 'label': label+'(2)', 'name': label, 'count': 2}
               for label in ('质量好', '好用')]
    batches = [([{'ui_fingerprint': 'same', 'comment': '很好', 'positive_tags': ['质量好']}], 'end'),
               ([{'ui_fingerprint': 'same', 'comment': '很好', 'positive_tags': ['好用']}], 'end')]
    with patch.object(collector, '_select_review_filter'), patch.object(collector, '_review_batch', side_effect=batches):
        rows = collector._classified_reviews(tmp_path, filters, 'positive')
    assert len(rows) == 1
    assert rows[0]['positive_tags'] == ['质量好', '好用']
    assert len(json.loads((tmp_path / '好评原始.json').read_text(encoding='utf-8'))) == 1


def test_database_preserves_positive_and_negative_tags_on_same_ui_review(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed('2026-11-03', [{'name': '测试', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.save_rows(1, tmp_path, [], [{'排行': 1, '标题': '商品'}], 'android')
    row = {'ui_fingerprint': 'same-ui-review', 'comment': '商品还可以', 'name': '用户'}
    store.product_state(1, 'completed', details={'comments': [row],
        'positive_comments': [{**row, 'positive_tags': ['质量好']}],
        'negative_comments': [{**row, 'negative_tags': ['物流慢']}]})
    records = store.db.execute('SELECT * FROM product_comments').fetchall()
    assert len(records) == 1 and records[0]['is_negative'] == 1
    raw = json.loads(records[0]['raw_json'])
    assert raw['positive_tags'] == ['质量好'] and raw['negative_tags'] == ['物流慢']
    store.close()


def test_negative_filter_saves_actual_filtered_rows_with_source_tags(tmp_path):
    collector = NativeProduct(Mock(), comment_limit=2)
    tag = {'kind': 'negative', 'basis': 'app_gray_tag', 'label': '质量差(1)', 'name': '质量差', 'count': 1}
    row = {'ui_fingerprint': 'negative-row', 'comment': '坏了', 'negative_tags': ['质量差'],
           'classification_basis': 'app_gray_tag'}
    with patch.object(collector, '_select_review_filter') as select, \
            patch.object(collector, '_review_batch', return_value=([row], 'end')):
        assert collector._classified_reviews(tmp_path, [tag], 'negative') == [row]
    select.assert_called_once_with('质量差(1)')
    stage = json.loads((tmp_path / 'status.json').read_text(encoding='utf-8'))['stages']['negative_comments']
    assert stage['state'] == 'completed' and stage['count'] == 1
    assert json.loads((tmp_path / '差评原始.json').read_text(encoding='utf-8'))[0]['negative_tags'] == ['质量差']


def test_review_menu_date_is_exported_and_menu_is_cancelled():
    from automation.pdd.spider.comments import _comment_rows
    root = ET.Element('root')
    for text in ['发表时间: 2025.11.10', '举报该评价', '取消']:
        ET.SubElement(root, 'node', {'package': PACKAGE, 'text': text})
    collector = NativeProduct(Mock())
    row = {'name': '用户甲', 'comment': '评价内容', 'specs': '鸡肉,100支',
           '_menu_bounds': (900, 100, 950, 140)}
    with patch.object(collector, 'snapshot', return_value=('', root)), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        result = collector.review_metadata(row)
    assert result['评论日期'] == '2025-11-10'
    assert '_menu_bounds' not in result
    assert _comment_rows([result])[0][1:5] == ['鸡肉,100支', '用户甲', '', '2025-11-10']
    collector.d.assert_called_with(text='取消')
    collector.d.return_value.click.assert_called_once()


def test_review_specs_are_joined_from_product_card_below_comment():
    from automation.pdd.spider.native_product import review_rows
    root = ET.Element('root')
    card = ET.SubElement(root, 'node')
    header = ET.SubElement(card, 'node')
    ET.SubElement(header, 'node', {'package': PACKAGE, 'content-desc': '头像'})
    ET.SubElement(header, 'node', {'text': '用户甲'})
    ET.SubElement(card, 'node', {'text': '实际评论内容', 'resource-id': PACKAGE + ':id/tv_content'})
    product = ET.SubElement(card, 'node', {'clickable': 'true'})
    ET.SubElement(product, 'node', {'class': 'android.widget.ImageView'})
    for text in ['混合口味, 100', '支【新货速发】', '¥ 26.22 #']:
        ET.SubElement(product, 'node', {'text': text})
    assert review_rows(root)[0]['specs'] == '混合口味, 100支【新货速发】'


def test_review_menu_waits_for_delayed_popup_before_retrying():
    empty = ET.Element('root')
    menu = ET.Element('root')
    for value in ['发表时间: 2026.11.04', '取消']:
        ET.SubElement(menu, 'node', {'package': PACKAGE, 'text': value})
    collector = NativeProduct(Mock())
    row = {'name':'用户', 'comment':'评论内容', '_menu_bounds':(10,20,30,40)}
    with patch.object(collector,'snapshot',side_effect=[('',empty),('',menu)]), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        result = collector.review_metadata(row)
    assert result['评论日期'] == '2026-11-04'
    collector.d.click.assert_called_once()


def test_missing_menu_keeps_comment_without_inventing_date():
    root = ET.Element('root')
    collector = NativeProduct(Mock())
    row = {'name':'用户', 'comment':'评论内容', 'specs':'黑色', '_menu_bounds':(10,20,30,40)}
    visible = {**row, 'ui_fingerprint':'same', '_menu_bounds':(10,40,30,60)}
    with patch.object(collector,'snapshot',return_value=('',root)), \
            patch('automation.pdd.spider.native_product.review_rows',return_value=[visible]), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        result = collector.review_metadata(row)
    assert result['comment'] == '评论内容'
    assert result['date_status'] == 'unavailable'
    assert '评论日期' not in result
    assert collector.d.click.call_count == 3


def test_missing_menu_on_unverified_page_stops_clicking():
    root = ET.Element('root')
    collector = NativeProduct(Mock())
    with patch.object(collector,'snapshot',return_value=('',root)), \
            patch('automation.pdd.spider.native_product.review_rows',return_value=[]), \
            patch('automation.pdd.spider.native_product.time.sleep'), \
            pytest.raises(RuntimeError,match='未确认仍在原评论列表'):
        collector.review_metadata({'name':'用户','comment':'评论','_menu_bounds':(10,20,30,40)})
    collector.d.click.assert_called_once()
