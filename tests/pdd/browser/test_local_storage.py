import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from automation.pdd.storage.local_storage import import_local_storage, export_local_storage, clear_local_storage


class LocalStorageBusinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_validates_before_opening_page(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "storage.json"
            for data in [[], {"url": "https://evil.test", "local_storage": {}},
                         {"url": "https://mms.pinduoduo.com", "local_storage": {"key": 1}}]:
                path.write_text(json.dumps(data), encoding="utf-8")
                with patch("automation.pdd.storage.local_storage.request", new_callable=AsyncMock) as request:
                    with self.assertRaises(ValueError):
                        await import_local_storage(path)
                    request.assert_not_awaited()

    async def test_import_prepares_page_then_sends_storage_only(self):
        raw = {"url": "https://mms.pinduoduo.com/home", "cookies": [{"fixture": "ignored"}], "local_storage": {"key": "value"}}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "storage.json"
            path.write_text(json.dumps(raw), encoding="utf-8-sig")
            with patch("automation.pdd.storage.local_storage.request", new_callable=AsyncMock,
                       side_effect=[{"tab_id": 7}, {"imported": 1}]) as request:
                self.assertEqual(await import_local_storage(path), {"imported": 1})
                self.assertEqual(request.await_args_list[0].args[1], "local_storage_open")
                self.assertEqual(request.await_args_list[1].args, ("ws://127.0.0.1:8765/", "local_storage_import", {"tab_id": 7, "local_storage": {"key": "value"}}))

    async def test_export_writes_reimportable_file(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "storage.json"
            with patch("automation.pdd.storage.local_storage.request", new_callable=AsyncMock,
                       side_effect=[{"tab_id": 7}, {"tab_id": 7, "count": 1, "local_storage": {"key": "value"}}]):
                result = await export_local_storage(path)
            self.assertEqual(result["count"], 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["local_storage"], {"key": "value"})

    async def test_clear_uses_selected_tab(self):
        with patch("automation.pdd.storage.local_storage.request", new_callable=AsyncMock,
                   side_effect=[{"tab_id": 7}, {"cleared_keys": ["key"], "remaining": 0}]) as request:
            result = await clear_local_storage(tab_id=7)
            self.assertEqual(result["remaining"], 0)
            self.assertEqual(request.await_args_list[0].args[2]["tab_id"], 7)
