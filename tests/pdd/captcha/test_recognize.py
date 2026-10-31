"""只识别当前验证码，不拖动。旧诊断流程仍通过 CDP 读取页面。"""

if __name__ == "__main__":
    import argparse
    import unittest
    import _slider_live as live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    options = parser.parse_args()
    if not 1024 <= options.port <= 65535:
        parser.error("端口无效")
    live.OPTIONS = argparse.Namespace(port=options.port, timeout=8, dry_run=True)
    live.SliderCaptchaLiveTests.__unittest_skip__ = False
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        live.SliderCaptchaLiveTests("test_current_slider_candidates")
    ]))
    raise SystemExit(0 if result.wasSuccessful() else 1)
