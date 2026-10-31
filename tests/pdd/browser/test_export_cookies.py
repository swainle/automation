"""只导出当前调试浏览器中的拼多多 Cookie。"""

import argparse
import json
from pathlib import Path
import unittest

from automation.com.browser.connection import connect_context, export_cookies

ROOT = Path(__file__).resolve().parents[3]
OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动读取调试浏览器 Cookie")
class ExportCookiesTests(unittest.TestCase):
    def test_export_cookies(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            cookies = export_cookies(connect_context(p, OPTIONS.port))
        self.assertTrue(cookies, "当前浏览器没有拼多多 Cookie，未写出文件")
        OPTIONS.output.parent.mkdir(parents=True, exist_ok=True)
        OPTIONS.output.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已导出 {len(cookies)} 条 Cookie：{OPTIONS.output.resolve()}，未输出 Cookie 内容。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "export" / "cookies.json")
    OPTIONS = parser.parse_args()
    unittest.main(argv=[__file__], verbosity=2)
