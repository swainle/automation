"""Prepare and reuse native Android App automation."""
import argparse
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

from automation.pdd.spider import config


class AndroidConnection:
    def __init__(self, serial=None):
        self.serial = serial or os.getenv('SPIDER_ANDROID_SERIAL', '').strip()
        self.port = int(os.getenv('SPIDER_ANDROID_PORT', '7912'))
        self.package = 'com.xunmeng.pinduoduo'
        configured = os.getenv('SPIDER_ADB_EXE', '').strip()
        bundled = config.TOOLS_ROOT / 'vendor/android' / ('adb.exe' if os.name == 'nt' else 'adb')
        self.adb = configured or (str(bundled) if bundled.is_file() else shutil.which('adb'))
        if not self.adb or not Path(self.adb).is_file():
            raise RuntimeError('找不到 ADB；请安装 Android SDK Platform-Tools，设置 SPIDER_ADB_EXE 或放入 tools/vendor/android/')
        if not 1024 <= self.port <= 65535 or not re.fullmatch(r'[A-Za-z0-9_.]+', self.package):
            raise ValueError('请检查 SPIDER_ANDROID_PORT，必须为 1024–65535 的端口')

    def command(self, *args, device=True, allow_empty=False):
        command = [self.adb, *(['-s', self.serial] if device and self.serial else []), *args]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode and not (allow_empty and result.returncode == 1 and not result.stderr.strip()):
            raise RuntimeError(f'ADB {args[0]} 失败：{(result.stderr or result.stdout).strip()[-1200:]}')
        return result.stdout.strip()

    def prepare(self):
        devices = {}
        for line in self.command('devices', '-l', device=False).splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] in {'device', 'offline', 'unauthorized'}:
                devices[fields[0]] = fields[1]
        if self.serial:
            if devices.get(self.serial) != 'device':
                raise RuntimeError(f'安卓设备 {self.serial} 未就绪（{devices.get(self.serial, "未连接")}），请解锁并允许 USB 调试')
        else:
            ready = [serial for serial, state in devices.items() if state == 'device']
            if len(ready) != 1:
                raise RuntimeError('请连接并授权一台安卓设备；多设备时设置 SPIDER_ANDROID_SERIAL 或使用 --serial')
            self.serial = ready[0]
        if not self.command('shell', 'pm', 'path', self.package).startswith('package:'):
            raise RuntimeError('手机未安装拼多多 App，请先安装并登录')
        mappings = [line.split() for line in self.command('forward', '--list').splitlines()]
        current = next((row for row in mappings if len(row) == 3 and row[1] == f'tcp:{self.port}'), None)
        if current and (current[0] != self.serial or current[2] != 'tcp:9008'):
            raise RuntimeError(f'端口 {self.port} 已用于其他设备或服务，请设置 SPIDER_ANDROID_PORT')
        if not current:
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', self.port)) == 0:
                    raise RuntimeError(f'端口 {self.port} 被其他程序占用，未覆盖')
            self.command('forward', '--no-rebind', f'tcp:{self.port}', 'tcp:9008')
        self.endpoint = f'http://127.0.0.1:{self.port}'
        from automation.pdd.spider.utils import _xlsx_lock
        key = re.sub(r'[^A-Za-z0-9_-]', '_', self.serial)
        with _xlsx_lock(config.BUSINESS_ROOT / f'state/android-{key}'):
            self.ensure_service()
        os.environ['ADBUTILS_ADB_PATH'] = self.adb
        import uiautomator2 as u2
        self.device = u2.connect(self.serial, port=9008)
        self.device.info
        print(f'[Android] 原生连接已就绪：{self.serial}，UiAutomator {self.endpoint}；服务和转发保留')
        return self.endpoint

    def ping(self):
        try:
            with urlopen(f'http://127.0.0.1:{self.port}/ping', timeout=2) as response:
                return response.read() == b'pong'
        except OSError:
            return False

    def ensure_service(self):
        if self.ping():
            return
        log = config.BUSINESS_ROOT / 'logs/android-service.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, SPIDER_ADB_EXE=self.adb, SPIDER_ANDROID_PORT=str(self.port),
                   ADBUTILS_ADB_PATH=self.adb, PYTHONIOENCODING='utf-8')
        with log.open('ab') as output:
            process = subprocess.Popen([sys.executable, '-u', '-m', 'automation.pdd.spider.android',
                                        '--serve', '--serial', self.serial],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                       start_new_session=os.name != 'nt', env=env)
        print('[Android] 正在启动可复用的后台 UiAutomator 连接服务', flush=True)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self.ping():
                return
            if process.poll() is not None:
                raise RuntimeError(f'后台原生服务启动失败，请查看 {log}')
            time.sleep(.5)
        raise RuntimeError(f'后台原生服务启动超时，请查看 {log}')

    def serve(self):
        os.environ['ADBUTILS_ADB_PATH'] = self.adb
        import uiautomator2 as u2
        device = u2.connect(self.serial, port=9008)
        device.info
        print(f'[Android] 后台服务保留连接：{self.serial}', flush=True)
        failures = 0
        while True:
            time.sleep(5)
            failures = 0 if self.ping() else failures + 1
            if failures >= 3:
                print('[Android] 设备连接已断开，后台连接服务退出', flush=True)
                return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='一键准备安卓拼多多 App：复用 ADB、UiAutomator 和端口转发；退出后保留连接')
    parser.add_argument('--serial', help='多设备时指定 adb devices 中的设备序列号')
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        connection = AndroidConnection(args.serial)
        if args.serve:
            return connection.serve()
        connection.prepare()
        if connection.device.app_current().get('package') != connection.package:
            connection.device.app_start(connection.package, stop=False)
        print(f"[Android] 其他本机脚本可复用：uiautomator2.connect('{connection.serial}', port=9008)")
        print('[Android] 打开商品后运行：uv run python -m automation.pdd.spider.product --android --current')
        return 0
    except Exception as error:
        print(f'[Android] 准备失败：{error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
