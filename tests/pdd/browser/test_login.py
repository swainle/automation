"""独立账号登录：从飞书按 Cookie 文件名匹配账号，经 WS/插件提交一次。"""

import argparse
import asyncio
import json
import math
from pathlib import Path
import unittest

from automation.com.extension.client import output_folder
from automation.pdd.login import select_login_tab, submit_login
from automation.pdd.screenshot.state import VERIFICATIONS, VERIFICATION_LABELS
from automation.com.extension_bridge import BridgeError, request
from automation.pdd.credentials import CredentialError, FeishuCredentials, cookie_shop_name

ROOT = Path(__file__).resolve().parents[3]
OPTIONS = None
MESSAGES = {
    **VERIFICATION_LABELS,
    "logged_in": "已确认进入商家工作台",
    "verification_required": "出现验证码或身份验证，请手动处理；本测试不尝试过验证",
    "login_rejected": "页面提示登录失败或账号密码错误",
    "result_unknown": "登录结果未确认，请查看浏览器；未重复提交",
    "preview_ready": "登录页检查通过，未读取飞书凭据、未切换、未填写、未点击",
}


@unittest.skipUnless(__name__ == "__main__", "手动登录真实拼多多店铺")
class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_login(self):
        folder = OPTIONS.output_dir or output_folder("login")
        folder.mkdir(parents=True, exist_ok=True)
        report = {"status": "not_started", "submitted": False, "cookies_imported": False}
        try:
            endpoint = f"ws://127.0.0.1:{OPTIONS.port}/"
            tab_id = await select_login_tab(endpoint, OPTIONS.tab_id)
            report["tab_id"] = tab_id
            preview = await request(endpoint, "login_probe", {"tab_id": tab_id})
            if preview.get("status") in {"logged_in"} | VERIFICATIONS:
                report["status"] = preview["status"]
            else:
                self.assertEqual(preview.get("status"), "login_page", "请手动打开拼多多登录页")
                ready = preview.get("mode") == "account" or preview.get("account_tabs") == 1
                self.assertTrue(ready, "未能唯一确认账号登录入口或账号密码表单")
                if OPTIONS.dry_run:
                    report["status"] = "preview_ready"
                else:
                    name = cookie_shop_name(OPTIONS.cookies)
                    print(f"\n正在从飞书匹配店铺：{name}（不导入 Cookie）", flush=True)
                    matches = await asyncio.to_thread(FeishuCredentials(ROOT).match, [OPTIONS.cookies])
                    self.assertTrue(len(matches) == 1 and matches[0].credential is not None,
                                    matches[0].status if matches else "未找到店铺账号")
                    report["shop_name"] = name
                    report.update(await submit_login(endpoint, tab_id, matches[0].credential, OPTIONS.timeout))
            print("\n" + MESSAGES.get(report["status"], "登录未完成"), flush=True)
            self.assertIn(report["status"], {"logged_in", "preview_ready"}, "登录尚未完成，详见上方状态；浏览器保持打开")
        except AssertionError:
            if report["status"] == "not_started":
                report["status"] = "precondition_failed"
            raise
        except (CredentialError, BridgeError) as exc:
            report["status"] = "configuration_or_operation_failed"
            self.fail(f"登录测试失败：{exc}。请确认 WS 已重启、插件已重新加载、飞书配置正确")
        except Exception as exc:
            report["status"] = "execution_error"
            self.fail(f"登录执行异常（{type(exc).__name__}），不输出原始响应或凭据")
        finally:
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"结果：{folder.resolve()}；账号密码不输出、不落盘。", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="本机 WS 端口")
    parser.add_argument("--tab-id", type=int)
    parser.add_argument("--cookies", type=Path, default=ROOT / "一往无前宠物用品店cookie.json",
                        help="只使用文件名匹配店铺，不读取/导入 Cookie 内容")
    parser.add_argument("--timeout", type=float, default=15, help="提交后等待结果的秒数")
    parser.add_argument("--dry-run", action="store_true", help="仅检查页面，不读取凭据或执行登录")
    parser.add_argument("--output-dir", type=Path)
    OPTIONS = parser.parse_args()
    if not 1024 <= OPTIONS.port <= 65535 or not math.isfinite(OPTIONS.timeout) or OPTIONS.timeout <= 0:
        parser.error("端口或超时无效")
    unittest.main(argv=[__file__], verbosity=2)
