"""只获取元素，不截图、不启动浏览器。"""

import argparse
import json
from pathlib import Path
import unittest

from automation.com.extension.client import output_folder, select_tab
from automation.com.extension_bridge import request

OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动连接真实插件")
class GetElementTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_element(self):
        folder = OPTIONS.output_dir or output_folder("element")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            endpoint = f"ws://127.0.0.1:{OPTIONS.port}/"
            tab_id = await select_tab(endpoint, OPTIONS.tab_id)
            result = await request(endpoint, "element", {"tab_id": tab_id, "selector": OPTIONS.selector,
                                   "selector_type": OPTIONS.selector_type, "include_text": OPTIONS.text})
            path = folder / "elements.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            self.assertTrue(result.get("returned", 0) > 0, "未匹配到元素，查询结果已保存")
            print(f"\n匹配 {result['returned']} 个元素；结果：{path.resolve()}")
        except AssertionError:
            raise
        except Exception as exc:
            self.fail(f"元素查询失败（{type(exc).__name__}），请检查本机 WS、插件连接和标签页选择")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tab-id", type=int)
    parser.add_argument("--selector", default="body")
    parser.add_argument("--selector-type", choices=("css", "xpath"), default="css")
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    OPTIONS = parser.parse_args()
    if not 1024 <= OPTIONS.port <= 65535:
        parser.error("端口无效")
    unittest.main(argv=[__file__], verbosity=2)
