"""传入滑块按钮 class 或 XPath，通过插件检测关联元素；不拖动。"""

import argparse
from pathlib import Path
import unittest

from automation.pdd.captcha.detection import save_detection
from automation.com.extension.client import output_folder, select_tab
from automation.com.extension_bridge import BridgeError, request

OPTIONS = None


@unittest.skipUnless(__name__ == "__main__", "手动检测真实页面，不执行滑块操作")
class DetectCaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_detect(self):
        folder = OPTIONS.output_dir or output_folder("captcha-detect")
        try:
            endpoint = f"ws://127.0.0.1:{OPTIONS.port}/"
            params = {"tab_id": await select_tab(endpoint, OPTIONS.tab_id)}
            if OPTIONS.button_class is not None:
                params["button_class"] = OPTIONS.button_class
            else:
                params["button_xpath"] = OPTIONS.button_xpath
            result = await request(endpoint, "captcha_detect", params)
            report = save_detection(result, folder)
            print(f"\n检测状态：{report['status']}；可见按钮数：{report['visible_button_matches']}", flush=True)
            print(f"背景候选：{len(report['backgrounds'])}；拼图候选：{len(report['pieces'])}；轨道候选：{len(report['tracks'])}", flush=True)
            for warning in report.get("warnings", []):
                print("提示：" + warning, flush=True)
            print(f"诊断目录：{folder.resolve()}；未拖动、未刷新页面。", flush=True)
            self.assertTrue("screenshot" in result, "检测结果已保存，但诊断截图失败，请检查 debugger 是否被占用")
            self.assertEqual(report["status"], "detected", "无法唯一确认完整组件，候选信息及截图已保存，请人工确认")
        except AssertionError:
            raise
        except BridgeError as exc:
            self.fail(f"插件检测失败：{exc}。请重新加载插件并重启新版 WS 服务")
        except Exception as exc:
            self.fail(f"检测失败（{type(exc).__name__}），请检查 WS、插件和目标标签页；未输出页面原始数据")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    locator = parser.add_mutually_exclusive_group(required=True)
    locator.add_argument("--button-class", help="class 名称，可带前导点；多个 class 用空格分隔")
    locator.add_argument("--button-xpath", help="滑块按钮 XPath")
    parser.add_argument("--port", type=int, default=8765, help="本机 WS 端口")
    parser.add_argument("--tab-id", type=int)
    parser.add_argument("--output-dir", type=Path)
    OPTIONS = parser.parse_args()
    if not 1024 <= OPTIONS.port <= 65535:
        parser.error("端口无效")
    if not (OPTIONS.button_class or OPTIONS.button_xpath or "").strip():
        parser.error("按钮定位参数不能为空")
    unittest.main(argv=[__file__], verbosity=2)
