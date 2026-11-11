"""Own child collectors and keep the database consumer alive during rank retries."""
import math
import os
import signal
import subprocess
import sys
import time
import threading
from contextlib import contextmanager

from automation.pdd.spider import config
from automation.pdd.spider.concurrency import workflow_lock


def start(module, *args):
    from processkit import Command
    env = dict(os.environ, PDD_SPIDER_ROOT=str(config.BUSINESS_ROOT), PYTHONUNBUFFERED='1')
    command = (Command(sys.executable, ['-u', '-m', module, *args]).envs(env)
               .stdout('inherit').stderr('inherit').windows_graceful_ctrl_break())
    return ManagedProcess(command)


class ManagedProcess:
    """Kernel-backed tree ownership includes Windows virtualenv Python redirectors."""
    def __init__(self, command):
        from processkit import ProcessGroup
        self.group = ProcessGroup()
        self.returncode = None
        self.closed = False
        self.done = threading.Event()
        try:
            self.process = self.group.start(command)
        except BaseException:
            self.group.shutdown()
            raise
        self.pid = self.process.pid

        def reap():
            try:
                result = self.process.finish()
                self.returncode = result.code if result.code is not None else 1
            except Exception:
                self.returncode = 1
            finally:
                self.done.set()
        threading.Thread(target=reap, daemon=True).start()

    def poll(self):
        return self.returncode if self.done.is_set() else None

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return self.returncode

    def send_signal(self, sig):
        os.kill(self.pid, sig)

    def terminate(self):
        self.group.kill_all()

    kill = terminate

    def close(self):
        # Even if the launcher has exited, reap descendants still held by the job.
        if not self.closed:
            self.group.shutdown()
            self.closed = True


def request_stop(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
    except OSError:
        pass  # A detached console may reject control events; stop() still enforces exit.


def stop(process, *, requested=False, timeout=15):
    if process is None or process.poll() is not None:
        return
    if not requested:
        request_stop(process)
    try:
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                process.kill()
                process.wait(timeout=5)


@contextmanager
def uninterrupted_cleanup():
    """Repeated Ctrl+C must not abandon the second child while waiting for the first."""
    previous = {}
    try:
        for name in ('SIGINT', 'SIGBREAK'):
            sig = getattr(signal, name, None)
            if sig is not None:
                previous[sig] = signal.signal(sig, signal.SIG_IGN)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_all(*processes):
    errors = []
    with uninterrupted_cleanup():
        # Signal both immediately, so Android cannot keep collecting during WDA cleanup.
        for process in processes:
            request_stop(process)
        for process in processes:
            try:
                stop(process, requested=True)
            except Exception as error:
                errors.append(error)
            finally:
                if isinstance(process, ManagedProcess):
                    try:
                        process.close()
                    except Exception as error:
                        errors.append(error)
    if errors:
        raise RuntimeError('子进程清理失败：' + '；'.join(map(str, errors)))


class ProductWorker:
    def __init__(self, attempts, delay):
        self.attempts, self.delay = attempts, delay
        self.process, self.started, self.result = None, 0, None
        self.retry_at = 0

    def tick(self, *, producing):
        if self.result is not None:
            return
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                return
            if isinstance(self.process, ManagedProcess):
                self.process.close()
            self.process = None
            if code == 0 and not producing:
                self.result = 0
                return
            if self.started >= self.attempts:
                self.result = code or 1
                print(f'[完整流程] 商品进程退出（{code}），启动次数已达 {self.attempts}，榜单仍可独立完成', flush=True)
                return
            print(f'[完整流程] 商品进程退出（{code}），{self.delay:g} 秒后恢复数据库未完成任务', flush=True)
            self.retry_at = time.monotonic() + self.delay
        if time.monotonic() >= self.retry_at:
            self.process = start('automation.pdd.spider.product', '--android')
            self.started += 1


def run():
    try:
        with workflow_lock('mobile-run'):
            return _run()
    except KeyboardInterrupt:
        print('[完整流程] 已取消等待完整流程使用权', flush=True)
        return 130


def _run():
    from automation.pdd.spider.initialize import initialize
    if config.RETRY_ATTEMPTS < 1 or not math.isfinite(config.RETRY_DELAY) or config.RETRY_DELAY < 0:
        print('请检查 SPIDER_RETRY_ATTEMPTS 和 SPIDER_RETRY_DELAY')
        return 1
    initialize(config.BUSINESS_ROOT)
    rank, worker = None, ProductWorker(config.RETRY_ATTEMPTS, config.RETRY_DELAY)
    rank_code = 1
    try:
        # Held across startup and rank restarts; no task notification or IPC is needed.
        with workflow_lock('mobile'):
            print('[完整流程] iOS 采集榜单，安卓按数据库状态采集商品；Ctrl+C 停止两个进程', flush=True)
            for attempt in range(config.RETRY_ATTEMPTS):
                rank = start('automation.pdd.spider.ranking', '--ios')
                while rank.poll() is None:
                    worker.tick(producing=True)
                    time.sleep(.5)
                rank_code = rank.returncode
                if isinstance(rank, ManagedProcess):
                    rank.close()
                if rank_code == 0:
                    break
                if attempt + 1 < config.RETRY_ATTEMPTS:
                    print(f'[完整流程] 榜单退出（{rank_code}），{config.RETRY_DELAY:g} 秒后续采（{attempt+2}/{config.RETRY_ATTEMPTS}）', flush=True)
                    until = time.monotonic() + config.RETRY_DELAY
                    while time.monotonic() < until:
                        worker.tick(producing=True)
                        time.sleep(.5)
            # Start the consumer even if ranking completed before the first poll.
            worker.tick(producing=True)
        print(f'[完整流程] 榜单阶段结束（退出码 {rank_code}），等待商品队列处理完成', flush=True)
        while worker.result is None:
            worker.tick(producing=False)
            if worker.result is None:
                time.sleep(.5)
        code = 0 if rank_code == 0 and worker.result == 0 else 1
        print(f'[完整流程] {"全部完成" if code == 0 else "存在失败，请检查日志后重新运行续采"}', flush=True)
        return code
    except KeyboardInterrupt:
        print('[完整流程] 正在停止两个采集进程，请等待清理完成；进度已保留', flush=True)
        return 130
    except Exception as error:
        print(f'[完整流程] 启动或调度失败：{error}', flush=True)
        return 1
    finally:
        stop_all(rank, worker.process)
        print('[完整流程] 两个采集进程均已退出', flush=True)
