"""可取消的同步 I/O 桥接；退出不等待不可取消的网络工作线程。"""
import asyncio
import contextlib
import threading


async def blocking(function, *args):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    def complete(value, error):
        if not future.done():
            if error is None:
                future.set_result(value)
            else:
                future.set_exception(error)
    def worker():
        value, error = None, None
        try:
            value = function(*args)
        except BaseException as exc:
            error = exc
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(complete, value, error)
    threading.Thread(target=worker, name="automation-io", daemon=True).start()
    return await future
