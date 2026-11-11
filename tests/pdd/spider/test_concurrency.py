from types import SimpleNamespace
from unittest.mock import Mock, patch
import os
import subprocess
import sys
import time
import threading

import pytest

from automation.pdd.spider import config
from automation.pdd.spider.concurrency import ranking_active, ranking_lock, workflow_lock
from automation.pdd.spider.database import Store, today
from automation.pdd.spider.product import Product


@pytest.fixture(autouse=True)
def business_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'BUSINESS_ROOT', tmp_path)


def add_product(store, directory, rank=1):
    board = store.seed(today(), [{'name': '测试榜单', 'url': ''}])[0]
    store.discover(board['id'], [SimpleNamespace(tab='畅销榜', index=0, has_sub=False)])
    store.save_rows(1, directory, ['排行', '标题', '产品链接'],
                    [{'排行': n, '标题': f'商品{n}', '产品链接': f'https://mobile.yangkeduo.com/goods.html?goods_id={n}'}
                     for n in range(1, rank + 1)], 'ios', update_existing=True)


def test_workflows_and_devices_have_independent_locks():
    assert not ranking_active()
    with workflow_lock('rank', timeout=0), workflow_lock('device-ios', timeout=0):
        assert ranking_active()
        with workflow_lock('product', timeout=0), workflow_lock('device-android', timeout=0):
            with pytest.raises(TimeoutError):
                with workflow_lock('device-ios', timeout=0):
                    pass
    assert not ranking_active()


def test_rank_presence_is_detected_across_processes(tmp_path):
    ready = tmp_path / 'ready'
    script = '''from pathlib import Path
from automation.pdd.spider.concurrency import workflow_lock
import sys
with workflow_lock('rank'), workflow_lock('device-ios'):
    Path(sys.argv[1]).write_text('ready')
    sys.stdin.readline()
'''
    env = dict(os.environ, PDD_SPIDER_ROOT=str(tmp_path))
    child = subprocess.Popen([sys.executable, '-c', script, str(ready)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists()
        assert ranking_active()
        with workflow_lock('product', timeout=0), workflow_lock('device-android', timeout=0):
            with pytest.raises(TimeoutError):
                with workflow_lock('device-ios', timeout=0):
                    pass
    finally:
        child.communicate('\n', timeout=15)
    assert child.returncode == 0
    assert not ranking_active()


def test_same_subranking_excludes_writers_but_different_subranking_can_run():
    with ranking_lock(1, timeout=0):
        with ranking_lock(2, timeout=0):
            with pytest.raises(TimeoutError):
                with ranking_lock(1, timeout=0):
                    pass


def test_worker_waits_for_first_link_and_consumes_links_added_during_collection(tmp_path):
    store = Store(tmp_path / 'test.db')
    producer = Store(tmp_path / 'test.db')
    runtime = Mock()
    runner = Product(store, runtime, item_delay=(0, 0), delay=0)
    collected = []
    def collect(task):
        collected.append(task['rank'])
        if task['rank'] == 1:
            add_product(producer, tmp_path / 'images', rank=2)
        return 'completed', {}
    def sleep(seconds):
        if seconds == 5 and not collected:
            add_product(producer, tmp_path / 'images')
    runner.collect = collect
    try:
        with patch('automation.pdd.spider.concurrency.ranking_active', side_effect=[True, False, False, False]), \
                patch('automation.pdd.spider.product.time.sleep', side_effect=sleep):
            assert runner.run() == 0
        assert collected == [1, 2]
        assert [row['product_state'] for row in producer.product_tasks(day=today())] == ['completed', 'completed']
        assert producer.db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    finally:
        store.close()
        producer.close()


def test_worker_does_not_collect_a_subranking_while_rank_owns_it(tmp_path):
    store = Store(tmp_path / 'test.db')
    add_product(store, tmp_path / 'images')
    runner = Product(store, Mock(), item_delay=(0, 0), delay=0)
    runner.collect = Mock(return_value=('completed', {}))
    lock = ranking_lock(1, timeout=0)
    lock.__enter__()
    def sleep(seconds):
        runner.collect.assert_not_called()
        lock.__exit__(None, None, None)
    try:
        with patch('automation.pdd.spider.concurrency.ranking_active', side_effect=[True, False, False]), \
                patch('automation.pdd.spider.product.time.sleep', side_effect=lambda seconds: sleep(seconds) if seconds == 5 else None):
            assert runner.run() == 0
        runner.collect.assert_called_once()
    finally:
        store.close()


def test_sqlite_readers_and_short_writers_share_database(tmp_path):
    path = tmp_path / 'test.db'
    store = Store(path)
    add_product(store, tmp_path / 'images')
    acquired, errors = threading.Event(), []
    def ranking_writer():
        other = Store(path)
        try:
            with other.db:
                other.db.execute("UPDATE rankings SET state='completed' WHERE id=1")
                acquired.set()
                time.sleep(.2)
        except Exception as error:
            errors.append(error)
        finally:
            other.close()
    writer = threading.Thread(target=ranking_writer)
    writer.start()
    try:
        assert acquired.wait(5)
        # WAL permits a reader while the other connection owns the write transaction.
        assert len(store.product_tasks(day=today())) == 1
        # The product write waits for the short ranking transaction, then commits independently.
        store.product_state(1, 'completed', details={'info': {'goods_name': '商品1'}})
        writer.join(5)
        assert not writer.is_alive() and not errors
        assert store.list(1)['state'] == 'completed'
        assert store.item(1)['product_state'] == 'completed'
    finally:
        writer.join(5)
        store.close()
