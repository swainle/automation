"""只检查本机 WS 和插件连接状态。"""

import argparse
import unittest

from automation.com.extension_bridge import request

PORT = 8765


@unittest.skipUnless(__name__ == "__main__", "手动检查真实 WS 服务")
class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection(self):
        try:
            result = await request(f"ws://127.0.0.1:{PORT}/", "status")
        except Exception as exc:
            self.fail(f"WS 连接失败（{type(exc).__name__}），请先启动 extension_bridge")
        self.assertTrue(result.get("connected"), "WS 已启动，但插件尚未连接")
        print("\n本机 WS 和 Chrome 插件连接正常。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    PORT = parser.parse_args().port
    if not 1024 <= PORT <= 65535:
        parser.error("端口无效")
    unittest.main(argv=[__file__], verbosity=2)
