"""只尝试当前滑块一次；仍使用旧 Playwright 诊断流程，不是插件拖动。"""

if __name__ == "__main__":
    import argparse
    import unittest
    import math
    import _slider_live as live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=8)
    options = parser.parse_args()
    if not 1024 <= options.port <= 65535 or not math.isfinite(options.timeout) or options.timeout <= 0:
        parser.error("端口或超时无效")
    live.OPTIONS = argparse.Namespace(port=options.port, timeout=options.timeout, dry_run=False)
    live.SliderCaptchaLiveTests.__unittest_skip__ = False
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        live.SliderCaptchaLiveTests("test_current_slider_candidates")
    ]))
    raise SystemExit(0 if result.wasSuccessful() else 1)
