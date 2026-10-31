"""自动化启动前关闭 Chrome，并准备默认配置中的启动提示偏好。"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import psutil
from automation.pdd.screenshot.state import CHINA


class BrowserPreparationError(RuntimeError):
    pass


def chrome_process(process):
    name = (process.info.get("name") or "").casefold()
    return name in {"chrome.exe", "chrome", "google-chrome", "google-chrome-stable", "google chrome"} or name.startswith("google chrome helper")


def close_existing_chrome():
    targets = []
    for process in psutil.process_iter(["name"]):
        try:
            if chrome_process(process):
                targets.append(process)
        except psutil.NoSuchProcess:
            continue
    for process in targets:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            raise BrowserPreparationError("无法关闭 Chrome，请手动关闭所有 Chrome 窗口和后台进程") from None
    _, alive = psutil.wait_procs(targets, timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            raise BrowserPreparationError("无法结束残留 Chrome 进程，请手动关闭后重试") from None
    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive:
        raise BrowserPreparationError("Chrome 尚未完全退出，已取消重新启动")
    # Chrome 后台进程可能在枚举后产生；此时停止，不与仍运行的 Chrome 并发改配置。
    for process in psutil.process_iter(["name"]):
        try:
            if chrome_process(process):
                raise BrowserPreparationError("检测到 Chrome 再次运行，请关闭后重试")
        except psutil.NoSuchProcess:
            continue
    return len(targets)


def default_user_data():
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA")
        if not base:
            raise BrowserPreparationError("未找到 LOCALAPPDATA，无法准备默认 Chrome 配置")
        return Path(base) / "Google/Chrome/User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    base = Path(os.getenv("CHROME_CONFIG_HOME") or os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "google-chrome"


def prepare_default_profile(backup_root, *, user_data=None):
    root = Path(user_data) if user_data is not None else default_user_data()
    local_state = root / "Local State"
    try:
        state = json.loads(local_state.read_text(encoding="utf-8")) if local_state.exists() else {}
        name = state.get("profile", {}).get("last_used") or "Default"
        if not isinstance(name, str) or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("invalid profile name")
        profile = root / name
        if root.resolve() not in profile.resolve().parents:
            raise ValueError("profile outside user data")
        target = profile / "Preferences"
        prefs = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        if not isinstance(prefs, dict):
            raise ValueError("invalid preferences")
        for section in ("profile", "session", "browser"):
            if section in prefs and not isinstance(prefs[section], dict):
                raise ValueError("invalid preferences section")
            prefs.setdefault(section, {})
    except (ValueError, AttributeError, OSError):
        raise BrowserPreparationError("无法读取 Chrome 默认配置，未修改；请检查 Local State / Preferences") from None
    backup = None
    if target.exists():
        stamp = datetime.now(CHINA).strftime("%Y-%m-%d_%H-%M-%S_%f")
        backup = Path(backup_root) / stamp / name / "Preferences.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
    prefs["profile"].update(exit_type="Normal", exited_cleanly=True, password_manager_enabled=False)
    prefs["session"].update(restore_on_startup=5, startup_urls=[])
    prefs["browser"]["check_default_browser"] = False
    prefs["credentials_enable_service"] = False
    profile.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="Preferences.pdd-", dir=profile, delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(prefs, stream, ensure_ascii=False)
        if target.exists():
            shutil.copystat(target, temp_path)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return {"profile": name, "backup": backup}
