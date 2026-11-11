"""默认浏览器、实时日志、分块和取消退出的回归验证。"""
import asyncio
import contextlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from automation.pdd.screenshot.config import Config
from automation.pdd.screenshot.logs import ChinaFormatter, DailyFiles, configure, log, close
from automation.pdd.screenshot.runner import ShopRunner
from automation.pdd.screenshot.services import InfrastructureError, Services, StartupError
from automation.com.extension_bridge import BridgeError


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_ws_is_reused_and_not_owned(self):
        services = Services(Config(root=Path.cwd()))
        services.spawn = Mock()
        services.wait_ready = AsyncMock()
        with patch('automation.pdd.screenshot.services.port_free', side_effect=[False, True]), patch(
            'automation.pdd.screenshot.services.request', new=AsyncMock(return_value={'protocol': 3})
        ), patch('automation.pdd.screenshot.services.subprocess.Popen') as spawn, patch(
            'automation.pdd.screenshot.services.close_existing_chrome', return_value=0
        ), patch('automation.pdd.screenshot.services.prepare_default_profile', return_value={'profile': 'Default', 'backup': None}), patch(
            'automation.pdd.screenshot.services.launch_chrome'
        ):
            await services.start()
        spawn.assert_not_called()
        self.assertFalse(services.processes)
        self.assertEqual(services.spawn.call_count, 1)  # Only the Cookie server.

    async def test_default_launch_has_no_user_directory_and_exited_launcher_is_healthy(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(root=Path(folder))
            services = Services(config)
            services.spawn = Mock()
            services.wait_ready = AsyncMock()
            browser = Mock()
            browser.poll.return_value = 0  # Chrome 将请求交给已有实例后的正常退出。
            with patch("automation.pdd.screenshot.services.close_existing_chrome", return_value=2) as close_chrome, patch(
                "automation.pdd.screenshot.services.prepare_default_profile", return_value={"profile": "Default", "backup": None}
            ), patch("automation.pdd.screenshot.services.port_free", return_value=True), patch(
                "automation.pdd.screenshot.services.launch_chrome", return_value=browser
            ) as launch, patch("automation.pdd.screenshot.services.subprocess.Popen"):
                await services.start()
            launch.assert_called_once_with(url="about:blank", quiet_startup=True)
            close_chrome.assert_called_once()
            services.check_processes()
            browser.poll.return_value = 1
            with self.assertRaises(InfrastructureError):
                services.check_processes()

    async def test_stop_closes_tasks_but_never_kills_default_chrome(self):
        services = Services(Config(root=Path.cwd()))
        browser = Mock()
        services.browser = browser
        with patch("automation.pdd.screenshot.services.request", new=AsyncMock()) as request, patch(
            "automation.pdd.screenshot.services.stop_tree"
        ) as stop:
            await services.stop()
        request.assert_awaited_once_with(services.config.endpoint, "automation_tabs_close")
        stop.assert_not_called()
        browser.terminate.assert_not_called()

    async def test_other_user_pdd_tabs_stop_without_retrying_or_clearing(self):
        runner = ShopRunner(Config(root=Path.cwd()), Mock(), Mock())
        runner.rpc = AsyncMock(side_effect=BridgeError("other_pdd_tabs_open"))
        with self.assertRaisesRegex(StartupError, "其他拼多多标签页"):
            await runner.cleanup()
        self.assertEqual(runner.rpc.await_count, 1)

    async def test_old_plugin_is_rejected_before_any_legacy_clear(self):
        runner = ShopRunner(Config(root=Path.cwd()), Mock(), Mock())
        runner.rpc = AsyncMock(side_effect=BridgeError("unsupported_action"))
        with self.assertRaisesRegex(StartupError, "重新加载"):
            await runner.cleanup()
        self.assertEqual(runner.rpc.call_args.args[0], "automation_state_clear")


class ConfigurationTests(unittest.TestCase):
    def test_old_profile_env_no_longer_changes_default_chrome(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"AUTOMATION_PROFILE": "old-profile"}, clear=True):
            project = Path(folder) / "project"
            business = Path(folder) / "automation.pdd.screenshot"
            business.mkdir()
            (business / ".env").write_text("", encoding="utf-8")
            config = Config.load(project)
            self.assertFalse(hasattr(config, "profile"))


