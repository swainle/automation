import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from automation.pdd.spider.processes import ProcessManager, go_ios_path
from automation.pdd.spider.logging_setup import console_logging

class ProcessTests(unittest.TestCase):
    def test_platform_selection(self):
        for system, arch, filename in [('Windows','AMD64','ios.exe'), ('Linux','x86_64','ios-amd64'), ('Darwin','arm64','ios')]:
            with patch('platform.system', return_value=system), patch('platform.machine', return_value=arch):
                self.assertEqual(go_ios_path(Path('.')).name, filename)
        with patch('platform.system', return_value='Linux'), patch('platform.machine', return_value='aarch64'):
            with self.assertRaises(RuntimeError):
                go_ios_path(Path('.'))

    def test_live_output_and_cleanup(self):
        with TemporaryDirectory() as tmp, console_logging(Path(tmp)):
            manager = ProcessManager()
            from datetime import datetime
            log = Path(tmp) / 'logs' / datetime.now().strftime('%Y-%m-%d') / 'forward.log'
            try:
                manager.start('forward', [sys.executable, '-u', '-c', 'import time; print("ready"); time.sleep(30)'])
                proc = manager.processes[0][1]
                import time
                deadline = time.monotonic() + 5
                while (not log.exists() or 'ready' not in log.read_text(encoding='utf-8')) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn('ready', log.read_text(encoding='utf-8'))
                manager.check()
            finally:
                manager.close()
            self.assertIsNotNone(proc.poll())
            manager.close()

    def test_detect_exited_process(self):
        with TemporaryDirectory() as tmp, console_logging(Path(tmp)):
            manager = ProcessManager()
            try:
                manager.start('failed', [sys.executable, '-c', 'raise SystemExit(7)'])
                manager.processes[0][1].wait(timeout=5)
                with self.assertRaisesRegex(RuntimeError, '7'):
                    manager.check()
            finally:
                manager.close()

    def test_stopping_one_service_preserves_another_owned_service(self):
        manager = ProcessManager()
        try:
            manager.start('first', [sys.executable, '-c', 'import time; time.sleep(30)'])
            manager.start('second', [sys.executable, '-c', 'import time; time.sleep(30)'])
            first, second = [entry[1] for entry in manager.processes]
            manager.stop('first')
            self.assertIsNotNone(first.poll())
            self.assertIsNone(second.poll())
            manager.check()
        finally:
            manager.close()

    def test_cleanup_contains_spawned_descendants(self):
        import psutil
        import time
        manager = ProcessManager()
        descendants = []
        try:
            manager.start('tree', [sys.executable, '-c',
                'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); time.sleep(30)'])
            parent = psutil.Process(manager.processes[0][1].pid)
            deadline = time.monotonic() + 5
            while not descendants and time.monotonic() < deadline:
                descendants = parent.children(recursive=True)
                time.sleep(0.05)
            self.assertTrue(descendants)
        finally:
            manager.close()
        psutil.wait_procs(descendants, timeout=5)
        for process in descendants:
            try:
                self.assertTrue(not process.is_running() or process.status() == psutil.STATUS_ZOMBIE)
            except psutil.NoSuchProcess:
                pass
