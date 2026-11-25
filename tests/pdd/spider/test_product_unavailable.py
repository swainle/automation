from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from automation.pdd.spider import weekly_mobile as flow
from automation.pdd.spider.analysis import AnalysisRunner
from automation.pdd.spider.full_flow import product_unavailable_reason
from automation.pdd.spider.mobile_database import PRODUCT_COLUMNS, Store, product_complete, product_skipped


QUOTA_XML = '''<XCUIElementTypeApplication>
    <XCUIElementTypeButton name="发起拼单名额已满" visible="true"/>
    </XCUIElementTypeApplication>'''


def test_full_group_order_button_is_unavailable():
    assert product_unavailable_reason(QUOTA_XML) == '发起拼单名额已满'
    assert product_unavailable_reason(QUOTA_XML.replace('已满', '已 满')) == '发起拼单名额已满'
    assert not product_unavailable_reason(QUOTA_XML.replace('visible="true"', 'visible="false"'))
    assert not product_unavailable_reason(QUOTA_XML.replace('名额已满', ''))


def test_quota_product_skips_immediately_then_processes_next_product(tmp_path, monkeypatch):
    store = Mock()
    store.items.return_value = []
    store.db.transaction.side_effect = nullcontext
    items = {n: dict(id=n, list_id=1, rank=n, title=str(n), product_screenshot_state='completed') for n in (1, 2)}
    store.save_item.side_effect = lambda rid, directory, row: items[row['排行']]
    store.item.side_effect = lambda pid: items[pid]
    pages = iter([QUOTA_XML, '<root/>'])
    read = Mock(side_effect=lambda client: next(pages))
    monkeypatch.setattr(flow.ios, '_current_page_source', read)
    monkeypatch.setattr(flow, 'wait_for_elements', lambda read_source, **kwargs: read_source())
    monkeypatch.setattr(flow, 'locate_product', lambda *args: SimpleNamespace(displayed=True, tap=Mock()))
    monkeypatch.setattr(flow, 'product_page_checks', lambda source, title: ([], dict(title_candidates=[title], observed_title=title)))
    monkeypatch.setattr(flow, 'return_to_board', Mock())
    collector = Mock()
    monkeypatch.setattr(flow, 'WeeklyProduct', Mock(return_value=collector))
    runner = Mock()
    entry = dict(id=1, directory=str(tmp_path), main_name='猫粮', tab='畅销榜')
    flow.run_products(store, Mock(), entry, [{'排行': n, '标题': str(n)} for n in (1, 2)], single_round=True, analysis=runner)
    assert read.call_count == 2  # No 180-second readiness wait for the quota page.
    store.skip_product.assert_called_once_with(1, '发起拼单名额已满')
    assert collector.collect.call_args.args[0]['id'] == 2
    collector.collect.assert_called_once()
    runner.process.assert_called_once_with(items[2])
    assert flow.return_to_board.call_count == 2


def test_skipped_product_is_terminal_and_analysis_is_not_run():
    item = dict(id=1, **dict.fromkeys(PRODUCT_COLUMNS, 'skipped'))
    assert product_complete(item) and product_skipped(item)
    runner = AnalysisRunner(Mock())
    assert runner.process(item)
    runner.store.db.execute.assert_not_called()


def test_skip_survives_reopen_and_is_not_reselected(pg_database, tmp_path, monkeypatch):
    store = Store()
    try:
        entry = store.seed_rankings('2026-11-16', [dict(name='猫粮',main_name='猫粮',tab='畅销榜',
            url='https://example.com/rank',tab_index=0)], tmp_path/'data')[0]
        item = store.save_item(entry['id'], entry['directory'], {'排行':1,'标题':'商品'})
        store.stage_state(entry['id'],'main_images','completed',item_id=item['id'])
        store.skip_product(item['id'],'发起拼单名额已满')
        store.close()
        store = Store()
        item = store.item(item['id'])
        assert item['main_images_state'] == 'completed'
        assert product_complete(item) and product_skipped(item)
        runner = AnalysisRunner(store)
        assert runner.is_complete(item) and not runner.has_failed([item])
        assert runner.state(item['id'])['last_error'] == '发起拼单名额已满'
        phone = Mock(side_effect=AssertionError('skipped product reopened'))
        monkeypatch.setattr(flow, 'product_element', phone)
        flow.run_products(store, Mock(), entry, [{'排行':1,'标题':'商品'}])
        phone.assert_not_called()
    finally:
        store.close()
