"""统一启动每日店铺流程；所有浏览器动作通过本机插件执行。"""
import argparse
import asyncio
import signal
from automation.config import env_bool

from automation.pdd.screenshot.blocking import blocking
from automation.pdd.screenshot.logs import configure, close as close_logs, log, report
from automation.pdd.screenshot.config import Config
from automation.pdd.screenshot.runner import CleanupFatal, DayChanged, ShopRunner, VerificationLimit, load_shops
from automation.pdd.screenshot.services import InfrastructureError, Services, StartupError
from automation.pdd.screenshot.state import RunLock, StateStore, today


async def watch_date(day, clock=today):
    while clock() == day:
        await asyncio.sleep(.2)


async def run_cycle(runner, services, day, shops, *, once=False, date_waiter=watch_date, after_day=None):
    async def work():
        await runner.run_day(day, shops)
        if after_day is not None:
            await after_day(day)
        if not once:
            log("业务", f"{day} 本轮店铺处理结束，等待北京时间次日；未完成店铺也可重启后继续。")
            await asyncio.Future()
    work_task = asyncio.create_task(work())
    date_task = asyncio.create_task(date_waiter(day))
    health_task = asyncio.create_task(services.monitor())
    tasks = [work_task, date_task, health_task]
    try:
        finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if health_task in finished:
            await health_task
        if work_task in finished:
            await work_task
            return "finished"
        return "new_day"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 清理失败优先于跨日或进程恢复，禁止带着未清理状态进入下一店。
        for result in results:
            if isinstance(result, CleanupFatal):
                raise result


async def run(config, store, *, once=False, skip_sms_shop=False, report_cli=None):
    restarts = 0
    while True:
        services = Services(config)
        try:
            await services.start()
            while True:
                day = today()
                shops = await blocking(load_shops, config.root)
                runner = ShopRunner(config, store, services, skip_sms_shop=skip_sms_shop, export_on_finish=False)
                store.event(day, None, "cycle_started", {"shops": len(shops)})
                try:
                    options = {}
                    if report_cli is not None:
                        async def finish(day):
                            from automation.pdd.screenshot.export import export_day
                            from automation.pdd.screenshot.report import ReportError, run_report
                            try:
                                await asyncio.to_thread(export_day, config.root, day)
                            except OSError as error:
                                raise ReportError(f"报表导出失败（{type(error).__name__}）") from None
                            await run_report(report_cli)
                        options["after_day"] = finish
                    result = await run_cycle(runner, services, day, shops, once=once, **options)
                except DayChanged:
                    result = "new_day"
                if result == "finished":
                    return
                store.event(day, None, "day_changed", {})
                log("管理", "已跨午夜，清理旧流程后从新日期第一家店铺开始。")
        except (CleanupFatal, StartupError):
            raise
        except Exception as error:
            import asyncssh
            if not isinstance(error, (InfrastructureError, OSError, asyncssh.Error)):
                raise
            restarts += 1
            store.event(today(), None, "infrastructure_restart", {"attempt": restarts, "error": type(error).__name__})
            if restarts > config.restart_limit:
                raise InfrastructureError("infrastructure_restart_limit_reached") from None
            log("管理", f"服务异常（{type(error).__name__}），准备重启本流程服务（{restarts}/{config.restart_limit}）。")
        finally:
            await services.stop()
        await asyncio.sleep(2)



def run_interruptible(coroutine):
    previous = signal.getsignal(signal.SIGINT)
    try:
        with asyncio.Runner() as runner:
            loop = runner.get_loop()
            task = loop.create_task(coroutine)
            stopping = False
            def interrupt(_signum, _frame):
                nonlocal stopping
                if not stopping:
                    stopping = True
                    log("管理", "正在停止流程并清理，请稍候。")
                    loop.call_soon_threadsafe(task.cancel)
            signal.signal(signal.SIGINT, interrupt)
            try:
                return runner.run(task)
            except asyncio.CancelledError:
                raise KeyboardInterrupt from None
    finally:
        signal.signal(signal.SIGINT, previous)


def main(*, crawl_only=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", default=None, help="处理完当天一轮后退出，默认等待次日自动开始")
    parser.add_argument("--skip-sms-shop", action="store_true", help="跳过当天已记录为 sms_required 的店铺")
    args = parser.parse_args()
    store = None
    try:
        config = Config.load()
        once = env_bool("PDD_SCREENSHOT_ONCE") if args.once is None else args.once
        from automation.pdd.screenshot.report import cli_path
        report_cli = None if crawl_only else cli_path()
        configure(config.root / "logs")
        with RunLock(config.root / "state/automation.lock"):
            store = StateStore(config.db_path)
            run_interruptible(run(config, store, once=once, skip_sms_shop=args.skip_sms_shop, report_cli=report_cli))
        return 0
    except KeyboardInterrupt:
        report("管理", "流程已停止。")
        return 0
    except (ValueError, StartupError, CleanupFatal, InfrastructureError, VerificationLimit) as error:
        report("管理", f"流程停止：{error}")
        return 1
    except Exception as error:
        # 第三方异常可能包含请求内容，不输出原文。
        report("管理", f"流程停止（{type(error).__name__}），请检查配置及服务状态。")
        return 1
    finally:
        if store:
            store.close()
        close_logs()
