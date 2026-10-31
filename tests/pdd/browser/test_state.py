import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from automation.pdd.storage.state import import_state, clear_state, print_state_report, summary
from automation.com.extension_bridge import BridgeError

COOKIE = {"domain": ".pinduoduo.com", "name": "fixture", "path": "/", "value": "secret-cookie"}


class BrowserStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_validation_failure_never_opens_or_clears(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({"url": "https://mms.pinduoduo.com/home", "cookies": [COOKIE], "local_storage": {}}), encoding="utf-8")
            with patch("automation.pdd.storage.state.request", new_callable=AsyncMock,
                       side_effect=BridgeError("invalid_cookie_entry")) as request:
                with self.assertRaises(BridgeError):
                    await import_state(path)
                self.assertEqual(request.await_count, 1)
                self.assertEqual(request.await_args.args[1], "cookies_validate")

    async def test_invalid_local_storage_never_sends_requests(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({"url": "https://mms.pinduoduo.com/home", "cookies": [COOKIE], "local_storage": {"key": 1}}), encoding="utf-8")
            with patch("automation.pdd.storage.state.request", new_callable=AsyncMock) as request:
                with self.assertRaises(ValueError):
                    await import_state(path)
                request.assert_not_awaited()

    async def test_clear_reports_deletions_before_rechecking_both_stores(self):
        events = []
        async def respond(endpoint, action, params=None):
            events.append(action)
            if action == "local_storage_open": return {"tab_id": 1}
            if action == "cookies_export": return {"cookies": [], "count": 0}
            if action == "local_storage_export": return {"local_storage": {}, "count": 0}
            if action == "cookies_clear": return {"cleared": 0, "cleared_cookies": [], "failed_cookies": []}
            if action == "local_storage_clear": return {"cleared": 0, "cleared_keys": []}
        with patch("automation.pdd.storage.state.request", side_effect=respond):
            result = await clear_state(report=lambda stage, data: events.append(stage))
        index = events.index("cleared")
        self.assertEqual(events[index + 1:], ["cookies_export", "local_storage_export", "after"])
        self.assertEqual(result["after"], {"cookies": [], "local_storage_keys": []})

    def test_before_after_report_never_contains_values(self):
        data = summary({"cookies": [COOKIE], "local_storage": {"storage-key": "secret-storage"}})
        output = io.StringIO()
        with redirect_stdout(output):
            print_state_report("before", data)
            print_state_report("after", data)
        text = output.getvalue()
        self.assertIn("fixture", text)
        self.assertIn("storage-key", text)
        self.assertNotIn("secret-cookie", text)
        self.assertNotIn("secret-storage", text)