class LogTests(unittest.TestCase):
    def tearDown(self):
        close()

    def test_terminal_prefix_and_daily_files(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as terminal:
            configure(folder)
            log("WS", "插件已连接")
            log("CS", "GET /cookies 200")
            close()
            self.assertIn("[WS] 插件已连接", terminal.getvalue())
            files = list(Path(folder).glob("*/ws.log"))
            self.assertEqual(len(files), 1)
            self.assertIn("插件已连接", files[0].read_text(encoding="utf-8"))
            self.assertTrue(files[0].with_name("cs.log").is_file())

    def test_default_limit_is_twenty_mib(self):
        self.assertEqual(DailyFiles(Path.cwd()).max_bytes, 20 * 1024 * 1024)

    def test_chunks_preserve_utf8_and_existing_archives_across_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            handler = DailyFiles(folder, max_bytes=64)
            handler.setFormatter(ChinaFormatter("%(message)s"))
            text = "中文日志" * 20
            def emit(handler):
                record = logging.LogRecord("fixture", logging.INFO, "", 0, text, (), None)
                record.source = "WS"
                handler.handle(record)
            emit(handler)
            # 模拟重启，编号不能覆盖已有分块。
            emit(DailyFiles(folder, max_bytes=64))
            directory = next(Path(folder).iterdir())
            archives = sorted(directory.glob("ws.*.log"), key=lambda p: int(p.stem.split(".")[1]))
            files = archives + [directory / "ws.log"]
            self.assertGreater(len(files), 2)
            self.assertTrue(all(p.stat().st_size <= 64 for p in files))
            recovered = "".join(p.read_text(encoding="utf-8") for p in files)
            self.assertEqual(recovered, (text + "\n") * 2)

    def test_child_stdout_stderr_are_both_streamed_with_source(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as terminal:
            config = Config(root=Path(folder))
            services = Services(config)
            configure(Path(folder) / "logs")
            child = services.spawn("timeit", "-n", "1", "-r", "1", "import sys; print('child fixture'); print('stderr fixture', file=sys.stderr)")
            child.wait(timeout=5)
            for thread in services.log_threads:
                thread.join(2)
            close()
            self.assertIn("[WS] child fixture", terminal.getvalue())
            self.assertIn("[WS] stderr fixture", terminal.getvalue())
            self.assertTrue(list((Path(folder) / "logs").glob("*/ws.log")))


class InterruptTests(unittest.TestCase):
    def test_repeated_interrupt_waits_for_cleanup_without_thread_join_traceback(self):
        source = '''
import asyncio, signal, time
from automation.pdd.screenshot import run_interruptible
from automation.pdd.screenshot.blocking import blocking
async def work():
    loop = asyncio.get_running_loop()
    interrupt = signal.getsignal(signal.SIGINT)
    loop.call_later(.03, interrupt, signal.SIGINT, None)
    loop.call_later(.05, interrupt, signal.SIGINT, None)
    try:
        await blocking(time.sleep, 60)
    finally:
        await asyncio.sleep(.08)
        print("cleanup completed")
try:
    run_interruptible(work())
except KeyboardInterrupt:
    print("stopped")
'''
        result = subprocess.run([sys.executable, "-B", "-c", source], capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cleanup completed", result.stdout)
        self.assertIn("stopped", result.stdout)
        self.assertNotIn("KeyboardInterrupt", result.stderr)

class SSHCredentialsTests(unittest.IsolatedAsyncioTestCase):
    async def test_username_password_port_are_loaded_and_password_is_not_in_repr(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder) / "project"
            business = Path(folder) / "automation.pdd.screenshot"
            business.mkdir()
            (business / '.env').write_text('CS_SSH_HOST=fixture\nCS_SSH_PORT=2222\nCS_SSH_USER=operator\nCS_SSH_PASSWORD=" fixture secret "\nCS_SSH_COMMAND=fixture-command\n', encoding='utf-8')
            cfg = Config.load(root)
            self.assertEqual(cfg.ssh_password, ' fixture secret ')
            self.assertNotIn('fixture secret', repr(cfg))
            service = Services(cfg)
            with patch('asyncssh.connect', new=AsyncMock(side_effect=RuntimeError('stop before network'))) as connect:
                with self.assertRaisesRegex(RuntimeError, 'stop before network'):
                    await service._remote_start()
            options = connect.call_args.kwargs
            self.assertEqual(options['username'], 'operator')
            self.assertEqual(options['password'], ' fixture secret ')
            self.assertEqual(options['port'], 2222)
            self.assertNotIn('known_hosts', options)

    async def test_empty_password_preserves_default_key_auth(self):
        service = Services(Config(root=Path.cwd(), ssh_host='fixture', ssh_key='fixture-key'))
        with patch('asyncssh.connect', new=AsyncMock(side_effect=RuntimeError('stop before network'))) as connect:
            with self.assertRaises(RuntimeError):
                await service._remote_start()
        self.assertNotIn('password', connect.call_args.kwargs)
        self.assertEqual(connect.call_args.kwargs['client_keys'], ['fixture-key'])

class StepTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_step_waits_half_second_and_post_import_waits_one(self):
        from automation.pdd.credentials import ShopCredential
        runner = ShopRunner(Config(root=Path.cwd()), Mock(), Mock(), day_clock=lambda: '2026-10-29')
        runner.day = '2026-10-29'
        runner.shop = ShopCredential('fixture', '', '')
        with patch('automation.pdd.screenshot.runner.asyncio.sleep', new=AsyncMock()) as sleep:
            await runner.step('validate_cookies')
            await runner.step('login', delay=1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [.5, 1])

    async def test_navigation_errors_remain_retriable_business_errors(self):
        from automation.pdd.screenshot.runner import BusinessError
        runner = ShopRunner(Config(root=Path.cwd()), Mock(), Mock())
        runner.tab = 1
        runner.rpc = AsyncMock(side_effect=BridgeError('page_not_allowed'))
        with self.assertRaisesRegex(BusinessError, 'navigation_page_not_allowed'):
            await runner.enter_workbench()
