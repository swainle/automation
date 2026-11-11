import xml.etree.ElementTree as ET
from unittest.mock import Mock, PropertyMock, patch

from PIL import Image, ImageDraw
import pytest

from automation.pdd.spider.native_product import PACKAGE, NativeProduct, image_crop_bounds, sku_dimensions, review_rows, product_info, ui_price


def node(**attrs):
    return ET.Element('node', {'package': PACKAGE, **attrs})


def test_crop_removes_viewer_toolbar_black_bars_and_bottom_controls():
    image = Image.new('RGB', (300, 650), 'black')
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 10, 90, 20), fill='white')
    draw.rectangle((0, 160, 299, 459), fill='white')
    draw.rectangle((10, 550, 80, 560), fill='white')
    assert image_crop_bounds(image, (0, 0, 300, 650)) == (0, 160, 300, 460)


def test_sku_parser_does_not_mistake_option_rows_for_extra_dimensions():
    root = node()
    for name, options in [('颜色', ['黑色', '红色']), ('组合', ['6支', '12支'])]:
        group = ET.SubElement(root, 'node')
        heading = ET.SubElement(group, 'node')
        heading.append(node(text=name, clickable='false'))
        values = ET.SubElement(group, 'node')
        for text in options:
            values.append(node(text=text, clickable='true', **{'class': 'android.widget.TextView'}))
    false_dimension = ET.SubElement(root, 'node')
    false_dimension.append(node(text='6支', clickable='true'))
    false_option = ET.SubElement(false_dimension, 'node')
    false_option.append(node(text='12支', clickable='true', **{'class': 'android.widget.TextView'}))
    duplicate = ET.SubElement(root, 'node')
    header = ET.SubElement(duplicate, 'node')
    header.append(node(text='颜色', clickable='false'))
    values = ET.SubElement(duplicate, 'node')
    for value in ['黑色', '红色', '6支', '12支']:
        values.append(node(text=value, clickable='true', **{'class': 'android.widget.TextView'}))
    assert sku_dimensions(root) == [{'name': '颜色', 'options': ['黑色', '红色']},
                                    {'name': '组合', 'options': ['6支', '12支']}]


def test_review_parser_keeps_nickname_specs_and_content_without_inventing_review_id():
    root = node()
    card = ET.SubElement(root, 'node')
    header = ET.SubElement(card, 'node')
    header.append(node(**{'content-desc': '头像'}))
    header.append(node(text='用户甲'))
    body = ET.SubElement(card, 'node')
    body.append(node(text='颜色：黑色'))
    body.append(node(text='商品书写顺滑，很好用', **{'resource-id': PACKAGE + ':id/tv_content'}))
    rows = review_rows(root)
    assert len(rows) == 1
    assert rows[0]['name'] == '用户甲'
    assert rows[0]['specs'] == '颜色：黑色'
    assert rows[0]['comment'] == '商品书写顺滑，很好用'
    assert 'review_id' not in rows[0]
    assert review_rows(root)[0]['ui_fingerprint'] == rows[0]['ui_fingerprint']


def test_product_summary_requires_real_product_controls():
    root = node()
    root.append(node(**{'content-desc': '当前测试商品的完整商品标题', 'class': 'android.widget.ImageView'}))
    root.append(node(text='免拼购买'))
    with pytest.raises(RuntimeError, match='不是可识别'):
        product_info(root)
    root.append(node(**{'content-desc': '分享'}))
    root.append(node(text='¥3.01'))
    assert product_info(root)['goods_name'] == '当前测试商品的完整商品标题'
    assert product_info(root)['display_price'] == '¥3.01'
    assert ui_price(['¥', '3.01']) == '¥3.01'


