"""只测试获取飞书工作表列表。"""

if __name__ == "__main__":
    import unittest
    from _sheets_live import FeishuSheetsLiveTests
    FeishuSheetsLiveTests.__unittest_skip__ = False
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        FeishuSheetsLiveTests("test_list_worksheets_v3")
    ]))
    raise SystemExit(0 if result.wasSuccessful() else 1)
