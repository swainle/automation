"""仅监听本机的 Chrome 扩展通信服务。"""

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import uuid

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from automation.config import ROOT
ACTIONS = {"notification_close", "receivable_panel", "receivable_amount", "automation_state_clear", "automation_tabs_close", "tab_create", "tab_navigate", "tab_close", "browser_state_clear", "shop_identity", "tabs", "element", "screenshot", "captcha_detect", "login_probe", "login_status", "login_submit",
           "screenshot_prepare", "screenshot_restore",
           "cookies_export", "cookies_import", "cookies_clear", "cookies_validate",
           "yidun_probe", "yidun_status", "yidun_drag", "local_storage_open", "local_storage_import", "local_storage_export", "local_storage_clear"}


class BridgeError(Exception):
    pass


class Bridge:
    def __init__(self, timeout=30):
        self.timeout = timeout
        self.extension = None
        self.pending = {}
        self.lock = asyncio.Lock()

    async def dispatch(self, action, params):
        if action == "status":
            return {"connected": self.extension is not None}
        if action not in ACTIONS or not isinstance(params, dict):
            raise BridgeError("unsupported_action")
        async with self.lock:
            extension = self.extension
            if extension is None:
                raise BridgeError("extension_not_connected")
            print(f"任务开始：{action}", flush=True)
            request_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            try:
                await extension.send(json.dumps({"type": "request", "id": request_id, "action": action, "params": params}))
                response = await asyncio.wait_for(future, self.timeout + 5 if action == "tab_navigate" else self.timeout)
                if not response.get("ok"):
                    error = response.get("error", "extension_operation_failed")
                    raise BridgeError(error if isinstance(error, str) and re.fullmatch(r"[a-z_]+", error) else "extension_operation_failed")
                print(f"任务完成：{action}", flush=True)
                return response.get("result", {})
            except (TimeoutError, ConnectionClosed):
                raise BridgeError("extension_timeout_or_disconnected") from None
            except BridgeError:
                print(f"任务失败：{action}", flush=True)
                raise
            finally:
                self.pending.pop(request_id, None)

    async def handler(self, websocket):
        origin = websocket.request.headers.get("Origin")
        extension_origin = bool(origin and re.fullmatch(r"chrome-extension://[a-p]{32}", origin))
        if origin is not None and not extension_origin:
            await websocket.close(1008, "origin_not_allowed")
            return
        role = None
        try:
            hello_text = await asyncio.wait_for(websocket.recv(), 5)
            if len(hello_text) > 4096:
                raise BridgeError("invalid_handshake")
            hello = json.loads(hello_text)
            role = hello.get("role")
            if hello.get("type") != "hello" or role not in {"extension", "client"} or (origin is not None and role != "extension"):
                raise BridgeError("invalid_handshake")
            if role == "extension":
                if self.extension is not None:
                    await websocket.close(1008, "extension_already_connected")
                    return
                self.extension = websocket
                print("Chrome 插件已连接。", flush=True)
            await websocket.send(json.dumps({"type": "hello", "ok": True}))
            async for raw in websocket:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise BridgeError("invalid_message")
                if message.get("type") == "ping":
                    await websocket.send('{"type":"pong"}')
                elif role == "extension" and message.get("type") == "result":
                    future = self.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        future.set_result(message)
                elif role == "client" and message.get("type") == "request":
                    request_id = message.get("id")
                    if not isinstance(request_id, str) or len(request_id) > 100:
                        raise BridgeError("invalid_request")
                    try:
                        result = await self.dispatch(message.get("action"), message.get("params", {}))
                        response = {"type": "result", "id": request_id, "ok": True, "result": result}
                    except BridgeError as exc:
                        response = {"type": "result", "id": request_id, "ok": False, "error": str(exc)}
                    await websocket.send(json.dumps(response, ensure_ascii=False))
                else:
                    raise BridgeError("invalid_message")
        except (ConnectionClosed, TimeoutError, ValueError, AttributeError, TypeError, BridgeError):
            await websocket.close(1008, "invalid_or_closed_connection")
        finally:
            if role == "extension" and self.extension is websocket:
                self.extension = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_result({"ok": False, "error": "extension_disconnected"})
                print("Chrome 插件已断开。", flush=True)


async def request(endpoint, action, params=None):
    async with connect(endpoint, max_size=24 * 1024 * 1024, open_timeout=5) as websocket:
        await websocket.send(json.dumps({"type": "hello", "role": "client"}))
        hello = json.loads(await asyncio.wait_for(websocket.recv(), 5))
        if hello.get("type") != "hello" or not hello.get("ok"):
            raise BridgeError("handshake_failed")
        request_id = uuid.uuid4().hex
        await websocket.send(json.dumps({"type": "request", "id": request_id, "action": action, "params": params or {}}))
        response = json.loads(await asyncio.wait_for(websocket.recv(), 40))
        if response.get("id") != request_id or not response.get("ok"):
            raise BridgeError(response.get("error", "invalid_response"))
        return response["result"]


async def start_server(port):
    bridge = Bridge()
    async with serve(bridge.handler, "127.0.0.1", port, max_size=24 * 1024 * 1024):
        print(f"插件通信服务已启动：ws://127.0.0.1:{port}/（Ctrl+C 退出）", flush=True)
        await asyncio.Future()


def main():
    from automation.config import load_environment
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.getenv("AUTOMATION_WS_PORT", "8765")))
    sub = parser.add_subparsers(dest="command")
    for name in ("serve", "status", "tabs"):
        sub.add_parser(name)
    for name in ("element", "screenshot"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--tab-id", type=int)
        cmd.add_argument("--selector", required=name == "element")
        cmd.add_argument("--selector-type", choices=("css", "xpath"), default="css")
        if name == "element":
            cmd.add_argument("--text", action="store_true", help="包含元素文本（不包含表单值）")
        else:
            cmd.add_argument("--full-page", action="store_true")
            cmd.add_argument("--output", type=Path, default=ROOT / "data" / "extension" / "screenshot.png")
    args = parser.parse_args()
    args.command = args.command or "serve"
    if not 1024 <= args.port <= 65535:
        parser.error("port 必须在 1024 到 65535 之间")
    try:
        if args.command == "serve":
            asyncio.run(start_server(args.port))
            return 0
        params = {}
        if args.command in {"element", "screenshot"}:
            params = {"selector": args.selector, "selector_type": args.selector_type}
            if args.tab_id is not None:
                params["tab_id"] = args.tab_id
            if args.command == "element":
                params["include_text"] = args.text
            else:
                params["full_page"] = args.full_page
        result = asyncio.run(request(f"ws://127.0.0.1:{args.port}/", args.command, params))
        if args.command == "screenshot":
            image = base64.b64decode(result["data"], validate=True)
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                raise BridgeError("invalid_png_response")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(image)
            print(f"截图已保存：{args.output.resolve()}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 0
    except BridgeError as exc:
        print(f"插件任务失败：{exc}")
        return 1
    except Exception as exc:
        print(f"通信或保存失败（{type(exc).__name__}），请检查本机服务、连接状态及输出路径。")
        return 1