def webview_goods_fixture():
    root = node()
    root.append(node(**{'class': 'android.webkit.WebView'}))
    root.append(node(text='商品大图', **{'class': 'android.widget.Image', 'clickable': 'true', 'bounds': '[0,0][1080,1080]'}))
    root.append(node(text='共9张商品图 当前为第1张'))
    root.append(node(text='万人团价¥ 27.89', **{'class': 'android.widget.Button'}))
    root.append(node(text='博贝冻干鸡胸肉干批发猫咪狗狗零食宠物猫粮幼猫发腮增肥补钙',
                     **{'class': 'android.view.View', 'bounds': '[32,1397][1025,1517]'}))
    root.append(node(text='商品评价(94,315)', bounds='[30,1900][600,1950]'))
    root.append(node(text='这是一条比标题还长的评论，不能作为商品标题' * 3,
                     **{'class': 'android.view.View', 'bounds': '[30,2000][1050,2150]'}))
    root.append(node(text='立即参与万人团拼单 最后2天',
                     **{'class': 'android.widget.Button', 'clickable': 'true', 'bounds': '[387,2185][1080,2340]'}))
    return root


def test_webview_product_title_and_embedded_price_without_share_description():
    from automation.pdd.spider.native_product import image_counter
    root = webview_goods_fixture()
    info = product_info(root)
    assert info['goods_name'] == '博贝冻干鸡胸肉干批发猫咪狗狗零食宠物猫粮幼猫发腮增肥补钙'
    assert info['display_price'] == '¥27.89'
    assert info['page_structure'] == 'webview'
    assert image_counter(root) == [1, 9]


@pytest.mark.parametrize('missing', ['商品大图', '立即参与万人团拼单 最后2天'])
def test_webview_text_alone_does_not_bypass_product_validation(missing):
    root = webview_goods_fixture()
    for child in list(root):
        if child.get('text') == missing:
            root.remove(child)
    with pytest.raises(RuntimeError, match='不是可识别'):
        product_info(root)


def test_current_cli_does_not_start_queue_or_open_other_product_links():
    from automation.pdd.spider import product
    runtime = Mock()
    with patch('automation.pdd.spider.commands.execute', side_effect=lambda description, callback, **kwargs: callback(Mock(), runtime)), \
            patch('automation.pdd.spider.native_product.NativeProduct') as collector, \
            patch.object(product.Product, 'run') as queue:
        assert product.main(['--android', '--current', '--comment-limit', '3']) == 0
    collector.assert_called_once_with(runtime.ensure_android.return_value, comment_limit=3)
    collector.return_value.collect_current.assert_called_once_with()
    queue.assert_not_called()


def test_current_cli_rejects_missing_android_flag():
    from automation.pdd.spider import product
    with pytest.raises(SystemExit):
        product.main(['--current'])


def test_sku_keeps_selected_option_instead_of_toggling_it_off(tmp_path):
    device = Mock()
    device.return_value.info = {'selected': True, 'bounds': {'top': 100, 'bottom': 180}}
    device.return_value.exists = False
    collector = NativeProduct(device)
    root = node()
    root.append(node(**{'content-desc': '增加数量'}))
    root.append(node(text='¥3.01'))
    with patch.object(collector, 'snapshot', return_value=('<root/>', root)), \
            patch.object(collector, 'sku_to_top'), \
            patch('automation.pdd.spider.native_product.sku_dimensions', return_value=[{'name': '颜色', 'options': ['黑色']}]), \
            patch.object(collector, 'retry_click') as tap, \
            patch.object(collector, 'enlarged_image', return_value={'local_path': 'SKU/SKU_001.jpg'}), \
            patch.object(collector, 'check_title'), patch('automation.pdd.spider.native_product.time.sleep'):
        rows, total = collector.sku(tmp_path, '商品标题', [])
    assert total == 1 and rows[0][2] == '¥3.01'
    assert tap.call_count == 2  # Open SKU, open image; selected option is not clicked.


def test_sku_scroll_stays_above_purchase_confirmation():
    device = Mock()
    device.window_size.return_value = (300, 650)
    scroll, confirm, selector = Mock(), Mock(), Mock()
    scroll.info = {'bounds': {'top': 100, 'bottom': 650}}
    confirm.exists = True
    confirm.info = {'bounds': {'top': 550, 'bottom': 650}}
    device.side_effect = lambda **kwargs: scroll if 'scrollable' in kwargs else confirm
    outside = {'selected': False, 'bounds': {'top': 560, 'bottom': 600}}
    inside = {'selected': False, 'bounds': {'top': 300, 'bottom': 350}}
    type(selector).info = PropertyMock(side_effect=[outside, outside, inside])
    collector = NativeProduct(device)
    with patch.object(collector, 'sku_to_top') as top, patch('automation.pdd.spider.native_product.time.sleep'):
        assert collector.visible_sku_option(selector) == inside
    top.assert_called_once()
    device.swipe.assert_called_once()
    assert all(100 < value < 550 for value in (device.swipe.call_args.args[1], device.swipe.call_args.args[3]))


