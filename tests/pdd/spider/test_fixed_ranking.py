import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from automation.pdd.spider.database import Store
from automation.pdd.spider.ranking import Ranking
from automation.pdd.spider.utils import read_xlsx


DAY = '2026-10-31'
SOURCES = [{'name': '主榜甲', 'url': 'https://example.com/one'}, {'name': '主榜乙', 'url': 'https://example.com/two'}]


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'sqlite.db')
    yield value
    value.close()


class Device:
    def __init__(self, name, root, store, calls):
        self.name, self.root, self.store, self.calls = name, root, store, calls
        self.fail_read = set()
        self.fail_discover = set()
        self.fail_shot = False

    def discover(self, board):
        self.calls.append((self.name, 'discover', board['name']))
        if board['name'] in self.fail_discover:
            raise RuntimeError('page unavailable')
        return [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)]

    def read(self, board, entry, day):
        self.calls.append((self.name, 'read', board['name']))
        if board['name'] in self.fail_read:
            raise RuntimeError('risk control')
        directory = self.root / board['name'] / entry['tab'] / day
        return ['排行', '标题', '价格', '产品链接'], [{'排行': 1, '标题': '商品', '价格': '12.30', '产品ID': '123', '产品链接': 'https://mobile.yangkeduo.com/goods.html?goods_id=123'}], directory

    def prepare(self, board, entry, day):
        self.calls.append((self.name, 'prepare', board['name']))
        self.entry = entry

    def screenshot(self, row):
        self.calls.append((self.name, 'screenshot', row['标题']))
        assert self.store.items(self.entry['id']), 'rows must be committed before image work'
        if self.fail_shot:
            raise RuntimeError('image failed')
        directory = Path(self.entry['directory']) / '榜01'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / '排行截图.jpg').write_bytes(b'raw image')

    def crop(self, row):
        self.calls.append((self.name, 'crop', row['标题']))
        (Path(self.entry['directory']) / '榜01/产品截图.jpg').write_bytes(b'crop')

    def link(self, row):
        return row

    def finish_item(self):
        pass


def devices(tmp_path, store):
    calls = []
    return Device('sogou', tmp_path, store, calls), Device('ios', tmp_path, store, calls), calls


def test_browser_and_ios_are_explicit_modes_and_only_unfinished_boards_are_retried(tmp_path, store):
    browser, ios, calls = devices(tmp_path, store)
    browser.fail_read.add('主榜甲')
    runtime = Mock()
    assert Ranking(store, runtime, browser, attempts=1, delay=0).run(SOURCES, day=DAY) == 0
    assert not any(call[0] == 'ios' for call in calls)
    runtime.ensure_tunnel.assert_not_called()
    runtime.start_ranking_browser.assert_called_once()
    assert len(store.product_tasks()) == 1
    calls.clear()
    phone_runtime = Mock()
    assert Ranking(store, phone_runtime, ios, attempts=1, delay=0).run(SOURCES, day=DAY) == 0
    assert not any(call[0] == 'sogou' for call in calls)
    assert [call[2] for call in calls if call[1] == 'read'] == ['主榜甲']
    rows = [tuple(entry[key] for key in ('device','state','collect_state','export_state')) for board in store.boards(DAY) for entry in store.lists(board['id'])]
    assert [tuple(row) for row in rows] == [('ios', 'completed', 'completed', 'completed'), ('sogou', 'completed', 'completed', 'completed')]
    header, values = read_xlsx(tmp_path / '主榜甲/畅销榜' / DAY / '榜单数据.xlsx')
    assert header == ['排行', '标题', '价格', '产品链接']
    assert values[0][:3] == [1, '商品', '12.30']
    assert len(store.product_tasks()) == 2
    assert [call[0] for call in phone_runtime.method_calls[:4]] == ['ensure_tunnel', 'ensure_image', 'ensure_driver', 'ensure_forward']
    phone_runtime.start_ranking_browser.assert_not_called()


def test_screenshot_failure_retries_on_same_device_and_resumes_without_recollecting(tmp_path, store):
    browser, ios, calls = devices(tmp_path, store)
    original = browser.screenshot
    def screenshot(row):
        browser.fail_shot = sum(call[1] == 'screenshot' for call in calls) < 2
        return original(row)
    browser.screenshot = screenshot
    assert Ranking(store, Mock(), browser, attempts=2, delay=0).run(SOURCES[:1], day=DAY) == 0
    assert sum(call[1] == 'screenshot' for call in calls) == 3
    assert not any(call[0] == 'ios' for call in calls)
    assert sum(call[1] == 'read' for call in calls) == 1
    browser.fail_shot = False
    calls.clear()
    assert Ranking(store, Mock(), browser, attempts=1, delay=0).run(SOURCES[:1], day=DAY) == 0
    assert not any(call[1] in {'read', 'discover'} for call in calls)
    assert len(store.items(1)) == 1
    assert len(store.product_tasks()) == 1


