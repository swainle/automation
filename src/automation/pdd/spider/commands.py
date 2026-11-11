"""Shared command setup; both public commands have a fixed internal workflow."""
import argparse
import math
import signal
import threading
from contextlib import ExitStack


def execute(description, collect, *, args=None, role=None):
    # Windows supervisor sends CTRL_BREAK to the child group for graceful cleanup.
    if hasattr(signal, 'SIGBREAK') and threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    if args is None:
        argparse.ArgumentParser(description=description).parse_args()
    from automation.pdd.spider import config
    from automation.pdd.spider.mobile_database import Store
    from automation.pdd.spider.runtime import Runtime, proxy_url
    from automation.pdd.spider.logging_setup import console_logging
    from automation.pdd.spider.initialize import initialize
    if config.RETRY_ATTEMPTS < 1 or any(not math.isfinite(value) or value < 0 for value in (config.RETRY_DELAY, config.PRODUCT_RETRY_DELAY)):
        print('重试次数须为正整数，重试间隔须为非负有限秒数')
        return 1
    android = bool(getattr(args, 'android', False))
    if args is not None and not getattr(args, 'ios', False) and not android and (
            not 1024 <= config.RANKING_WS_PORT <= 65535 or
            not math.isfinite(config.RANKING_READY_TIMEOUT) or config.RANKING_READY_TIMEOUT <= 0):
        print('请检查榜单插件端口、移动视口长宽、预览缩放与就绪超时配置')
        return 1
    root = config.BUSINESS_ROOT
    initialize(root)
    with console_logging(root):
        try:
            # Different devices may work concurrently; one owner per workflow and device.
            with ExitStack() as locks:
                from automation.pdd.spider.concurrency import workflow_lock
                device = 'android' if android else ('ios' if getattr(args, 'ios', False) else 'browser')
                print(f'[会话] 等待 {device} 设备使用权，同一设备串行，不同设备可并行')
                locks.enter_context(workflow_lock(f'device-{device}'))
                if role:
                    locks.enter_context(workflow_lock(role))
                if getattr(args, 'mobile', False):
                    locks.enter_context(workflow_lock('product'))
                store = Store(config.POSTGRES_URL)
                runtime = None
                try:
                    runtime_class = Runtime
                    if android:
                        from automation.pdd.spider.product_android import AndroidRuntime
                        runtime_class = AndroidRuntime
                    runtime = runtime_class(proxy=proxy_url(config.MITM_UPSTREAM), port=config.MITM_PORT)
                    return collect(store, runtime)
                finally:
                    try:
                        if runtime is not None:
                            runtime.close()
                    finally:
                        store.close()
        except KeyboardInterrupt:
            print('已手动停止；任务断点已保留')
            return 0
        except Exception as error:
            print(f'流程停止：{error}')
            return 1