def test_native_queue_rejects_foreign_links_without_opening_app():
    import json
    device = Mock()
    with pytest.raises(ValueError, match='拼多多地址'):
        NativeProduct(device).collect({'row_json': json.dumps({'产品链接': 'https://example.com/goods'})})
    device.shell.assert_not_called()


@pytest.mark.parametrize('flag,step', [('--image-sku', 'sku'), ('--image-main', 'image_main'), ('--image-detail', 'image_detail'), ('--comment', 'comment')])
def test_current_cli_dispatches_selected_stage(flag, step):
    from automation.pdd.spider import product
    runtime = Mock()
    with patch('automation.pdd.spider.commands.execute', side_effect=lambda description, callback, **kwargs: callback(Mock(), runtime)), \
            patch('automation.pdd.spider.native_product.NativeProduct') as collector:
        assert product.main(['--android', flag, '--current']) == 0
    collector.return_value.collect_current.assert_called_once_with(steps={step})


@pytest.mark.parametrize('flag', ['--image-sku', '--image-main', '--image-detail', '--comment'])
def test_partial_flags_require_android_current(flag):
    from automation.pdd.spider import product
    with pytest.raises(SystemExit):
        product.main(['--android', flag])


@pytest.mark.parametrize('step', ['sku', 'image_main', 'comment'])
def test_selected_collection_skips_other_operations_and_does_not_mark_whole_product_done(tmp_path, step):
    import json
    root = node()
    root.append(node(**{'content-desc': '当前测试商品的完整商品标题', 'class': 'android.widget.ImageView'}))
    root.append(node(text='免拼购买'))
    root.append(node(**{'content-desc': '分享'}))
    previous = [{'image_type': 'main', 'local_path': '主图/主图_01.jpg'}]
    (tmp_path / '图片清单.json').write_text(json.dumps(previous), encoding='utf-8')
    collector = NativeProduct(Mock())
    with patch.object(collector, 'snapshot', return_value=('<root/>', root)), \
            patch.object(collector, 'top'), \
            patch.object(collector, 'images', return_value=(previous, 1)) as images, \
            patch.object(collector, 'sku', return_value=([['one']], 1)) as sku, \
            patch.object(collector, 'comments', return_value=([{'comment': '好'}], 'limit')) as comments:
        collector.collect_current(tmp_path, steps={step})
    assert images.call_count == int(step == 'image_main')
    assert sku.call_count == int(step == 'sku')
    assert comments.call_count == int(step == 'comment')
    if step == 'sku':
        assert sku.call_args.args[2] == previous
    stages = json.loads((tmp_path / 'status.json').read_text(encoding='utf-8'))['stages']
    assert 'product' not in stages and stages['native_selection']['state'] == 'completed'
    collector.d.screenshot.assert_not_called()


def test_comment_only_closes_known_sku_panel_before_reading_same_product(tmp_path):
    panel = node()
    panel.append(node(text='确认款式'))
    panel.append(node(**{'content-desc': '增加数量'}))
    root = node()
    root.append(node(**{'content-desc': '当前测试商品的完整商品标题', 'class': 'android.widget.ImageView'}))
    root.append(node(text='免拼购买'))
    root.append(node(**{'content-desc': '分享'}))
    collector = NativeProduct(Mock())
    with patch.object(collector, 'snapshot', side_effect=[('', panel), ('', root), ('', root)]), \
            patch.object(collector, 'retry_click') as close, patch.object(collector, 'top'), \
            patch.object(collector, 'comments', return_value=([], 'end')) as comments, \
            patch.object(collector, 'sku') as sku, patch.object(collector, 'images') as images:
        collector.collect_current(tmp_path, steps={'comment'})
    close.assert_called_once()
    comments.assert_called_once()
    sku.assert_not_called()
    images.assert_not_called()


