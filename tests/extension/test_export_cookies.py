"""单点验证 browser 的 export 业务，同时处理 cookies 和 local_storage。"""
import argparse
from pathlib import Path
import unittest

from automation.pdd.storage.state import export_state, print_state_report
from automation.com.extension_bridge import BridgeError

ROOT = Path(__file__).resolve().parents[2]
OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动操作真实浏览器两类存储")
class CookiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_cookies(self):
        try:
            await self.run_operation()
        except AssertionError:
            raise
        except BridgeError as exc:
            self.fail(f"插件任务失败：{exc}。请检查 WS、插件版本和 tab-id；若已开始修改，请导出检查。")
        except Exception as exc:
            self.fail(f"操作失败（{type(exc).__name__}），请检查输入文件、输出路径和连接。")

    async def run_operation(self):
        result = await export_state(OPTIONS.output, port=OPTIONS.port, tab_id=OPTIONS.tab_id, report=print_state_report)
        self.assertTrue(result["output"].is_file())
        print(f"两类数据已导出：{result['output']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tab-id", type=int)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "export" / "cookies.json")
    OPTIONS = parser.parse_args()
    if not 1024 <= OPTIONS.port <= 65535:
        parser.error("端口无效")
    unittest.main(argv=[__file__], verbosity=2)