def test_both_devices_fail_to_discover_records_skip_and_continues(tmp_path, store):
    browser, ios, calls = devices(tmp_path, store)
    browser.fail_discover.add('主榜甲')
    ios.fail_discover.add('主榜甲')
    assert Ranking(store, Mock(), browser, attempts=1, delay=0).run(SOURCES, day=DAY) == 0
    states = [(board['state'],) for board in store.boards(DAY)]
    assert [row[0] for row in states] == ['access_failed', 'completed']
    assert len(store.product_tasks()) == 1


def test_both_devices_fail_to_read_child_records_skip(tmp_path, store):
    browser, ios, calls = devices(tmp_path, store)
    browser.fail_read.add('主榜甲')
    ios.fail_read.add('主榜甲')
    assert Ranking(store, Mock(), browser, attempts=1, delay=0).run(SOURCES[:1], day=DAY) == 0
    assert store.list(1)['state'] == 'access_failed'
    assert store.product_tasks() == []


def test_interrupt_during_images_preserves_data_and_allows_linked_products(tmp_path, store):
    browser, ios, calls = devices(tmp_path, store)
    browser.screenshot = Mock(side_effect=KeyboardInterrupt)
    runtime = Mock()
    with pytest.raises(KeyboardInterrupt):
        Ranking(store, runtime, browser, attempts=1, delay=0).run(SOURCES[:1], day=DAY)
    assert store.list(1)['collect_state'] == 'completed'
    assert len(store.items(1)) == 1
    runtime.close.assert_called_once()
    assert len(store.product_tasks()) == 1


def test_resume_after_crop_interrupt_keeps_saved_screenshot(tmp_path, store):
    browser, ios, calls = devices(tmp_path, store)
    crop = browser.crop
    browser.crop = Mock(side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        Ranking(store, Mock(), browser, attempts=1, delay=0).run(SOURCES[:1], day=DAY)
    assert store.items(1)[0]['screenshot_state'] == 'completed'
    calls.clear()
    browser.crop = crop
    assert Ranking(store, Mock(), browser, attempts=1, delay=0).run(SOURCES[:1], day=DAY) == 0
    assert not any(call[1] in {'read', 'screenshot'} for call in calls)
    assert store.items(1)[0]['crop_state'] == 'completed'


def test_export_failure_retries_without_repeating_images(tmp_path, store):
    browser, ios, calls = devices(tmp_path, store)
    export = store.export
    store.export = Mock(side_effect=[OSError('file locked'), None])
    def retry_export(list_id):
        if store.export.call_count == 1:
            raise OSError('file locked')
        return export(list_id)
    store.export.side_effect = retry_export
    assert Ranking(store, Mock(), browser, attempts=1, delay=0).run(SOURCES[:1], day=DAY) == 0
    assert store.export.call_count == 2
    assert sum(call[1] == 'screenshot' for call in calls) == 1
    assert not any(call[0] == 'ios' for call in calls)


def test_database_transaction_rolls_back_invalid_rows(tmp_path, store):
    board = store.seed(DAY, SOURCES[:1])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    with pytest.raises(ValueError):
        store.save_rows(1, tmp_path, ['排行'], [{'排行': 1}, {'排行': 1}], 'sogou')
    assert store.items(1) == []
    assert store.list(1)['collect_state'] == 'pending'


def test_preparation_failure_cleans_services_and_does_not_start_browser(tmp_path, store):
    runtime = Mock()
    runtime.ensure_image.side_effect = RuntimeError('mount failed')
    browser, ios, calls = devices(tmp_path, store)
    with pytest.raises(RuntimeError, match='mount failed'):
        Ranking(store, runtime, ios).run(SOURCES, day=DAY)
    assert calls == []
    runtime.ensure_driver.assert_not_called()
    runtime.close.assert_called_once()


def test_source_url_change_is_rejected_without_overwriting_existing_day(store):
    store.seed(DAY, SOURCES[:1])
    with pytest.raises(ValueError, match='链接已变更'):
        store.seed(DAY, [{'name': SOURCES[0]['name'], 'url': 'https://example.com/changed'}])
    assert store.boards(DAY)[0]['url'] == SOURCES[0]['url']


def test_explicit_device_switch_recollects_partial_list_without_mixing_images(tmp_path, store):
    browser, ios, calls = devices(tmp_path, store)
    browser.crop = Mock(side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        Ranking(store, Mock(), browser, attempts=1, delay=0).run(SOURCES[:1], day=DAY)
    assert store.list(1)['device'] == 'sogou'
    calls.clear()
    assert Ranking(store, Mock(), ios, attempts=1, delay=0).run(SOURCES[:1], day=DAY) == 0
    assert store.list(1)['device'] == 'ios'
    assert all(call[0] == 'ios' for call in calls)
    assert len(store.items(1)) == 1
    assert any(call[1] == 'screenshot' for call in calls)