def test_detail_entry_uses_title_on_parent_and_does_not_require_clickable_image():
    root = node()
    root.append(node(text='商品详情', bounds='[0,250][300,300]'))
    container = ET.SubElement(root, 'node', {'content-desc': '商品完整标题', 'bounds': '[0,300][300,550]'})
    container.append(node(**{'class': 'android.widget.ImageView', 'bounds': '[0,300][300,550]'}))
    viewer = node()
    viewer.append(node(text='1/7'))
    viewer.append(node(**{'class': 'android.widget.ImageView', 'bounds': '[0,200][300,600]'}))
    device = Mock()
    device.window_size.return_value = (300, 800)
    collector = NativeProduct(device)
    with patch.object(collector, 'snapshot', return_value=('', viewer)), \
            patch.object(collector, 'check_title', return_value=root), patch('automation.pdd.spider.native_product.time.sleep'):
        assert collector.open_detail('商品完整标题') is viewer
    device.click.assert_called_once_with(150, 425)
    device.swipe.assert_not_called()


def test_detail_only_preserves_other_stages(tmp_path):
    import json
    root = node()
    root.append(node(**{'content-desc': '当前测试商品的完整商品标题', 'class': 'android.widget.ImageView'}))
    root.append(node(text='免拼购买'))
    root.append(node(**{'content-desc': '分享'}))
    collector = NativeProduct(Mock())
    with patch.object(collector, 'snapshot', return_value=('', root)), patch.object(collector, 'top'), \
            patch.object(collector, 'images', return_value=([{'image_type': 'detail'}], 1)) as images, \
            patch.object(collector, 'sku') as sku, patch.object(collector, 'comments') as comments:
        collector.collect_current(tmp_path, steps={'image_detail'})
    images.assert_called_once_with(tmp_path, '当前测试商品的完整商品标题', detail=True)
    sku.assert_not_called()
    comments.assert_not_called()
    stages = json.loads((tmp_path / 'status.json').read_text(encoding='utf-8'))['stages']
    assert stages['detail_images']['state'] == 'completed'
    assert 'product' not in stages


def test_batch_product_uses_chrome_jump_before_collecting_current_product(tmp_path):
    import json
    title = '当前测试商品的完整商品标题'
    url = 'https://mobile.yangkeduo.com/goods1.html?ps=test'
    collector = NativeProduct(Mock())
    task = {'directory': str(tmp_path), 'rank': 1, 'row_json': json.dumps({'标题': title, '产品链接': url})}
    directory = tmp_path / '榜01'
    order = []
    with patch('automation.pdd.spider.android_jump.jump', side_effect=lambda *a, **kw: order.append('chrome')) as jump, \
            patch.object(collector, 'snapshot', return_value=('', node())), \
            patch('automation.pdd.spider.native_product.product_info', side_effect=[RuntimeError('加载中'), {'goods_name': title}]), \
            patch.object(collector, 'mark_sold_out', return_value=False), \
            patch.object(collector, 'collect_current', side_effect=lambda *a: order.append('collect')) as collect, \
            patch('automation.pdd.spider.product.artifacts', return_value={}), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        assert collector.collect(task) == ('completed', {})
    jump.assert_called_once_with(url, directory / '网页跳转', close_page=True)
    collect.assert_called_once_with(directory)
    collector.d.shell.assert_not_called()
    assert order == ['chrome', 'collect']


def test_changed_product_title_is_accepted_for_in_place_refresh(tmp_path):
    import json
    collector = NativeProduct(Mock())
    task = {'directory': str(tmp_path), 'rank': 1, 'row_json': json.dumps({
        '标题': '榜单旧标题', '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=123'})}
    with patch('automation.pdd.spider.android_jump.jump'), patch.object(collector, 'snapshot', return_value=('', node())), \
            patch('automation.pdd.spider.native_product.product_info', return_value={'goods_name': '商品页新标题'}), \
            patch.object(collector, 'collect_current') as collect:
        assert collector.collect(task)[0] == 'completed'
    collect.assert_called_once_with(tmp_path / '榜01')


