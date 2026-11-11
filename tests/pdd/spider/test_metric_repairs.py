from types import SimpleNamespace
import json

import pytest

from automation.pdd.spider import ios
from automation.pdd.spider.database import Store
from automation.pdd.spider.metrics import from_text, from_row
from automation.pdd.spider.rank import BestReviewed, PriceDrop


@pytest.mark.parametrize('text', ['24小时内90+人拼单', '一周内100+人拼单', '30天内300+人拼单', '近30日1000+人拼单'])
def test_recent_sales_preserves_the_original_period(text):
    assert from_text(text)['recent_sales'] == text
    assert 'sales_24h' not in from_text(text)


def text(value, x=190, y=100):
    return f'<XCUIElementTypeStaticText label="{value}" x="{x}" y="{y}" width="60" height="24"/>'


def test_document_order_prevents_offscreen_price_from_crossing_cards():
    xml = '<XCUIElementTypeApplication>' + text('散装猫零食好评榜')
    xml += text('好评率90.94%', y=-100) + text('零食大礼包猫条猫薄荷饼干罐头完整标题', y=-1500)
    xml += text('24小时内90+人拼单', y=-1500) + text('券后', y=-1500) + text('￥', y=-1500) + text('36.59', y=-1500)
    xml += text('好评率90.70%', y=-10) + text('喵小胖蒸煮鸡肝猫咪狗狗宠物完整标题', y=50)
    xml += text('一周内100+人拼单', y=50) + text('￥', y=50) + text('6.6', y=50)
    xml += text('更多上榜商品推荐', y=500) + '</XCUIElementTypeApplication>'
    rows = ios._parse_rows(ios._xcui_texts(xml), BestReviewed)
    assert len(rows) == 2
    assert rows[0]['metrics']['coupon_price'] == '券后 ￥ 36.59'
    assert rows[1]['metrics']['display_price'] == '￥ 6.6'
    assert rows[1]['metrics']['recent_sales'] == '一周内100+人拼单'


def test_price_drop_rank_badges_include_split_coupon_and_reduction():
    xml = '<XCUIElementTypeApplication>' + text('散装猫零食降价榜')
    for rank, amount in [(1, '21.9'), (2, '33.6')]:
        xml += text(str(rank), x=20) + text(f'第{rank}件鸡肉冻干颗粒猫咪零食完整标题')
        xml += text('券后') + text('￥') + text(amount)
        xml += text('近30日成交价') + text('￥41.9') + text('直降') + text('￥20')
    xml += text('更多上榜商品推荐') + '</XCUIElementTypeApplication>'
    rows = ios._parse_rows(ios._xcui_texts(xml), PriceDrop)
    assert len(rows) == 2
    assert rows[0]['metrics']['coupon_price'] == '券后 ￥ 21.9'
    assert rows[0]['metrics']['deal_price_30d'] == '近30日成交价 ￥41.9'
    assert rows[0]['metrics']['price_drop'] == '直降 ￥20'
    assert from_row(rows[0])['display_price'] is None


def test_split_reference_price_is_not_misclassified_as_display_price():
    metrics = from_text('券后 ￥ 21.9 直降 ¥ 20 近 30 日成交价 ¥ 41.9')
    assert metrics['coupon_price'] == '券后 ￥ 21.9'
    assert metrics['deal_price_30d'] == '近 30 日成交价 ¥ 41.9'
    assert metrics['price_drop'] == '直降 ¥ 20'
    assert metrics['display_price'] is None


def test_refresh_keeps_existing_id_and_product_link(tmp_path):
    store = Store(tmp_path / 'sqlite.db')
    board = store.seed('2026-11-02', [{'name': '猫粮', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='好评榜', index=0, has_sub=False)])
    old = {'排行': 1, '标题': '猫粮', '产品ID': '123', '产品链接': 'https://example.com/123', 'metrics': {'sales_24h': '24小时内90+人拼单'}}
    store.save_rows(1, tmp_path, [], [old], 'ios')
    item_id = store.items(1)[0]['id']
    row = {'排行': 1, '标题': '猫粮', 'metrics': from_text('一周内100+人拼单 券后 ￥36.59')}
    store.save_rows(1, tmp_path, [], [row], 'ios', update_existing=True)
    updated = store.items(1)[0]
    assert updated['id'] == item_id
    assert updated['goods_id'] == '123'
    assert updated['goods_url'] == old['产品链接']
    assert json.loads(updated['metrics_json'])['recent_sales'] == '一周内100+人拼单'
    assert 'sales_24h' not in json.loads(updated['metrics_json'])
    store.close()
