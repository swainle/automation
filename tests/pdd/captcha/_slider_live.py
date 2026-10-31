"""手动运行：默认选择综合分较低的候选拖动一次，--dry-run 只识别。"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time
import unittest
from unittest.mock import Mock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
OPTIONS = argparse.Namespace(port=9222, offset=0.0, timeout=8.0, dry_run=False)


def drag_distance(target_center, piece_width, background_width, rendered_width, current_offset=0):
    numbers = (target_center, piece_width, background_width, rendered_width, current_offset)
    if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in numbers):
        raise ValueError("图片尺寸和识别坐标必须为有限数值")
    if min(piece_width, background_width, rendered_width) <= 0:
        raise ValueError("图片尺寸必须大于零")
    return (target_center - piece_width / 2) * rendered_width / background_width - current_offset


def drag_once(page, knob_box, distance):
    x = knob_box["x"] + knob_box["width"] / 2
    y = knob_box["y"] + knob_box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    try:
        page.mouse.move(x + distance, y, steps=35)
    finally:
        page.mouse.up()


class SliderGeometryTests(unittest.TestCase):
    def test_center_coordinate_is_converted_to_css_distance(self):
        self.assertAlmostEqual(drag_distance(197, 60, 320, 272), 141.95)

    def test_existing_piece_offset_is_subtracted(self):
        self.assertAlmostEqual(drag_distance(197, 60, 320, 272, 20), 121.95)

    def test_invalid_dimensions_are_rejected(self):
        for width in (0, -1, float("nan")):
            with self.assertRaises(ValueError):
                drag_distance(197, 60, width, 272)

    def test_drag_releases_mouse_when_movement_fails(self):
        page = Mock()
        page.mouse.move.side_effect = [None, RuntimeError("movement failed")]
        with self.assertRaises(RuntimeError):
            drag_once(page, {"x": 0, "y": 0, "width": 50, "height": 50}, 100)
        page.mouse.down.assert_called_once()
        page.mouse.up.assert_called_once()


@unittest.skipUnless(__name__ == "__main__", "仅直接运行此文件时才操作真实验证码")
class SliderCaptchaLiveTests(unittest.TestCase):
    def test_current_slider_candidates(self):
        from automation.pdd.captcha.candidates import analyze_candidates
        from playwright.sync_api import sync_playwright

        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d_%H-%M-%S_%f")
        folder = ROOT / "data" / "captcha_tests" / stamp
        folder.mkdir(parents=True)
        report = {"status": "未执行", "attempts": 0}
        page = None
        print(f"\n测试记录目录：{folder}", flush=True)
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{OPTIONS.port}", timeout=5000)
                    candidates = []
                    for context in browser.contexts:
                        for candidate in context.pages:
                            if urlparse(candidate.url).hostname != "mms.pinduoduo.com":
                                continue
                            if candidate.locator(".captcha-wrapper:visible img.slider-img-bg").count() == 1:
                                candidates.append(candidate)
                    if len(candidates) != 1:
                        raise ValueError(f"找到 {len(candidates)} 个带滑块的拼多多页面，需要恰好一个")
                    page = candidates[0]
                    page.screenshot(path=str(folder / "before.png"))
                    wrapper = page.locator(".captcha-wrapper:visible").filter(has=page.locator("img.slider-img-bg"))
                    bg = wrapper.locator("img.slider-img-bg")
                    piece = wrapper.locator("img.slider-item")
                    bg_src, piece_src = bg.get_attribute("src"), piece.get_attribute("src")
                    if not all(src and src.startswith("data:image/") and ";base64," in src for src in (bg_src, piece_src)):
                        raise ValueError("当前验证码图片不是支持的 Base64 图片格式")
                    bg_bytes = base64.b64decode(bg_src.split(",", 1)[1], validate=True)
                    piece_bytes = base64.b64decode(piece_src.split(",", 1)[1], validate=True)
                    (folder / "background.png").write_bytes(bg_bytes)
                    (folder / "piece.png").write_bytes(piece_bytes)
                    analysis = analyze_candidates(bg_bytes, piece_bytes, folder / "candidates.png")
                    bg_box = bg.bounding_box()
                    if not bg_box:
                        raise ValueError("背景图不可见")
                    for item in analysis["candidates"]:
                        item["css_distance"] = round(item["natural_distance"] * bg_box["width"] / analysis["background_width"], 2)
                        print(f"候选 #{item['rank']}：综合={item['score']:.3f}，轮廓={item['shape_score']:.3f}，纹理={item['texture_score']:.3f}，距离={item['css_distance']:.2f} 像素", flush=True)
                    report.update(analysis)
                    report["status"] = "仅识别候选，未拖动"
                    if analysis["recommended_rank"]:
                        print(f"按综合分较低优先规则选择：候选 #{analysis['recommended_rank']}；不代表通过概率更高。", flush=True)
                    else:
                        print("候选分数相同或不足两个，暂不按低分规则选择，请查看对比图。", flush=True)
                    print(f"候选对比图：{folder / 'candidates.png'}", flush=True)
                    if OPTIONS.dry_run:
                        print("dry-run：仅识别，未拖动。", flush=True)
                        return
                    if analysis["recommended_rank"] is None:
                        report["status"] = "没有唯一低分候选，未拖动"
                        self.fail(report["status"])
                    chosen = next(item for item in analysis["candidates"] if item["rank"] == analysis["recommended_rank"])
                    # 核对图片未刷新后，再读取当前滑块和轨道位置。
                    if bg.get_attribute("src") != bg_src or piece.get_attribute("src") != piece_src:
                        raise ValueError("验证码已刷新，本次未拖动，请重新运行")
                    piece_box = piece.bounding_box()
                    bg_box = bg.bounding_box()
                    knob_box = wrapper.locator(".slide-button").bounding_box()
                    rail = wrapper.locator(".slide-handlebar").bounding_box()
                    if not all((piece_box, bg_box, knob_box, rail)):
                        raise ValueError("滑块按钮或轨道不可见，本次未拖动")
                    distance = (chosen["natural_distance"] * bg_box["width"] / analysis["background_width"]
                                - (piece_box["x"] - bg_box["x"]))
                    remaining = rail["x"] + rail["width"] - knob_box["x"] - knob_box["width"]
                    if not math.isfinite(distance) or not 0 < distance <= remaining:
                        raise ValueError("候选距离超出滑块可移动范围，本次未拖动")
                    report.update(attempts=1, distance=round(distance, 2), status="正在拖动")
                    print(f"拖动候选 #{chosen['rank']}：{distance:.2f} 像素，仅尝试一次。", flush=True)
                    drag_once(page, knob_box, distance)
                    report["status"] = "结果未确认，请人工查看页面"
                    deadline = time.monotonic() + OPTIONS.timeout
                    page.wait_for_timeout(800)
                    while time.monotonic() < deadline:
                        visible = wrapper.count() == 1 and wrapper.is_visible()
                        text = wrapper.inner_text() if visible else ""
                        if "验证失败" in text or "请重试" in text:
                            report["status"] = "验证失败"
                            break
                        if "验证成功" in text or "验证通过" in text:
                            report["status"] = "验证通过"
                            break
                        if not visible and urlparse(page.url).path.startswith("/home"):
                            body = page.locator("body").inner_text()
                            if "商品管理" in body and "订单查询" in body:
                                report["status"] = "已进入工作台"
                                break
                        page.wait_for_timeout(200)
                    print(report["status"] + "；不自动重试，浏览器保持打开。", flush=True)
                    if report["status"] not in {"验证通过", "已进入工作台"}:
                        self.fail(report["status"])
                except AssertionError:
                    raise
                except Exception as exc:
                    # 不输出第三方异常原文，避免其中包含页面数据或凭据。
                    report["status"] = str(exc) if type(exc) is ValueError else f"执行异常（{type(exc).__name__}），请检查浏览器连接及页面状态"
                    self.fail(report["status"])
                finally:
                    if page is not None and not page.is_closed():
                        try:
                            page.screenshot(path=str(folder / "after.png"), timeout=5000)
                        except Exception:
                            pass
                    # 不调用 browser.close/context.close，退出仅断开本次 CDP 连接。
        finally:
            (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--dry-run", action="store_true", help="只识别并保存候选图，不拖动")
    parser.add_argument("--timeout", type=float, default=8, help="拖动后等待验证结果的秒数，默认 8")
    OPTIONS = parser.parse_args()
    if not 1 <= OPTIONS.port <= 65535 or not math.isfinite(OPTIONS.timeout) or OPTIONS.timeout <= 0:
        parser.error("端口或超时无效")
    unittest.main(argv=[__file__], verbosity=2)