def test_androidx_pager_title_wins_over_repeated_video_control_description():
    root = node()
    for _ in range(2):
        root.append(node(**{'class': 'android.widget.FrameLayout', 'content-desc': 'tronplayer_view'}))
    root.append(node(**{'class': 'androidx.viewpager.widget.ViewPager',
                        'content-desc': '当前测试商品的完整商品标题'}))
    root.append(node(text='免拼购买'))
    root.append(node(**{'content-desc': '分享'}))
    assert product_info(root)['goods_name'] == '当前测试商品的完整商品标题'


def test_scrolled_product_title_ignores_video_overlay_without_pager():
    root = node()
    root.append(node(**{'class': 'android.widget.FrameLayout', 'content-desc': 'tronplayer_view'}))
    root.append(node(**{'class': 'android.view.ViewGroup',
                        'content-desc': '当前测试商品的完整商品标题'}))
    root.append(node(text='免拼购买'))
    root.append(node(**{'content-desc': '分享'}))
    assert product_info(root)['goods_name'] == '当前测试商品的完整商品标题'


def test_collection_page_check_locates_purchase_button_without_matching_title():
    root = node()
    button = node(clickable='true', enabled='true', **{'class': 'android.view.ViewGroup',
                   'content-desc': '11.11狂降价¥8.8'})
    button.append(node(text='惊喜特价 免拼购买'))
    root.append(button)
    collector = NativeProduct(Mock())
    with patch.object(collector, 'snapshot', return_value=('', root)), \
            patch('automation.pdd.spider.native_product.product_info') as info:
        assert collector.check_title('原商品标题，无需再次比较') is root
    info.assert_not_called()


def test_collection_page_check_rejects_purchase_text_without_button():
    root = node()
    root.append(node(text='免拼购买', clickable='false'))
    collector = NativeProduct(Mock())
    with patch.object(collector, 'snapshot', return_value=('', root)), \
            pytest.raises(RuntimeError, match='购买按钮'):
        collector.check_title('原商品标题')


def test_detail_entry_retries_candidate_that_does_not_enlarge():
    root = node()
    root.append(node(text='商品详情', bounds='[0,0][300,50]'))
    for rectangle in ['[0,50][300,250]', '[0,300][300,550]']:
        container = node(**{'content-desc': '商品完整标题', 'bounds': rectangle})
        container.append(node(**{'class': 'android.widget.ImageView', 'bounds': rectangle}))
        root.append(container)
    viewer = node()
    viewer.append(node(text='1/7'))
    viewer.append(node(**{'class': 'android.widget.ImageView', 'bounds': '[0,200][300,600]'}))
    device = Mock()
    device.window_size.return_value = (300, 800)
    collector = NativeProduct(device)
    with patch.object(collector, 'snapshot', side_effect=[('', root), ('', viewer)]), \
            patch.object(collector, 'check_title', return_value=root), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        assert collector.open_detail('商品完整标题') is viewer
    assert device.click.call_count == 2
    device.press.assert_not_called()


def test_static_sku_panel_does_not_require_scrollview():
    collector = NativeProduct(Mock())
    collector.d.window_size.return_value = (300, 800)
    region = {'top': 200, 'bottom': 700}
    selector = Mock()
    selector.info = {'selected': False, 'bounds': {'top': 300, 'bottom': 360}}
    collector.d.return_value.exists = False
    with patch.object(collector, 'sku_region', return_value=(region, False)):
        assert collector.visible_sku_option(selector) == selector.info
    collector.d.swipe.assert_not_called()


def test_detail_search_continues_after_heading_scrolls_offscreen():
    heading = node()
    heading.append(node(text='商品详情', bounds='[0,250][300,300]'))
    later = node()
    container = node(**{'content-desc': '商品完整标题', 'bounds': '[0,100][300,550]'})
    container.append(node(**{'class': 'android.widget.ImageView', 'bounds': '[0,100][300,550]'}))
    later.append(container)
    viewer = node()
    viewer.append(node(text='1/7'))
    viewer.append(node(**{'class': 'android.widget.ImageView', 'bounds': '[0,100][300,550]'}))
    device = Mock()
    device.window_size.return_value = (300, 800)
    collector = NativeProduct(device)
    with patch.object(collector, 'check_title', side_effect=[heading, later]), \
            patch.object(collector, 'snapshot', return_value=('', viewer)), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        assert collector.open_detail('商品完整标题') is viewer
    device.click.assert_called_once()


