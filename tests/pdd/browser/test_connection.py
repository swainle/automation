import unittest
from unittest.mock import Mock

from automation.com.browser.connection import connect_context, export_cookies, import_cookies


class BrowserConnectionTests(unittest.TestCase):
    def test_connects_without_launching_or_closing_browser(self):
        p = Mock()
        context = Mock()
        p.chromium.connect_over_cdp.return_value.contexts = [context]
        self.assertIs(connect_context(p), context)
        p.chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:9222", timeout=5000)
        context.close.assert_not_called()
        p.chromium.launch.assert_not_called()

    def test_export_is_limited_to_pinduoduo(self):
        context = Mock()
        context.cookies.return_value = [{"domain": ".pinduoduo.com"}, {"domain": "mms.pinduoduo.com"}, {"domain": "example.com"}]
        self.assertEqual(len(export_cookies(context)), 2)

    def test_import_clears_only_pinduoduo_cookies(self):
        context = Mock()
        import_cookies(context, [{"name": "test"}])
        pattern = context.clear_cookies.call_args.kwargs["domain"]
        self.assertTrue(pattern.search(".pinduoduo.com"))
        self.assertFalse(pattern.search("evilpinduoduo.com"))
        context.add_cookies.assert_called_once_with([{"name": "test"}])
