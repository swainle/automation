"""单独测试启动本机 Chrome，默认不加任何浏览器设置。"""

import unittest
from unittest.mock import patch

from automation.com.browser import launch_chrome, main


class LaunchArgumentsTests(unittest.TestCase):
    def test_default_launch_has_no_browser_flags(self):
        with patch("automation.com.browser.chrome_executable", return_value="chrome.exe"), patch("automation.com.browser.subprocess.Popen") as process:
            launch_chrome()
            self.assertEqual(process.call_args.args[0], ["chrome.exe"])

    def test_debugging_requires_explicit_profile(self):
        with patch("automation.com.browser.chrome_executable", return_value="chrome.exe"), patch("automation.com.browser.subprocess.Popen") as process:
            with self.assertRaises(ValueError):
                launch_chrome(debug_port=9222)
            process.assert_not_called()


if __name__ == "__main__":
    # 直接运行是手动启动测试；discover 只验证参数，不打开真实浏览器。
    raise SystemExit(main())
