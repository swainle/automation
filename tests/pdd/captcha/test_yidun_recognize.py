"""网易易盾演示页单点测试：recognize；复用已有浏览器，不自动重试。"""
import argparse
from pathlib import Path
import unittest

from automation.pdd.captcha.browser import recognize_yidun
from automation.com.extension_bridge import BridgeError

OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动操作网易易盾演示页")
class YidunTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognize(self):
        try:
            selectors = {key: getattr(OPTIONS, key) for key in ("button_selector", "background_selector", "piece_selector", "track_selector") if getattr(OPTIONS, key)}
            result = await recognize_yidun(port=OPTIONS.port, tab_id=OPTIONS.tab_id, threshold=OPTIONS.threshold,
                timeout=OPTIONS.timeout, output_dir=OPTIONS.output_dir, selectors=selectors)
            print(f"\n状态：{result['status']}；尝试次数：{result['attempts']}")
            if "analysis" in result:
                a = result["analysis"]
                print(f"缺口 x={a['gap_x']}，拖动距离={a['distance']} CSS px，边界计数={a['edge_score']}，次选={a['runner_score']}")
            print(f"诊断目录：{result['output_dir']}；不自动重试。")
            self.assertEqual(result["status"], "recognized", "结果未确认或识别有歧义，请检查诊断图")
        except BridgeError as exc:
            self.fail(f"插件任务失败：{exc}；请检查插件、WS 版本及目标页。拖动结果不确定时请人工检查。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tab-id", type=int)
    parser.add_argument("--threshold", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--output-dir", type=Path)
    for name in ("button", "background", "piece", "track"):
        parser.add_argument(f"--{name}-selector")
    OPTIONS = parser.parse_args()
    if not 1024 <= OPTIONS.port <= 65535 or not 1 <= OPTIONS.threshold <= 254 or not 0 < OPTIONS.timeout <= 15:
        parser.error("端口、阈值或超时参数无效")
    unittest.main(argv=[__file__], verbosity=2)