@pytest.mark.parametrize('rectangle', [(0, 300, 299, 349), (120, 100, 179, 549)])
def test_crop_preserves_wide_and_narrow_image_aspect_ratios(rectangle):
    image = Image.new('RGB', (300, 650), 'black')
    ImageDraw.Draw(image).rectangle(rectangle, fill='white')
    x1, y1, x2, y2 = rectangle
    assert image_crop_bounds(image, (0, 0, 300, 650)) == (x1, y1, x2 + 1, y2 + 1)


def test_sku_summary_comparison_ignores_recommendation_badges():
    from automation.pdd.spider.native_product import sku_option_value
    assert sku_option_value('  混合装\n最多人选择') == '混合装'
    assert sku_option_value('鸡肉 11.11爆卖') == '鸡肉'


def test_sku_scan_merges_options_revealed_by_scrolling():
    first, last = node(), node()
    first.append(node(scrollable='true'))
    last.append(node(scrollable='true'))
    collector = NativeProduct(Mock())
    collector.d.window_size.return_value = (300, 800)
    collector.d.return_value.exists = False
    with patch('automation.pdd.spider.native_product.sku_dimensions',
               side_effect=[[{'name': '口味', 'options': ['鸡肉']}, {'name': '型号', 'options': ['100支']}],
                            [{'name': '型号', 'options': ['100支', '10支']}]]), \
            patch.object(collector, 'sku_region', side_effect=[({'top': 200, 'bottom': 700}, True), ({}, False)]), \
            patch.object(collector, 'snapshot', return_value=('', last)), \
            patch.object(collector, 'sku_to_top'), patch('automation.pdd.spider.native_product.time.sleep'):
        assert collector.all_sku_dimensions(first) == [
            {'name': '口味', 'options': ['鸡肉']}, {'name': '型号', 'options': ['100支', '10支']}]


def test_visible_sku_option_does_not_reset_scroll_position():
    collector = NativeProduct(Mock())
    collector.d.window_size.return_value = (300, 800)
    collector.d.return_value.exists = False
    selector = Mock()
    selector.info = {'selected': False, 'bounds': {'top': 300, 'bottom': 360}}
    with patch.object(collector, 'sku_region', return_value=({'top': 200, 'bottom': 700}, True)), \
            patch.object(collector, 'sku_to_top') as reset:
        assert collector.visible_sku_option(selector) == selector.info
    reset.assert_not_called()
    collector.d.swipe.assert_not_called()


def test_native_stage_order_and_resume(tmp_path):
    import json
    collector = NativeProduct(Mock())
    root = webview_goods_fixture()
    order = []
    records = [{'image_type': 'main', 'position': 1, 'local_path': 'main.jpg'}]
    Image.new('RGB', (20, 20)).save(tmp_path / 'main.jpg')
    def images(directory, title, **kwargs):
        order.append('detail' if kwargs else 'main')
        return ([{'image_type': 'detail', 'position': 1}] if kwargs else records), 1
    with patch.object(collector, 'snapshot', return_value=('', root)), patch.object(collector, 'top'), \
            patch.object(collector, 'images', side_effect=images), \
            patch.object(collector, 'sku', side_effect=lambda *a: (order.append('sku') or [['one']], 1)), \
            patch.object(collector, 'comments', side_effect=lambda *a: (order.append('comment') or [], 'end')):
        collector.collect_current(tmp_path)
        assert order == ['main', 'sku', 'comment', 'detail']
        (tmp_path / '图片清单.json').write_text(json.dumps(records), encoding='utf-8')
        order.clear()
        collector.collect_current(tmp_path, steps={'image_main'})
        assert order == []
        (tmp_path / 'main.jpg').unlink()
        collector.collect_current(tmp_path, steps={'image_main'})
        assert order == ['main']


