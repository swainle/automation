"""只通过插件截图，不单独执行元素查询、不启动浏览器。"""

import argparse
from pathlib import Path
import unittest

from PIL import Image
from automation.com.extension.client import output_folder, select_tab, save_png
from automation.com.extension_bridge import request

OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动连接真实插件")
class ScreenshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_screenshot(self):
        path = OPTIONS.output or output_folder("screenshot") / "screenshot.png"
        try:
            endpoint = f"ws://127.0.0.1:{OPTIONS.port}/"
            tab_id = await select_tab(endpoint, OPTIONS.tab_id)
            result = await request(endpoint, "screenshot", {"tab_id": tab_id, "selector": OPTIONS.selector,
                                   "selector_type": OPTIONS.selector_type, "full_page": OPTIONS.full_page})
            saved = save_png(result, path)
            with Image.open(saved) as image:
                image.load()
                self.assertTrue(image.width > 0 and image.height > 0, "截图为空")
                print(f"\n截图成功：{image.width}×{image.height}；{saved}")
        except AssertionError:
            raise
        except Exception as exc:
            self.fail(f"截图失败（{type(exc).__name__}），请检查 WS、插件、唯一可见元素及 debugger 是否被占用")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tab-id", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--selector", help="只截取唯一可见元素")
    mode.add_argument("--full-page", action="store_true")
    parser.add_argument("--selector-type", choices=("css", "xpath"), default="css")
    parser.add_argument("--output", type=Path)
    OPTIONS = parser.parse_args()
    if not 1024 <= OPTIONS.port <= 65535:
        parser.error("端口无效")
    unittest.main(argv=[__file__], verbosity=2)
