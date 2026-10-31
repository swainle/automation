"""只测试读取飞书电子表格区域，配置取自根目录 .env。"""

if __name__ == "__main__":
    import unittest
    from _sheets_live import FeishuSheetsLiveTests
    FeishuSheetsLiveTests.__unittest_skip__ = False
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        FeishuSheetsLiveTests("test_read_cells_v2")
    ]))
    raise SystemExit(0 if result.wasSuccessful() else 1)
