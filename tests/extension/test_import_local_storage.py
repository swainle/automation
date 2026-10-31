"""单点验证 browser 的 local_storage import 业务。"""
import argparse
from pathlib import Path
import unittest

from automation.pdd.storage.local_storage import import_local_storage

ROOT = Path(__file__).resolve().parents[2]
OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动操作真实商家后台 local_storage")
class LocalStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_local_storage(self):
        try:
            await self.run_operation()
        except AssertionError:
            raise
        except Exception as exc:
            self.fail(f"local_storage 操作失败（{type(exc).__name__}），请检查文件和插件连接；若已开始写入，请导出检查。")

    async def run_operation(self):
        result = await import_local_storage(OPTIONS.input, port=OPTIONS.port, tab_id=OPTIONS.tab_id)
        print(f"已清除 {result['cleared']} 项，导入 {result['imported']} 项 local_storage；页面已保留，请按需手动刷新。")
        self.assertEqual(result["remaining"], result["imported"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tab-id", type=int)
    parser.add_argument("--input", type=Path, default=ROOT / "一往无前宠物用品店cookie.json")
    OPTIONS = parser.parse_args()
    if not 1024 <= OPTIONS.port <= 65535:
        parser.error("端口无效")
    unittest.main(argv=[__file__], verbosity=2)
