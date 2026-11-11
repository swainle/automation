import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.pdd.spider import retry
from automation.pdd.spider.status import update_stage, load_status
from automation.pdd.spider.product_data import mark_sold_out


def tasks(root, size):
    table = root / '榜' / '畅销榜' / '2026-10-26' / '榜单数据.xlsx'
    return [(table, {'排行': i, '标题': f'商品{i}'}) for i in range(1, size+1)]


class RetryTests(unittest.TestCase):
    def test_five_failures_pause_then_restart_at_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = tasks(root, 5)
            calls = []
            seen = set()
            def collect(table, row):
                n = row['排行']; calls.append(n)
                directory = table.parent / f'榜{n:02d}'
                if n not in seen:
                    seen.add(n)
                    mark_sold_out(directory)
                else:
                    update_stage(directory, 'access', 'completed')
                    update_stage(directory, 'product', 'skipped', '测试终态')
            def wait(epoch):
                state = load_status(root)['product_worker']
                self.assertEqual(state['state'], 'waiting_retry')
                self.assertEqual(state['resume_from']['rank'], 1)
                self.assertEqual(state['consecutive_sold_out'], 5)
                self.assertGreaterEqual(epoch - retry.time.time(), 299)
            with patch.object(retry, '_wait_until', side_effect=wait) as sleeper:
                retry.run_product_queue(queue, root, collect)
                sleeper.assert_called_once()
            self.assertEqual(calls, [1,2,3,4,5,1,2,3,4,5])

    def test_restart_uses_persisted_first_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); queue = tasks(root, 5)
            def fail(table, row):
                mark_sold_out(table.parent / f"榜{row['排行']:02d}")
            with patch.object(retry, '_wait_until', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    retry.run_product_queue(queue, root, fail)
            self.assertEqual(load_status(root)['product_worker']['resume_from']['rank'], 1)
            calls = []
            def finish(table, row):
                calls.append(row['排行'])
                directory = table.parent / f"榜{row['排行']:02d}"
                update_stage(directory, 'access', 'completed')
                update_stage(directory, 'product', 'skipped', '测试终态')
            with patch.object(retry, '_wait_until') as wait:
                retry.run_product_queue(queue, root, finish)
                wait.assert_called_once()
            self.assertEqual(calls, [1,2,3,4,5])

    def test_accessible_product_resets_consecutive_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); queue = tasks(root, 7); calls = []; seen = set()
            def collect(table, row):
                n = row['排行']; calls.append(n); directory = table.parent / f'榜{n:02d}'
                if n == 3 or n in seen:
                    update_stage(directory, 'access', 'completed')
                    update_stage(directory, 'product', 'skipped', '测试终态')
                else:
                    seen.add(n)
                    mark_sold_out(directory)
            with patch.object(retry, '_wait_until') as wait:
                retry.run_product_queue(queue, root, collect)
                wait.assert_not_called()
            self.assertEqual(calls, [1,2,3,4,5,6,7])

    def test_single_sold_out_does_not_trigger_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); queue = tasks(root, 1)
            calls = []
            def collect(table, row):
                calls.append(row['排行'])
                directory = table.parent / '榜01'
                update_stage(directory, 'access', 'completed')
                mark_sold_out(directory)
            with patch.object(retry, '_wait_until') as wait:
                for _ in range(11):
                    retry.run_product_queue(queue, root, collect)
                wait.assert_not_called()
            self.assertEqual(len(calls), 11)
            self.assertEqual(load_status(queue[0][0].parent / '榜01')['product_state'], 'skipped')

    def test_five_network_failures_do_not_trigger_sold_out_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); queue = tasks(root, 5); calls = []
            def collect(table, row):
                calls.append(row['排行'])
                update_stage(table.parent / f"榜{row['排行']:02d}", 'access', 'failed', '网络失败')
            with patch.object(retry, '_wait_until') as wait:
                retry.run_product_queue(queue, root, collect)
                wait.assert_not_called()
            self.assertEqual(calls, [1,2,3,4,5])


if __name__ == '__main__':
    unittest.main()
