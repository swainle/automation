"""单浏览器、逐店串行的每日业务流程。"""
import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from websockets.exceptions import ConnectionClosed
from automation.com.extension_bridge import BridgeError, request
from automation.com.extension.client import save_jpeg
from automation.com.files import publish_file, write_versioned
from automation.pdd.credentials import FeishuCredentials, ShopCredential, text_value
from automation.pdd.screenshot.services import InfrastructureError, StartupError
from automation.pdd.screenshot.blocking import blocking
from automation.pdd.screenshot.logs import log
from automation.pdd.screenshot.state import today, VERIFICATIONS, VERIFICATION_LABELS
from automation.pdd.screenshot.workbook import export_receivables, parse_amount
from automation.pdd.screenshot.format_xlsx import read_source_shops, shop_identity

SHOP_NAME_SELECTOR = "#mms-header-next .user-info-top .user-name-name > span"


class LoginError(RuntimeError):
    pass


class BusinessError(RuntimeError):
    pass


class CleanupFatal(RuntimeError):
    pass


class DayChanged(RuntimeError):
    pass


class VerificationLimit(RuntimeError):
    pass


def shop_segment(name):
    # 店铺名原样作为目录名；不能安全表示时拒绝，避免归一化后两个店铺覆盖。
    if (not name or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.endswith((".", " ")) or len(name) > 150
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name)):
        raise ValueError("店铺名不能安全用作跨平台目录名")
    return name


def load_shops(root):
    source = read_source_shops(root / 'data/店铺应收截图.xlsx')
    service = FeishuCredentials(root)
    records = service.fetch_records()
    credentials = {}
    for fields in records:
        name = text_value(fields.get(service.shop_field)).strip()
        if not name or (name.startswith('【') and not name.startswith('【拼多多】')):
            continue
        key = shop_identity(name)
        if key not in source:
            continue
        shop_segment(name)
        if key in credentials:
            raise ValueError("飞书存在重复店铺名，请先处理重复记录")
        credentials[key] = (name, fields)
    shops = []
    for key, entry in source.items():
        name, fields = credentials.get(key, (key, {}))
        shop = ShopCredential(name, text_value(fields.get(service.account_field)).strip(),
                              text_value(fields.get(service.password_field)), entry['row_number'], entry['manager'])
        shop_folder_name(shop)
        shops.append(shop)
    return shops


def shop_folder_name(shop):
    if not isinstance(shop.row_number, int) or isinstance(shop.row_number, bool) or shop.row_number < 1:
        raise ValueError("店铺缺少有效的来源表行号")
    return shop_segment(f"{shop.row_number:03d}{shop.shop_name}")


def download_cookies(base, shop):
    filename = quote(shop + "cookie.json", safe="")
    try:
        with urlopen(base + "/cookies/" + filename, timeout=15) as response:
            raw = response.read()
    except HTTPError as error:
        error.close()
        if error.code == 404:
            raise LoginError("cookies_missing") from None
        raise InfrastructureError("cs_http_error") from None
    except (URLError, TimeoutError, OSError):
        raise InfrastructureError("cs_unreachable") from None
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise LoginError("cookies_invalid_json") from None
    items = obj.get("cookies") if isinstance(obj, dict) else obj
    if not isinstance(items, list) or not items:
        raise LoginError("cookies_empty_or_invalid")
    return raw, obj


@dataclass
class Outcome:
    status: str
    reason: str = ""