def test_negative_stage_accepts_confirmed_empty_result(tmp_path):
    from automation.pdd.spider.status import update_stage
    (tmp_path / '差评原始.json').write_text('[]', encoding='utf-8')
    (tmp_path / '差评.csv').write_text('评论\n', encoding='utf-8')
    update_stage(tmp_path, 'negative_comments', 'completed', count=0)
    collector = NativeProduct(Mock())
    assert collector.stage_complete(tmp_path, 'negative_comments')
    (tmp_path / '差评原始.json').unlink()
    assert not collector.stage_complete(tmp_path, 'negative_comments')


@pytest.mark.parametrize('starts_with_video', [True, False])
def test_main_video_entry_switches_to_image_and_records_video(tmp_path, starts_with_video):
    title = '测试商品完整标题'
    def page(number, *, product=False, video=False):
        root = node()
        root.append(node(text=f'{number}/2'))
        if product:
            pager = node(**{'class': 'androidx.viewpager.widget.ViewPager', 'content-desc': title, 'bounds': '[0,0][300,300]'})
            pager.append(node(**{'class': 'android.widget.ImageView', 'content-desc': title, 'bounds': '[0,0][300,300]'}))
            root.append(pager)
            root.append(node(text='免拼购买'))
        if video:
            root.append(node(**{'content-desc': 'tronplayer_view'}))
        return root
    first, second = page(1, product=True, video=starts_with_video), page(2, product=True)
    viewer, video = page(1), page(2, video=True)
    collector = NativeProduct(Mock())
    collector.d.window_size.return_value = (300, 800)
    with patch.object(collector, 'top'), patch.object(collector, 'check_title', side_effect=[first, first, second, second]), \
            patch.object(collector, 'snapshot', side_effect=([] if starts_with_video else [('', first)]) + [('', viewer), ('', viewer), ('', viewer), ('', video)]), \
            patch.object(collector, 'enlarged_image', return_value={'page_counter': [1, 2], 'local_path': 'main.jpg', 'image_type': 'main'}), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        records, total = collector.images(tmp_path, title)
    assert total == 2
    assert {r['image_type'] for r in records} == {'main', 'main_video'}
    assert collector.d.click.call_count == (1 if starts_with_video else 2)
    assert collector.d.swipe.call_count == 2


def test_empty_webview_is_marked_for_later_processing(tmp_path):
    import json
    root = node()
    root.append(node(**{'class': 'meco.webkit.WebView'}))
    device = Mock()
    device.screenshot.return_value = Image.new('RGB', (20, 20))
    collector = NativeProduct(device)
    task = {'directory': str(tmp_path), 'rank': 1, 'row_json': json.dumps({
        '标题': '目标商品的完整标题用于识别', '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=123'})}
    with patch('automation.pdd.spider.android_jump.jump'), \
            patch.object(collector, 'snapshot', return_value=('<hierarchy/>', root)), \
            patch.object(collector, 'collect_current') as collect, \
            patch('automation.pdd.spider.native_product.time.sleep'):
        state, details = collector.collect(task)
    assert state == 'blocked_webview'
    assert details['status']['stages']['product']['state'] == 'blocked_webview'
    assert (tmp_path / '榜01/WebView页面.xml').is_file()
    assert (tmp_path / '榜01/WebView页面.jpg').is_file()
    collect.assert_not_called()


def test_readable_webview_is_not_classified_as_empty():
    root = node()
    view = node(**{'class': 'meco.webkit.WebView'})
    view.append(node(text='正在加载商品'))
    root.append(view)
    from automation.pdd.spider.native_product import UnreadableWebView
    with pytest.raises(RuntimeError) as error:
        product_info(root)
    assert not isinstance(error.value, UnreadableWebView)


def test_gallery_with_purchase_toolbar_is_recognized():
    root = node()
    root.append(node(text='1/10'))
    root.append(node(text='免拼购买'))
    pager = node(**{'class': 'androidx.viewpager.widget.ViewPager', 'bounds': '[0,0][1080,2340]'})
    pager.append(node(**{'class': 'android.widget.ImageView'}))
    root.append(pager)
    collector = NativeProduct(Mock())
    collector.d.window_size.return_value = (1080, 2340)
    assert collector.image_viewer(root, '商品标题')


