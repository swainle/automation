import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from automation.pdd.spider.runtime import Runtime


class RuntimeTests(unittest.TestCase):
    def test_windows_usbmux_refusal_reports_apple_service_recovery(self):
        from automation.pdd.spider import ios
        for field in ('stdout', 'stderr'):
            result = Mock(is_success=False, code=1, stdout='', stderr='')
            setattr(result, field, 'dial tcp 127.0.0.1:27015: connectex: actively refused it')
            with self.subTest(field=field), patch.object(ios, 'IOS_EXE') as executable, \
                    patch('processkit.Command') as command, \
                    patch('automation.pdd.spider.runtime.sys.platform', 'win32'):
                executable.is_file.return_value = True
                command.return_value.timeout.return_value.cwd.return_value.output.return_value = result
                with self.assertRaisesRegex(RuntimeError, 'services.msc.*Apple Mobile Device Service'):
                    Runtime()._ios_command('list')

    def test_other_ios_failures_preserve_original_error(self):
        from automation.pdd.spider import ios
        with patch.object(ios, 'IOS_EXE'), patch('processkit.Command') as command:
            command.return_value.timeout.return_value.cwd.return_value.output.return_value = Mock(
                is_success=False, code=2, stdout='', stderr='pairing failed')
            with self.assertRaisesRegex(RuntimeError, '退出码 2: pairing failed'):
                Runtime()._ios_command('list')

    def test_foreign_debug_port_is_not_reused_or_terminated(self):
        import psutil
        from automation.pdd.spider.runtime import verify_debug_browser
        from types import SimpleNamespace
        connection = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=9222), pid=999)
        with patch('psutil.net_connections', return_value=[connection]), patch('psutil.Process') as process:
            process.return_value.exe.return_value = 'C:/OtherBrowser/chrome.exe'
            with self.assertRaisesRegex(RuntimeError, '其他程序'):
                verify_debug_browser(9222, 'C:/Sogou/SogouExplorer.exe')
            process.return_value.terminate.assert_not_called()

    def test_internal_mitm_uses_installed_module_entry(self):
        runtime = Runtime(proxy='http://127.0.0.1:1080')
        with patch('automation.pdd.spider.runtime.port_ready', side_effect=[False, True]), \
                patch.object(runtime.processes, 'start') as start:
            runtime.start_mitm()
        command = start.call_args.args[1]
        self.assertEqual(command[1:4], ['-m', 'automation.pdd.spider.mitm', '8080'])
        self.assertIn('--upstream', command)

    def test_ready_wda_is_reused_without_go_ios_or_service_startup(self):
        runtime = Runtime()
        with patch("automation.pdd.spider.ios.wda_ready", return_value=True), \
                patch("automation.pdd.spider.ios.ensure_wda") as start, \
                patch("wda.Client") as client, patch("automation.pdd.spider.ios.stop_wda"):
            self.assertIs(runtime.ensure_wda(), client.return_value)
            self.assertIs(runtime.ensure_wda(), client.return_value)
            runtime.close()
        start.assert_not_called()
        client.assert_called_once()

    def test_reused_browser_is_not_terminated(self):
        runtime = Runtime()
        playwright = Mock()
        browser = Mock()
        browser.contexts = [Mock(pages=[Mock()])]
        playwright.chromium.connect_over_cdp.return_value = browser
        with patch("automation.pdd.spider.sogou.cdp_ready", return_value=True), \
                patch('automation.pdd.spider.runtime.verify_debug_browser'), \
                patch("automation.pdd.spider.sogou.set_dpi_aware"), \
                patch("playwright.sync_api.sync_playwright") as factory, \
                patch.object(runtime.processes, "start") as start, \
                patch("psutil.process_iter") as processes:
            factory.return_value.start.return_value = playwright
            runtime.open_browser()
            runtime.close()
        start.assert_not_called()
        processes.assert_not_called()
        browser.close.assert_not_called()
        playwright.stop.assert_called_once()

    def test_browser_restart_targets_only_configured_executable_and_keeps_profile(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "browser.exe"
            executable.touch()
            matching = Mock(pid=101, info={"exe": str(executable)})
            matching.children.return_value = []
            unrelated = Mock(pid=102, info={"exe": str(Path(directory) / "other.exe")})
            playwright = Mock()
            playwright.chromium.connect_over_cdp.return_value.contexts = [Mock(pages=[Mock()])]
            runtime = Runtime()
            with patch("automation.pdd.spider.config.SOGOU_EXECUTABLE", str(executable)), \
                    patch('automation.pdd.spider.runtime.verify_debug_browser'), \
                    patch("automation.pdd.spider.config.USER_DATA_DIR", "existing-profile"), \
                    patch("automation.pdd.spider.sogou.cdp_ready", side_effect=[False, True]), \
                    patch("automation.pdd.spider.sogou.set_dpi_aware"), \
                    patch("playwright.sync_api.sync_playwright") as factory, \
                    patch("psutil.process_iter", return_value=[matching, unrelated]), \
                    patch("psutil.wait_procs", return_value=([], [])), \
                    patch.object(runtime.processes, "start") as start:
                factory.return_value.start.return_value = playwright
                runtime.open_browser()
                runtime.close()
            matching.terminate.assert_called_once()
            unrelated.terminate.assert_not_called()
            self.assertIn("--user-data-dir=existing-profile", start.call_args.args[1])

    def test_reused_mitm_updates_explicit_proxy_without_owning_process(self):
        runtime = Runtime(proxy="https://user:a%3Ab@127.0.0.1:1080")
        with patch("automation.pdd.spider.runtime.port_ready", return_value=True), \
                patch("automation.pdd.spider.ios._update_proxy_config") as update, \
                patch("automation.pdd.spider.ios._write_mitm_config") as scheme, \
                patch.object(runtime.processes, "start") as start:
            runtime.start_mitm()
            runtime.stop_mitm()
            runtime.close()
        update.assert_called_once_with("127.0.0.1:1080", "user:a:b")
        scheme.assert_called_once_with({"upstream_scheme": "https"})
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
