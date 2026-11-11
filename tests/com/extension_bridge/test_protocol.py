import asyncio
import json
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from automation.com.extension_bridge import Bridge, BridgeError, request, main


class BridgeEntryTests(unittest.TestCase):
    def test_no_subcommand_starts_server(self):
        with patch("sys.argv", ["extension_bridge"]), patch(
            "automation.com.extension_bridge.start_server"
        ) as start, patch("automation.com.extension_bridge.asyncio.run", side_effect=lambda coroutine: coroutine.close()):
            self.assertEqual(main(), 0)
            start.assert_called_once_with(8765)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = Bridge(timeout=0.1)
        self.server = await serve(self.bridge.handler, "127.0.0.1", 0)
        self.endpoint = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_status_and_missing_extension(self):
        result = await request(self.endpoint, "status")
        self.assertFalse(result["connected"])
        with self.assertRaisesRegex(BridgeError, "extension_not_connected"):
            await request(self.endpoint, "tabs")

    async def test_webpage_origin_is_rejected(self):
        async with connect(self.endpoint, origin="https://mms.pinduoduo.com") as ws:
            with self.assertRaises(ConnectionClosed):
                await ws.recv()

    async def test_roundtrip_and_timeout_cleanup(self):
        async with connect(self.endpoint, origin="chrome-extension://" + "a" * 32) as extension:
            await extension.send(json.dumps({"type": "hello", "role": "extension"}))
            self.assertTrue(json.loads(await extension.recv())["ok"])
            task = asyncio.create_task(request(self.endpoint, "element", {"selector": "#target"}))
            message = json.loads(await extension.recv())
            self.assertEqual(message["params"]["selector"], "#target")
            await extension.send(json.dumps({"type": "result", "id": message["id"], "ok": True,
                                             "result": {"elements": [{"tag": "div"}]}}))
            self.assertEqual((await task)["elements"][0]["tag"], "div")
            with self.assertRaisesRegex(BridgeError, "extension_timeout_or_disconnected"):
                await request(self.endpoint, "tabs")
            self.assertFalse(self.bridge.pending)

    async def test_unknown_action_is_rejected(self):
        with self.assertRaisesRegex(BridgeError, "unsupported_action"):
            await request(self.endpoint, "execute_javascript")

    async def test_edge_routes_ranking_requests(self):
        async with connect(self.endpoint) as edge:
            await edge.send(json.dumps({'type': 'hello', 'role': 'extension', 'browser': 'edge'}))
            self.assertTrue(json.loads(await edge.recv())['ok'])
            task = asyncio.create_task(request(self.endpoint, 'ranking_read', browser='edge'))
            message = json.loads(await asyncio.wait_for(edge.recv(), 2))
            await edge.send(json.dumps({'type': 'result', 'id': message['id'], 'ok': True, 'result': {'cards': []}}))
            self.assertEqual(await task, {'cards': []})

    async def test_two_browsers_route_requests_and_disconnect_independently(self):
        async with connect(self.endpoint) as chrome, connect(self.endpoint) as sogou:
            for browser, socket in [('chrome', chrome), ('sogou', sogou)]:
                await socket.send(json.dumps({'type': 'hello', 'role': 'extension', 'browser': browser}))
                self.assertTrue(json.loads(await socket.recv())['ok'])
            for browser, action, socket in [('chrome', 'tabs', chrome), ('sogou', 'ranking_prepare', sogou)]:
                self.assertTrue((await request(self.endpoint, 'status', browser=browser))['connected'])
                task = asyncio.create_task(request(self.endpoint, action))
                message = json.loads(await asyncio.wait_for(socket.recv(), 2))
                self.assertEqual(message['action'], action)
                # The other browser cannot supply a result for this request.
                other = sogou if socket is chrome else chrome
                await other.send(json.dumps({'type': 'result', 'id': message['id'], 'ok': True, 'result': 'wrong'}))
                await socket.send(json.dumps({'type': 'result', 'id': message['id'], 'ok': True, 'result': browser}))
                self.assertEqual(await task, browser)
            await sogou.close()
            self.assertTrue((await request(self.endpoint, 'status', browser='chrome'))['connected'])
            self.assertFalse((await request(self.endpoint, 'status', browser='sogou'))['connected'])
