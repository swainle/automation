import logging
import sys
import unittest
import io
import threading
from contextlib import redirect_stdout
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from automation.pdd.spider.logging_setup import DailySizeHandler, console_logging
from automation.pdd.spider.logging_setup import operation

class LoggingTests(unittest.TestCase):
    def test_file_error_does_not_reenter_redirected_stderr(self):
        with TemporaryDirectory() as tmp:
            output = io.StringIO()
            with patch.object(sys, '__stderr__', output), console_logging(Path(tmp)):
                with patch.object(DailySizeHandler, '_open', side_effect=PermissionError('denied')):
                    print('message survives file failure')
            self.assertIn('PermissionError: denied', output.getvalue())

    def test_size_rotation_preserves_chunks_and_date_switch(self):
        with TemporaryDirectory() as tmp:
            handler = DailySizeHandler(tmp, 'main', max_bytes=12)
            try:
                record = logging.LogRecord('test', logging.INFO, '', 0, 'abcdef', (), None)
                day = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d')
                for _ in range(3):
                    handler.handle(record)
                folder = Path(tmp) / day
                self.assertEqual(len(list(folder.glob('*.log'))), 3)
                self.assertTrue(all(p.stat().st_size <= 12 for p in folder.glob('*.log')))
                record.created += 86400 * 2
                handler.handle(record)
                next_day = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d')
                self.assertTrue((Path(tmp) / next_day / 'main.log').exists())
            finally:
                handler.close()

    def test_print_stderr_and_child_are_separate(self):
        with TemporaryDirectory() as tmp:
            original = sys.stdout
            with console_logging(Path(tmp)):
                print('main message')
                print('error message', file=sys.stderr)
                logging.getLogger('automation.pdd.spider.tunel').info('tunnel message')
            self.assertIs(sys.stdout, original)
            folder = Path(tmp) / 'logs' / datetime.now().strftime('%Y-%m-%d')
            main = (folder / 'main.log').read_text(encoding='utf-8')
            self.assertIn('main message', main)
            self.assertIn('error message', main)
            self.assertNotIn('tunnel message', main)
            self.assertIn('tunnel message', (folder / 'tunel.log').read_text(encoding='utf-8'))


def test_operation_visible_before_work_and_finishes_on_same_line(tmp_path):
    output = io.StringIO()
    with redirect_stdout(output), console_logging(tmp_path):
        with patch('automation.pdd.spider.logging_setup.time.perf_counter', side_effect=[10, 13.25]):
            with operation('采集商品'):
                assert output.getvalue().endswith('采集商品...')
                assert not output.getvalue().endswith('\n')
                folder = tmp_path / 'logs' / datetime.now().strftime('%Y-%m-%d')
                assert '采集商品...' in (folder / 'main.log').read_text(encoding='utf-8')
                print('共 20 条')
        assert output.getvalue().splitlines()[0].endswith('采集商品... 共 20 条 [3.2s]')
        assert len(output.getvalue().splitlines()) == 1
    lines = (folder / 'main.log').read_text(encoding='utf-8').splitlines()
    assert '采集商品...' in lines[0]
    assert lines[-1].endswith('采集商品... [3.2s]')


def test_background_output_and_nested_steps_do_not_mix(tmp_path):
    output = io.StringIO()
    with redirect_stdout(output), console_logging(tmp_path):
        with operation('外层'):
            worker = threading.Thread(target=lambda: logging.getLogger(
                'automation.pdd.spider.tunel').info('后台输出'))
            worker.start()
            worker.join()
            with operation('内层'):
                pass
    lines = output.getvalue().splitlines()
    assert lines[0].endswith('外层...')
    assert lines[1].endswith('后台输出')
    assert '内层... [' in lines[2]
    assert '外层... [' in lines[3]


def test_failure_and_interrupt_finish_line_and_propagate(tmp_path):
    output = io.StringIO()
    with redirect_stdout(output), console_logging(tmp_path):
        for error in (ValueError('测试失败'), KeyboardInterrupt()):
            try:
                with operation('执行动作'):
                    raise error
            except BaseException as caught:
                assert caught is error
        print('后续日志')
    lines = output.getvalue().splitlines()
    assert '执行动作... 失败：测试失败 [' in lines[0]
    assert '执行动作... 失败：KeyboardInterrupt [' in lines[1]
    assert lines[2].endswith('后续日志')
