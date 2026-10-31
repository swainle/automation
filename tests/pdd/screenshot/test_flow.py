import asyncio
import base64
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from automation.pdd.screenshot import run_cycle
from automation.pdd.screenshot.config import Config
from automation.pdd.screenshot.remote import supervise
from automation.pdd.screenshot.runner import (
    BusinessError, CleanupFatal, DayChanged, LoginError, ShopRunner, VerificationLimit, download_cookies, shop_segment,
)
from automation.pdd.screenshot.services import InfrastructureError, stop_tree
from automation.pdd.screenshot.state import RunLock, StateStore
from automation.pdd.credentials import ShopCredential

DAY = "2026-10-29"
SHOP = ShopCredential("测试店", "fake-account", "fake-password", row_number=2)


class StateTests(unittest.TestCase):
    def test_daily_resume_and_interrupted_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sqlite.db"
            store = StateStore(path)
            names = ["成功", "登录异常", "失败", "运行中"]
            store.seed(DAY, names)
            store.finish(DAY, "成功", "success", cleanup_ok=True)
            store.finish(DAY, "登录异常", "login_error", "cookies_missing", True)
            store.finish(DAY, "失败", "business_failed", "screenshot_failed", True)
            store.begin(DAY, "运行中")
            store.close()
            store = StateStore(path)
            store.seed(DAY, names)
            self.assertTrue(store.completed(DAY, "成功"))
            self.assertTrue(store.completed(DAY, "登录异常"))
            self.assertFalse(store.completed(DAY, "失败"))
            self.assertEqual(store.get(DAY, "运行中")["status"], "interrupted")
            store.seed("2026-10-30", names)
            self.assertFalse(store.completed("2026-10-30", "成功"))
            store.close()

    def test_exclusive_lock_released(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.lock"
            with RunLock(path):
                with self.assertRaises(RuntimeError):
                    with RunLock(path):
                        pass
            with RunLock(path):
                pass

    def test_shop_paths_cannot_escape_or_collide_on_windows(self):
        self.assertEqual(shop_segment("黄金万两宠物零食店"), "黄金万两宠物零食店")
        for name in ("../x", "a/b", "a\\b", "a.", "NUL", "C:x", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                shop_segment(name)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_sms_flag_only_skips_previously_recorded_sms_for_today(self):
        shops = [ShopCredential(name, '', '', index + 2) for index, name in enumerate(['短信', '滑块', '其他', '新店'])]
        self.store.seed(DAY, [shop.shop_name for shop in shops])
        for shop, status in zip(shops, ['sms_required', 'slider_required', 'verification_other']):
            self.store.finish(DAY, shop.shop_name, status)
        self.runner.skip_sms_shop = True
        self.runner.execute = AsyncMock(side_effect=LoginError('sms_required'))
        await self.runner.run_day(DAY, shops)
        self.assertEqual(self.runner.execute.await_count, 3)
        self.assertEqual(self.store.get(DAY, '短信')['attempts'], 0)
        self.assertEqual(self.store.get(DAY, '新店')['status'], 'sms_required')
        self.runner.skip_sms_shop = False
        self.runner.execute.reset_mock()
        await self.runner.run_day(DAY, shops)
        self.assertEqual(self.runner.execute.await_count, 4)

    async def test_six_consecutive_verifications_stop_after_cleanup_and_export(self):
        shops = [ShopCredential(f'店{i}', '', '', i + 2) for i in range(8)]
        self.store.seed(DAY, [shop.shop_name for shop in shops])
        self.store.finish(DAY, shops[3].shop_name, 'login_error')  # skipped shops don't reset/count
        self.runner.execute = AsyncMock(side_effect=[LoginError(status) for status in
            ['slider_required', 'sms_required', 'verification_other'] * 2])
        with self.assertRaises(VerificationLimit):
            await self.runner.run_day(DAY, shops)
        self.assertEqual(self.runner.execute.await_count, 6)
        self.assertEqual(self.store.get(DAY, shops[6].shop_name)['status'], 'verification_other')
        self.assertTrue(self.store.get(DAY, shops[6].shop_name)['cleanup_ok'])
        self.assertEqual(self.store.get(DAY, shops[7].shop_name)['status'], 'pending')
        self.assertTrue((self.root / 'data' / DAY / '店铺应收截图.xlsx').exists())

    async def test_other_result_resets_verification_streak(self):
        shops = [ShopCredential(f'店{i}', '', '', i + 2) for i in range(11)]
        self.runner.execute = AsyncMock(side_effect=[LoginError('slider_required')] * 5 +
            [LoginError('cookies_missing')] + [LoginError('slider_required')] * 5)
        await self.runner.run_day(DAY, shops)
        self.assertEqual(self.runner.execute.await_count, 11)

    async def test_initial_element_timeout_is_retryable(self):
        self.runner.day, self.runner.shop, self.runner.tab = DAY, SHOP, 1
        self.runner.login_state = AsyncMock(side_effect=LoginError("workbench_unreachable"))
        with self.assertRaisesRegex(BusinessError, "page_elements_timeout"):
            await self.runner.enter_workbench()

    async def test_missing_credentials_identifies_missing_field(self):
        self.runner.day, self.runner.shop, self.runner.tab = DAY, ShopCredential("测试店", "", "secret"), 1
        self.runner.login_state = AsyncMock(return_value="login_page")
        with self.assertRaisesRegex(LoginError, "credentials_missing_account$"):
            await self.runner.enter_workbench()

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = StateStore(self.root / "sqlite.db")
        self.store.seed(DAY, [SHOP.shop_name])
        self.services = SimpleNamespace(cs_base="http://fixture", health=AsyncMock())
        self.cfg = Config(root=self.root, page_timeout=.01)
        self.runner = ShopRunner(self.cfg, self.store, self.services, day_clock=lambda: DAY)
        self.runner.rpc = AsyncMock(return_value={"remaining": 0, "local_storage_cleared": True})

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_business_retries_five_times_after_initial_attempt(self):
        self.runner.execute = AsyncMock(side_effect=BusinessError("screenshot_failed"))
        await self.runner.run_shop(DAY, SHOP)
        self.assertEqual(self.runner.execute.await_count, 6)
        row = self.store.get(DAY, SHOP.shop_name)
        self.assertEqual(row["status"], "business_failed")
        self.assertEqual(row["attempts"], 6)
        self.assertTrue(row["cleanup_ok"])
        self.assertFalse(self.store.completed(DAY, SHOP.shop_name))

    async def test_login_error_not_retried_and_completed_today(self):
        self.runner.execute = AsyncMock(side_effect=LoginError("cookies_missing"))
        await self.runner.run_shop(DAY, SHOP)
        self.assertEqual(self.runner.execute.await_count, 1)
        self.assertTrue(self.store.completed(DAY, SHOP.shop_name))
        self.assertEqual(self.store.get(DAY, SHOP.shop_name)["status"], "login_error")

    async def test_verification_is_not_retried_within_round_but_is_processed_on_restart(self):
        self.runner.execute = AsyncMock(side_effect=LoginError("slider_required"))
        await self.runner.run_day(DAY, [SHOP])
        self.assertEqual(self.runner.execute.await_count, 1)
        row = self.store.get(DAY, SHOP.shop_name)
        self.assertEqual(row["status"], "slider_required")
        self.assertTrue(row["cleanup_ok"])
        self.assertFalse(self.store.completed(DAY, SHOP.shop_name))
        self.store.close()
        self.store = StateStore(self.root / "sqlite.db")
        self.runner.store = self.store
        with patch("automation.pdd.screenshot.runner.export_receivables") as export:
            await self.runner.run_day(DAY, [SHOP])
        self.assertEqual(self.runner.execute.await_count, 2)
        self.assertEqual(export.call_args.args[2][0]["exception"], "需要滑块验证")
        self.assertEqual(self.store.get(DAY, SHOP.shop_name)["attempts"], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM daily_shops WHERE day=? AND shop=?",
                                               (DAY, SHOP.shop_name)).fetchone()[0], 1)
        self.runner.execute.side_effect = None
        await self.runner.run_day(DAY, [SHOP])
        self.assertEqual(self.store.get(DAY, SHOP.shop_name)["status"], "success")
        self.assertFalse(self.store.completed("2026-10-30", SHOP.shop_name))

    async def test_retry_can_succeed(self):
        self.runner.execute = AsyncMock(side_effect=[BusinessError("failed"), None])
        await self.runner.run_shop(DAY, SHOP)
        self.assertEqual(self.runner.execute.await_count, 2)
        self.assertEqual(self.store.get(DAY, SHOP.shop_name)["status"], "success")

    async def test_cleanup_four_attempts_then_stops_without_business(self):
        self.runner.rpc.side_effect = BusinessError("cleanup_failed")
        self.runner.execute = AsyncMock()
        with patch("automation.pdd.screenshot.runner.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(CleanupFatal):
                await self.runner.run_shop(DAY, SHOP)
        self.assertEqual(self.runner.rpc.await_count, 4)
        self.runner.execute.assert_not_awaited()
        self.assertFalse(self.store.get(DAY, SHOP.shop_name)["cleanup_ok"])

    async def test_cleanup_retry_success(self):
        self.runner.day = DAY
        self.runner.rpc.side_effect = [BusinessError("failed"), {"remaining": 0, "local_storage_cleared": True}]
        with patch("automation.pdd.screenshot.runner.asyncio.sleep", new=AsyncMock()):
            await self.runner.cleanup()
        self.assertEqual(self.runner.rpc.await_count, 2)

    async def test_cleanup_exhausted_after_success_still_stops(self):
        self.runner.execute = AsyncMock()
        self.runner.rpc.side_effect = [{"remaining": 0, "local_storage_cleared": True}] + [BusinessError("failed")] * 4
        with patch("automation.pdd.screenshot.runner.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(CleanupFatal):
                await self.runner.run_shop(DAY, SHOP)
        self.assertEqual(self.runner.rpc.await_count, 5)
        row = self.store.get(DAY, SHOP.shop_name)
        self.assertEqual(row["status"], "success")
        self.assertFalse(row["cleanup_ok"])

    async def test_process_fault_not_counted_as_login_error(self):
        self.runner.execute = AsyncMock(side_effect=InfrastructureError("cs_unreachable"))
        with self.assertRaises(InfrastructureError):
            await self.runner.run_shop(DAY, SHOP)
        self.assertEqual(self.runner.execute.await_count, 1)
        self.assertEqual(self.store.get(DAY, SHOP.shop_name)["status"], "interrupted")

    async def test_midnight_interrupts_and_cleans_current_shop(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()
        async def execute():
            started.set()
            await asyncio.Future()
        self.runner.execute = execute
        original = self.runner.cleanup
        async def cleanup():
            await original()
            if started.is_set():
                cleaned.set()
        self.runner.cleanup = cleanup
        async def midnight(day):
            await started.wait()
        self.services.monitor = AsyncMock(side_effect=lambda: None)
        async def monitor():
            await asyncio.Future()
        self.services.monitor = monitor
        result = await run_cycle(self.runner, self.services, DAY, [SHOP], date_waiter=midnight)
        self.assertEqual(result, "new_day")
        self.assertTrue(cleaned.is_set())
        self.assertEqual(self.store.get(DAY, SHOP.shop_name)["status"], "interrupted")

    async def test_completed_shops_are_skipped_on_resume(self):
        self.store.finish(DAY, SHOP.shop_name, "login_error")
        self.runner.execute = AsyncMock()
        await self.runner.run_day(DAY, [SHOP])
        self.runner.execute.assert_not_awaited()

    async def test_previous_success_without_receivable_is_processed_after_upgrade(self):
        self.store.finish(DAY, SHOP.shop_name, "success")
        self.runner.execute = AsyncMock()
        await self.runner.run_day(DAY, [SHOP])
        self.runner.execute.assert_awaited_once()

    async def test_success_with_receivable_is_skipped_on_resume(self):
        self.store.finish(DAY, SHOP.shop_name, "success")
        image = self.root / "data" / DAY / "002测试店" / "数据截图.jpg"
        self.store.save_receivable(DAY, SHOP.shop_name, image.resolve(), "0.00")
        self.runner.execute = AsyncMock()
        await self.runner.run_day(DAY, [SHOP])
        self.runner.execute.assert_not_awaited()

    async def test_success_writes_cookie_download_screenshot_and_export(self):
        cookies = [{"domain": ".pinduoduo.com", "name": "a", "value": "fake"}]
        async def rpc(action, params=None):
            return {
                "tab_create": {"tab_id": 12},
                "login_status": {"status": "logged_in"},
                "shop_identity": {"shop_name": SHOP.shop_name},
                "receivable_panel": {"selector": '//*[@id="mms-main"]/*[8]'},
                "receivable_amount": {"amount": "569.05"},
                "screenshot": {"format": "jpeg", "data": base64.b64encode(b"\xff\xd8\xfffixture\xff\xd9").decode()},
                "cookies_export": {"cookies": cookies, "count": 1},
                "automation_state_clear": {"remaining": 0, "local_storage_cleared": True},
            }.get(action, {})
        self.runner.rpc = AsyncMock(side_effect=rpc)
        with patch("automation.pdd.screenshot.runner.download_cookies", return_value=(json.dumps(cookies).encode(), cookies)):
            await self.runner.run_shop(DAY, SHOP)
        folder = self.root / "data" / DAY / "002测试店"
        self.assertEqual(json.loads((folder / "cookies.json").read_text()), cookies)
        self.assertEqual(json.loads((folder / "new.json").read_text()), cookies)
        image = folder / "数据截图.jpg"
        self.assertTrue(image.exists())
        self.assertEqual(self.store.receivables(DAY), [{"shop": SHOP.shop_name, "amount": "569.05",
                                                       "image_path": str(image.resolve())}])
        screenshot_call = next(call for call in self.runner.rpc.call_args_list if call.args[0] == "screenshot")
        self.assertTrue(screenshot_call.args[1]["viewport_to_bottom"])
        self.assertEqual(screenshot_call.args[1]["format"], "jpeg")
        actions = [call.args[0] for call in self.runner.rpc.call_args_list]
        self.assertLess(actions.index("notification_close"), actions.index("shop_identity"))
        self.assertLess(actions.index("screenshot_prepare"), actions.index("screenshot"))
        self.assertLess(actions.index("screenshot"), actions.index("screenshot_restore"))
        self.assertLess(actions.index("receivable_amount"), actions.index("screenshot"))
        self.assertEqual(self.store.get(DAY, SHOP.shop_name)["status"], "success")

    async def test_mismatched_shop_never_screenshots(self):
        self.runner.day, self.runner.shop, self.runner.tab = DAY, SHOP, 1
        self.runner.rpc.return_value = {"shop_name": "另一个店铺"}
        with self.assertRaisesRegex(LoginError, "shop_identity_mismatch"):
            await self.runner.verify_identity()

    async def test_identity_waits_for_delayed_name(self):
        self.runner.day, self.runner.shop, self.runner.tab = DAY, SHOP, 1
        self.runner.rpc.side_effect = [{"shop_name": None}, {"shop_name": SHOP.shop_name}]
        with patch("automation.pdd.screenshot.runner.asyncio.sleep", new=AsyncMock()):
            await self.runner.verify_identity()
        self.assertEqual(self.runner.rpc.await_count, 2)

    async def test_missing_identity_has_explicit_retryable_reason(self):
        self.runner.day, self.runner.shop, self.runner.tab = DAY, SHOP, 1
        self.runner.rpc.return_value = {"shop_name": None}
        with self.assertRaisesRegex(BusinessError, "shop_identity_unconfirmed"):
            await self.runner.verify_identity()

    async def test_identity_timeout_is_capped_at_ten_seconds(self):
        self.runner.day, self.runner.shop, self.runner.tab = DAY, SHOP, 1
        self.runner.config = Config(root=self.root, page_timeout=120)
        self.runner.rpc.return_value = {"shop_name": SHOP.shop_name}
        with patch("automation.pdd.screenshot.runner.asyncio.timeout", wraps=asyncio.timeout) as timeout:
            await self.runner.verify_identity()
        timeout.assert_called_once_with(10)


class DownloadTests(unittest.TestCase):
    def test_missing_cookie_is_login_error_but_500_is_infrastructure(self):
        from urllib.error import HTTPError
        for code, error in ((404, LoginError), (500, InfrastructureError)):
            with patch("automation.pdd.screenshot.runner.urlopen", side_effect=HTTPError("http://fixture", code, "", {}, None)):
                with self.assertRaises(error):
                    download_cookies("http://fixture", SHOP.shop_name)


class RemoteTests(unittest.TestCase):
    def test_stdin_eof_stops_remote_process(self):
        import os
        started = time.monotonic()
        result = supervise([sys.executable, "-c", "import time; time.sleep(60)"],
                           cwd=Path.cwd(), env=dict(os.environ), stream=io.StringIO(""))
        self.assertEqual(result, 0)
        self.assertLess(time.monotonic() - started, 12)

    def test_stop_tree_reaps_owned_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        stop_tree(child)
        self.assertIsNotNone(child.poll())

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_failure_restarts_owned_services_and_resumes(self):
        from automation.pdd.screenshot import run
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = Config(root=root, restart_limit=1)
            store = StateStore(root / "sqlite.db")
            first = SimpleNamespace(start=AsyncMock(side_effect=InfrastructureError("exited")), stop=AsyncMock())
            second = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
            try:
                with patch("automation.pdd.screenshot.runtime.Services", side_effect=[first, second]), patch(
                    "automation.pdd.screenshot.runtime.load_shops", return_value=[]
                ), patch("automation.pdd.screenshot.runtime.run_cycle", new=AsyncMock(return_value="finished")), patch(
                    "automation.pdd.screenshot.runtime.asyncio.sleep", new=AsyncMock()
                ):
                    await run(cfg, store, once=True)
                first.stop.assert_awaited_once()
                second.start.assert_awaited_once()
                second.stop.assert_awaited_once()
            finally:
                store.close()

    async def test_cleanup_failure_never_restarts_services(self):
        from automation.pdd.screenshot import run
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = Config(root=root)
            store = StateStore(root / "sqlite.db")
            service = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
            try:
                with patch("automation.pdd.screenshot.runtime.Services", return_value=service) as factory, patch(
                    "automation.pdd.screenshot.runtime.load_shops", return_value=[]
                ), patch("automation.pdd.screenshot.runtime.run_cycle", new=AsyncMock(side_effect=CleanupFatal("failed"))):
                    with self.assertRaises(CleanupFatal):
                        await run(cfg, store, once=True)
                self.assertEqual(factory.call_count, 1)
                service.stop.assert_awaited_once()
            finally:
                store.close()


class LeaseTests(unittest.TestCase):
    def test_remote_lease_expires_while_stdin_is_open(self):
        import os
        import threading
        unblock = threading.Event()
        class OpenStream:
            def __iter__(self):
                unblock.wait(5)
                return iter(())
        started = time.monotonic()
        try:
            result = supervise([sys.executable, "-c", "import time; time.sleep(60)"],
                               cwd=Path.cwd(), env=dict(os.environ), stream=OpenStream(), lease=.1)
            self.assertEqual(result, 0)
            self.assertLess(time.monotonic() - started, 5)
        finally:
            unblock.set()

class RemoteEntryTests(unittest.TestCase):
    def test_remote_entry_starts_real_cs_and_stop_closes_port(self):
        import socket
        import os
        from automation.pdd.screenshot.services import port_free
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile(mode="w+", encoding="utf-8") as logs:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            child = subprocess.Popen(
                [sys.executable, "-u", "-m", "automation.pdd.screenshot.remote", "--root", folder,
                 "--port", str(port), "--lease", "6"],
                stdin=subprocess.PIPE, stdout=logs, stderr=logs, text=True, encoding="utf-8",
                env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            try:
                from automation.pdd.screenshot.services import http_ready
                deadline = time.monotonic() + 5
                ready = False
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        break
                    try:
                        ready = http_ready(f"http://127.0.0.1:{port}")
                    except Exception:
                        pass
                    if ready:
                        break
                    time.sleep(.05)
                self.assertTrue(ready, "remote CS must become ready on its own test port")
                child.stdin.write('{"type":"ping"}\n{"type":"stop"}\n')
                child.stdin.flush()
                child.wait(timeout=10)
                self.assertEqual(child.returncode, 0)
                self.assertTrue(port_free(port))
            finally:
                if child.stdin:
                    child.stdin.close()
                stop_tree(child)

class SSHTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_heartbeat_forwarding_and_stop_over_ssh(self):
        import asyncssh
        from automation.pdd.screenshot.services import Services, http_ready
        seen = []
        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return False
            def connection_requested(self, *args):
                return True
        async def remote_process(process):
            process.stdout.write("remote fixture log\n")
            async for line in process.stdin:
                message = json.loads(line)["type"]
                seen.append(message)
                if message == "stop":
                    break
            process.exit(0)
        async def http(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = b'{"status":"ok"}'
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        http_server = await asyncio.start_server(http, "127.0.0.1", 0)
        ssh_server = await asyncssh.create_server(Server, "127.0.0.1", 0,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")], process_factory=remote_process)
        original_connect = asyncssh.connect
        def test_connect(*args, **kwargs):
            # 仅测试进程内临时主机密钥；生产代码必须继续使用默认 known_hosts。
            return original_connect(*args, known_hosts=None, **kwargs)
        cfg = Config(root=Path.cwd(), ssh_host="127.0.0.1", ssh_port=ssh_server.get_port(),
                     ssh_command="fixture", ssh_cs_port=http_server.sockets[0].getsockname()[1])
        service = Services(cfg)
        try:
            with patch("asyncssh.connect", side_effect=test_connect):
                await asyncio.wait_for(service._remote_start(), 5)
            self.assertTrue(await asyncio.to_thread(http_ready, service.cs_base))
            await asyncio.wait_for(service.stop(), 25)
            self.assertIn("ping", seen)
            self.assertIn("stop", seen)
        finally:
            await service.stop()
            ssh_server.close()
            await ssh_server.wait_closed()
            http_server.close()
            await http_server.wait_closed()