@pytest.mark.parametrize('badge', ['1139人回购', '1.2万人回购', '1000+人回购'])
def test_sku_comparison_ignores_repurchase_badge_without_losing_specification(badge):
    from automation.pdd.spider.native_product import sku_option_value
    option = '  猫草片50粒*1瓶 ' + badge
    assert sku_option_value(option) == '猫草片50粒*1瓶'
    assert sku_option_value(option) in sku_option_value('已选择： 猫草片50粒*1瓶')
    assert sku_option_value('猫草片50粒*2瓶') != sku_option_value(option)


@pytest.mark.parametrize('value', ['商品1/11', '商品 1 / 11', '1/11'])
def test_image_counter_accepts_product_prefix(value):
    from automation.pdd.spider.native_product import image_counter
    root = node()
    root.append(node(text=value))
    assert image_counter(root) == [1, 11]


@pytest.mark.parametrize('stalls_forever', [False, True])
def test_video_page_retries_unadvanced_counter_before_classifying(tmp_path, stalls_forever):
    video, photo = node(), node()
    video.append(node(text='1/2'))
    video.append(node(**{'content-desc': 'tronplayer_view'}))
    photo.append(node(text='2/2'))
    collector = NativeProduct(Mock())
    collector.d.window_size.return_value = (300,800)
    pages = [('',video),('',video)] + ([('',video)] * 3 if stalls_forever else [('',video),('',photo)])
    with patch.object(collector,'top'), patch.object(collector,'check_title',return_value=video), \
            patch.object(collector,'open_detail',return_value=video), \
            patch.object(collector,'snapshot',side_effect=pages), \
            patch.object(collector,'enlarged_image',return_value={'image_type':'detail','local_path':'detail.jpg','page_counter':[2,2]}), \
            patch('automation.pdd.spider.native_product.time.sleep'):
        if stalls_forever:
            with pytest.raises(RuntimeError,match='页码不一致'):
                collector.images(tmp_path,'商品',detail=True)
            assert collector.d.swipe.call_count == 3
        else:
            records,total = collector.images(tmp_path,'商品',detail=True)
            assert total == 2 and len(records) == 2
            assert collector.d.swipe.call_count == 2


@pytest.mark.parametrize('width', [1080, 720])
def test_sold_out_template_matches_notice_but_not_blank_page(width):
    from pathlib import Path
    from automation.pdd.spider import native_product as native
    with Image.open(Path(native.__file__).with_name('assets') / 'sold_out_notice.png') as notice:
        page = Image.new('RGB', (1080, 2340), 'white')
        page.paste(notice, (270, 250))
    page = page.resize((width, round(2340 * width / 1080)))
    assert native.sold_out_notice(node(), page)
    assert not native.sold_out_notice(node(), Image.new('RGB', (width, width * 2), 'white'))


def test_sold_out_batch_saves_evidence_and_skips_without_collecting_recommendations(tmp_path):
    import json
    from automation.pdd.spider.native_product import SOLD_OUT_REASON
    device = Mock()
    device.screenshot.return_value = Image.new('RGB', (1080, 2340), 'white')
    collector = NativeProduct(device)
    root = node(text=SOLD_OUT_REASON)
    task = {'directory': str(tmp_path), 'rank': 1, 'row_json': json.dumps({
        '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=123'})}
    with patch('automation.pdd.spider.android_jump.jump'), \
            patch.object(collector, 'snapshot', return_value=('<hierarchy/>', root)), \
            patch.object(collector, 'collect_current') as collect:
        state, details = collector.collect(task)
    assert state == 'skipped'
    stage = details['status']['stages']['product']
    assert stage['reason'] == SOLD_OUT_REASON
    assert stage['outcome'] == 'sold_out'
    assert stage['retryable'] is False
    assert (tmp_path / '榜01/售罄页面.jpg').is_file()
    collect.assert_not_called()
    from automation.pdd.spider.status import reconcile_product
    assert reconcile_product(tmp_path / '榜01')['product_state'] == 'skipped'