class ShopRunner:
    def __init__(self, config, store, services, *, day_clock=today, skip_sms_shop=False, export_on_finish=True):
        self.export_on_finish = export_on_finish
        self.config, self.store, self.services = config, store, services
        self.day_clock = day_clock
        self.skip_sms_shop = skip_sms_shop
        self.day = None
        self.shop = None
        self.tab = None
        self._last_login_status = None

    def check_day(self):
        if self.day_clock() != self.day:
            raise DayChanged()

    async def step(self, name, delay=.5):
        self.check_day()
        await asyncio.sleep(delay)
        self.check_day()
        self.store.step(self.day, self.shop.shop_name, name)
        log("业务", f"{self.shop.shop_name}：{name}")

    async def rpc(self, action, params=None):
        try:
            return await request(self.config.endpoint, action, params)
        except BridgeError:
            # 动作失败和连接失败分开；后者交给进程管理器恢复。
            await self.services.health()
            raise
        except (OSError, TimeoutError, ConnectionClosed):
            raise InfrastructureError("ws_unreachable") from None

    async def cleanup(self):
        for attempt in range(1, 5):
            try:
                result = await self.rpc("automation_state_clear", {"tab_id": self.tab})
                if result.get("remaining") != 0 or not result.get("local_storage_cleared"):
                    raise BusinessError("cleanup_incomplete")
                self.tab = None
                return
            except InfrastructureError:
                raise
            except BridgeError as error:
                if str(error) == "other_pdd_tabs_open":
                    raise StartupError("检测到其他拼多多标签页，请先关闭这些页面后重启流程；未清除其状态") from None
                if str(error) == "unsupported_action":
                    raise StartupError("请在 chrome://extensions 重新加载项目插件后重启流程") from None
                self.store.event(self.day or self.day_clock(), self.shop.shop_name if self.shop else None,
                                 "cleanup_failed", {"attempt": attempt, "error": str(error)})
                if attempt < 4:
                    await asyncio.sleep(.5)
            except Exception as error:
                self.store.event(self.day or self.day_clock(), self.shop.shop_name if self.shop else None,
                                 "cleanup_failed", {"attempt": attempt, "error": type(error).__name__})
                if attempt < 4:
                    await asyncio.sleep(.5)
        raise CleanupFatal("cleanup_failed_after_four_attempts")

    async def login_state(self, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + self.config.page_timeout
        while time.monotonic() < deadline:
            self.check_day()
            try:
                result = await self.rpc("login_status", {"tab_id": self.tab})
                descriptions = {
                    "logged_in": "已识别后台，准备核对店铺身份",
                    "login_page": "登录页，等待登录完成",
                    "verification_required": "需要验证码或身份验证",
                    **VERIFICATION_LABELS,
                    "login_rejected": "页面提示登录失败",
                    "unknown_page": "等待店铺名或待收货数据区域",
                }
                state = result.get("status")
                state = state if state in descriptions else "unknown_page"
                if state != self._last_login_status:
                    self._last_login_status = state
                    log("业务", f"{self.shop.shop_name}：{descriptions[state]}")
                if result.get("status") in {"logged_in", "login_page", "login_rejected"} | VERIFICATIONS:
                    return result["status"]
            except BridgeError:
                pass  # 导航中内容脚本尚未就绪，继续等待状态，不重发登录命令。
            await asyncio.sleep(.25)
        log("业务", f"{self.shop.shop_name}：登录状态等待超时，未识别到后台就绪元素")
        raise LoginError("workbench_unreachable")

    async def enter_workbench(self):
        self._last_login_status = None
        try:
            try:
                log("业务", f"{self.shop.shop_name if self.shop else '当前店铺'}：等待账号登录按钮、店铺名或待收货数据区域（最多 10 秒）")
                async with asyncio.timeout(10):
                    await self.rpc("tab_navigate", {"tab_id": self.tab,
                        "url": "https://mms.pinduoduo.com/home", "wait_for_load": False})
                    state = await self.login_state(time.monotonic() + 10)
            except TimeoutError:
                log("业务", "页面元素等待超时：10 秒内未识别到登录入口或后台就绪元素")
                raise BusinessError("page_elements_timeout") from None
            except LoginError as error:
                if str(error) == "workbench_unreachable":
                    raise BusinessError("page_elements_timeout") from None
                raise
            except BridgeError as error:
                raise BusinessError("navigation_" + str(error)) from None
            if state == "login_page":
                if not self.shop.account or not self.shop.password.strip():
                    missing = []
                    if not self.shop.account:
                        missing.append("account")
                    if not self.shop.password.strip():
                        missing.append("password")
                    raise LoginError("credentials_missing_" + "_and_".join(missing))
                # login_submit 自带切换账号登录；提交结果不明时只轮询状态，不再次提交。
                try:
                    response = await self.rpc("login_submit", {"tab_id": self.tab,
                        "account": self.shop.account, "password": self.shop.password})
                    if response.get("status") in VERIFICATIONS:
                        raise LoginError(response["status"])
                except BridgeError as error:
                    if str(error) != "login_submission_outcome_unknown":
                        raise
                await asyncio.sleep(.5)
                log("业务", f"{self.shop.shop_name}：登录已提交，等待店铺名或待收货数据区域（最多 {self.config.page_timeout:g} 秒）")
                deadline = time.monotonic() + self.config.page_timeout
                while time.monotonic() < deadline:
                    state = await self.login_state(deadline)
                    if state != "login_page":
                        break
                    await asyncio.sleep(.25)
            if state != "logged_in":
                if state == "login_page":
                    log("业务", f"{self.shop.shop_name}：登录结果等待超时，页面仍处于登录状态")
                raise LoginError(state if state in VERIFICATIONS | {"login_rejected"} else "login_not_completed")
        except BridgeError as error:
            raise LoginError(str(error)) from None

    async def verify_identity(self):
        timeout = min(10, self.config.page_timeout)
        log("业务", f"{self.shop.shop_name}：后台已就绪，等待店铺名进行身份核对（最多 {timeout:g} 秒）")
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self.check_day()
                    try:
                        result = await self.rpc("shop_identity", {"tab_id": self.tab, "selector": SHOP_NAME_SELECTOR})
                    except BridgeError:
                        await asyncio.sleep(.25)
                        continue
                    name = result.get("shop_name")
                    if name:
                        if name != self.shop.shop_name.removeprefix("【拼多多】").strip():
                            log("业务", f"{self.shop.shop_name}：店铺身份核对失败，页面店铺名与目标店铺不一致")
                            raise LoginError("shop_identity_mismatch")
                        log("业务", f"{self.shop.shop_name}：店铺身份核对通过")
                        return
                    await asyncio.sleep(.25)
        except TimeoutError:
            log("业务", f"{self.shop.shop_name}：店铺身份无法确认，{timeout:g} 秒内未获取到店铺名，按业务失败重试")
            raise BusinessError("shop_identity_unconfirmed") from None

    async def execute(self):
        folder = self.config.root / "data" / self.day / shop_folder_name(self.shop)
        await self.step("download_cookies")
        raw, cookies = await blocking(download_cookies, self.services.cs_base,
                                     self.shop.shop_name.removeprefix("【拼多多】").strip())
        folder.mkdir(parents=True, exist_ok=True)
        write_versioned(folder / "cookies.json", raw)
        await self.step("validate_cookies")
        try:
            await self.rpc("cookies_validate", {"cookies": cookies})
        except BridgeError as error:
            raise LoginError(str(error)) from None
        await self.step("new_tab")
        self.tab = (await self.rpc("tab_create"))["tab_id"]
        await self.step("import_cookies")
        try:
            await self.rpc("cookies_import", {"cookies": cookies})
        except BridgeError as error:
            raise LoginError(str(error)) from None
        await self.step("login", delay=1)
        await self.enter_workbench()
        await self.step("close_notification")
        await self.rpc("notification_close", {"tab_id": self.tab})
        await self.step("verify_shop_identity")
        await self.verify_identity()
        await self.step("screenshot")
        # 金额动作先关闭通知并显示金额，截图必须在这些动作完成后进行。
        result = await self.rpc("receivable_amount", {"tab_id": self.tab})
        amount = parse_amount(result.get("amount"))
        try:
            await self.rpc("screenshot_prepare", {"tab_id": self.tab})
            panel = await self.rpc("receivable_panel", {"tab_id": self.tab})
            result = await self.rpc("screenshot", {"tab_id": self.tab, "selector": panel["selector"],
                "selector_type": "xpath", "viewport_to_bottom": True, "format": "jpeg"})
        finally:
            await self.rpc("screenshot_restore", {"tab_id": self.tab})
        self.check_day()
        temporary_image = folder / "数据截图.pending.jpg"
        try:
            save_jpeg(result, temporary_image)
            image_path = publish_file(temporary_image, folder / "数据截图.jpg")
        finally:
            temporary_image.unlink(missing_ok=True)
        self.store.save_receivable(self.day, self.shop.shop_name, image_path, amount)
        log("业务", f"{self.shop.shop_name}：待到账金额 {amount} 元")
        await self.step("export_cookies")
        result = await self.rpc("cookies_export")
        self.check_day()
        cookies = result.get("cookies")
        if not isinstance(cookies, list) or not cookies or result.get("count") != len(cookies):
            raise BusinessError("exported_cookies_invalid")
        target = folder / "new.json"
        temp = folder / "new.json.tmp"
        temp.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        publish_file(temp, target)
        await self.step("done")

    async def run_shop(self, day, shop):
        self.day, self.shop = day, shop
        for attempt in range(1, 7):
            self.check_day()
            self.store.begin(day, shop.shop_name)
            log("业务", f"[{day}] {shop.shop_name}：第 {attempt}/6 次尝试")
            outcome = Outcome("interrupted", "process_interrupted")
            try:
                # 每次尝试前清理，防止上轮异常残留；每次结束也在 finally 清理。
                await self.cleanup()
                await self.execute()
                outcome = Outcome("success")
            except LoginError as error:
                reason = str(error)
                outcome = Outcome("verification_other" if reason == "verification_required" else
                                  reason if reason in VERIFICATIONS else "login_error", reason)
            except asyncio.CancelledError:
                outcome = Outcome("interrupted", "day_changed" if self.day_clock() != day else "process_interrupted")
                raise
            except DayChanged:
                outcome = Outcome("interrupted", "day_changed")
                raise
            except InfrastructureError:
                outcome = Outcome("interrupted", "infrastructure_error")
                raise
            except CleanupFatal:
                outcome = Outcome("interrupted", "cleanup_failed")
                # cleanup 已达到四次，不在 finally 再次调用。
                self.store.finish(day, shop.shop_name, outcome.status, outcome.reason, False)
                raise
            except (BridgeError, BusinessError) as error:
                outcome = Outcome("business_failed", str(error))
            except Exception as error:
                outcome = Outcome("business_failed", type(error).__name__)
            finally:
                if outcome.reason != "cleanup_failed":
                    try:
                        await self.cleanup()
                    except BaseException:
                        self.store.finish(day, shop.shop_name, outcome.status, outcome.reason, False)
                        raise
                    self.store.finish(day, shop.shop_name, outcome.status, outcome.reason, True)
            self.check_day()
            if outcome.status == "business_failed":
                log("业务", f"{shop.shop_name}：第 {attempt}/6 次业务失败，原因={outcome.reason}")
            if outcome.status in {"success", "login_error"} | VERIFICATIONS:
                log("业务", f"{shop.shop_name}：{outcome.status} {outcome.reason}")
                return
        log("业务", f"{shop.shop_name}：业务重试耗尽，保留为未完成")

    async def run_day(self, day, shops):
        if not self.export_on_finish:
            return await self._run_day(day, shops)
        try:
            await self._run_day(day, shops)
        finally:
            saved = {row["shop"]: row for row in self.store.receivables(day)}
            records = []
            for shop in shops:
                state = self.store.get(day, shop.shop_name) or {}
                records.append({**saved.get(shop.shop_name, {"shop": shop.shop_name, "amount": None, "image_path": ""}),
                                "shop": '【拼多多】' + shop_identity(shop.shop_name),
                                "manager": shop.manager,
                                "exception": VERIFICATION_LABELS[state["status"]] if state.get("status") in VERIFICATIONS else
                                "" if state.get("status") == "success" else state.get("reason") or state.get("status", "pending")})
            path = export_receivables(self.config.root, day, records)
            log("业务", f"店铺应收截图表已导出：{path}")

    async def _run_day(self, day, shops):
        self.day, self.shop = day, None
        self.store.seed(day, [shop.shop_name for shop in shops])
        self.store.save_shop_details(day, shops)
        log("业务", f"{day} 飞书店铺列表读取完成，共 {len(shops)} 家")
        await self.cleanup()
        collected = {row["shop"]: row for row in self.store.receivables(day) if row["amount"] is not None}
        consecutive_verifications = 0
        for shop in shops:
            self.check_day()
            previous = self.store.get(day, shop.shop_name)
            if self.skip_sms_shop and previous and previous["status"] == "sms_required":
                log("业务", f"{shop.shop_name}：跳过当天短信验证店铺（--skip-sms-shop）")
                continue
            record = collected.get(shop.shop_name)
            expected_image = self.config.root / "data" / day / shop_folder_name(shop) / "数据截图.jpg"
            legacy_success = previous and previous["status"] == "success" and (
                record is None or Path(record["image_path"]) != expected_image.resolve())
            if not self.store.completed(day, shop.shop_name) or legacy_success:
                await self.run_shop(day, shop)
                current = self.store.get(day, shop.shop_name)
                consecutive_verifications = consecutive_verifications + 1 if current["status"] in VERIFICATIONS else 0
                if consecutive_verifications > 5:
                    raise VerificationLimit("连续 6 家店铺需要人工验证，已停止流程")
        self.shop = None
