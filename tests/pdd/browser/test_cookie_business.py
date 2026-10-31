"""browser Cookie 业务的本地测试，不操作真实浏览器。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from automation.pdd.storage.cookies import import_cookies, export_cookies, clear_cookies


class CookieBusinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_keeps_metadata_in_one_request(self):
        cookies = [{"domain": ".pinduoduo.com", "name": "fixture", "value": "test",
                    "hostOnly": False, "partitionKey": {"topLevelSite": "https://pinduoduo.com"}}]
        result = {"cleared": 2, "imported": 1, "remaining": 1}
        with TemporaryDirectory() as folder:
            source = Path(folder) / "cookies.json"
            source.write_text(json.dumps({"cookies": cookies}), encoding="utf-8-sig")
            with patch("automation.pdd.storage.cookies.request", new_callable=AsyncMock, return_value=result) as request:
                self.assertEqual(await import_cookies(source, port=9876), result)
                request.assert_awaited_once_with("ws://127.0.0.1:9876/", "cookies_import", {"cookies": {"cookies": cookies}})

    async def test_invalid_json_never_contacts_plugin(self):
        with TemporaryDirectory() as folder:
            source = Path(folder) / "invalid.json"
            source.write_text("invalid", encoding="utf-8")
            with patch("automation.pdd.storage.cookies.request", new_callable=AsyncMock) as request:
                with self.assertRaises(ValueError):
                    await import_cookies(source)
                request.assert_not_awaited()

    async def test_export_writes_empty_array_and_returns_path(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "nested" / "cookies.json"
            with patch("automation.pdd.storage.cookies.request", new_callable=AsyncMock, return_value={"cookies": [], "count": 0}):
                result = await export_cookies(output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), [])
            self.assertEqual(result, {"count": 0, "output": output.resolve()})

    async def test_bad_export_preserves_existing_file(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "cookies.json"
            output.write_text("previous", encoding="utf-8")
            with patch("automation.pdd.storage.cookies.request", new_callable=AsyncMock, return_value={"cookies": [], "count": 1}):
                with self.assertRaises(ValueError):
                    await export_cookies(output)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous")

    async def test_clear_uses_browser_service_port(self):
        with patch("automation.pdd.storage.cookies.request", new_callable=AsyncMock, return_value={"remaining": 0}) as request:
            self.assertEqual(await clear_cookies(port=9876), {"remaining": 0})
            request.assert_awaited_once_with("ws://127.0.0.1:9876/", "cookies_clear")

    async def test_invalid_port_does_not_contact_plugin(self):
        with patch("automation.pdd.storage.cookies.request", new_callable=AsyncMock) as request:
            with self.assertRaises(ValueError):
                await clear_cookies(port=80)
            request.assert_not_awaited()
