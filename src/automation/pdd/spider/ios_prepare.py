"""Standalone USB device preparation and WDA signing."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from zipfile import ZipFile, ZIP_DEFLATED

from automation.pdd.spider import config, ios
from automation.pdd.spider.runtime import Runtime


def select_device(output, requested=''):
    devices = []
    def visit(value, device_list=False):
        if isinstance(value, str) and device_list and re.fullmatch(r'[A-Za-z0-9-]{16,}', value):
            devices.append(value)
        elif isinstance(value, list):
            for item in value:
                visit(item, device_list)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() == 'udid' and isinstance(item, str):
                    devices.append(item)
                elif key.casefold() in {'devicelist', 'devices'}:
                    visit(item, True)
    for line in output.splitlines():
        try:
            visit(json.loads(line))
        except ValueError:
            continue
    devices = list(dict.fromkeys(devices))
    if requested:
        if requested not in devices:
            raise RuntimeError(f'未检测到指定 USB 设备：{requested}')
        return requested
    if len(devices) != 1:
        raise RuntimeError('请连接并解锁一台 iOS 设备；多设备时使用 --udid 指定设备')
    return devices[0]


def signing_zip(directory, udid):
    pattern = re.compile(r'(?<![a-z0-9])' + re.escape(udid) + r'(?![a-z0-9])', re.I)
    candidates = [path for path in directory.glob('*.zip') if pattern.search(path.stem)]
    if len(candidates) != 1:
        raise RuntimeError(f'请在 {directory} 放置唯一的 certificate_{udid}.zip，内含一个 .p12 和一个 .mobileprovision')
    return candidates[0]


def read_signing(package, udid, target):
    with ZipFile(package) as archive:
        entries = [item for item in archive.infolist() if '__MACOSX' not in item.filename.split('/')
                   and not Path(item.filename).name.startswith('._')]
        keys = [item for item in entries if item.filename.lower().endswith('.p12')]
        profiles = [item for item in entries if item.filename.lower().endswith('.mobileprovision')]
        if len(keys) != 1 or len(profiles) != 1:
            raise RuntimeError('证书 ZIP 必须包含一个 .p12 和一个 .mobileprovision')
        if any(item.file_size > 16 * 1024 * 1024 for item in (keys[0], profiles[0])):
            raise RuntimeError('证书文件过大')
        key_bytes, profile_bytes = archive.read(keys[0]), archive.read(profiles[0])
    start, end = profile_bytes.find(b'<?xml'), profile_bytes.find(b'</plist>')
    if start < 0 or end < start:
        raise RuntimeError('无法解析描述文件中的 plist')
    profile = plistlib.loads(profile_bytes[start:end + len(b'</plist>')])
    expiry = profile.get('ExpirationDate')
    if not isinstance(expiry, datetime) or expiry.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise RuntimeError('描述文件已过期或缺少有效期')
    if not profile.get('ProvisionsAllDevices') and udid not in profile.get('ProvisionedDevices', []):
        raise RuntimeError('描述文件未授权当前设备 UDID')
    app_id = profile.get('Entitlements', {}).get('application-identifier', '')
    bundle = app_id.partition('.')[2]
    if not bundle or '*' in bundle:
        raise RuntimeError('描述文件必须包含明确的 application-identifier，不能使用通配符')
    key, provision = target / 'certificate.p12', target / 'profile.mobileprovision'
    key.write_bytes(key_bytes)
    provision.write_bytes(profile_bytes)
    return key, provision, bundle


def package_wda(source, output):
    required = ('Info.plist', 'WebDriverAgentRunner-Runner', 'PlugIns/WebDriverAgentRunner.xctest/Info.plist')
    if not all((source / name).is_file() for name in required):
        raise RuntimeError(f'WDA 预编译包不完整：{source}；Windows 不能从源码编译，请提供预编译 .app')
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                archive.write(path, 'Payload/WebDriverAgentRunner-Runner.app/' + path.relative_to(source).as_posix())


def sign_and_install(runtime, udid, version, password=None):
    tools = config.TOOLS_ROOT
    package = signing_zip(tools / 'ios/signing', udid)
    source = tools / f'ios/archive/WebDriverAgentRunner-Runner.app.v{version}'
    executable = tools / 'vendor/zsign' / ('zsign.exe' if os.name == 'nt' else 'zsign')
    if not executable.is_file():
        raise RuntimeError(f'找不到签名工具：{executable}')
    destination = tools / f'releases/wda/wda-{udid}-v{version}.ipa'
    with tempfile.TemporaryDirectory(prefix='wda-prepare-') as folder:
        temp = Path(folder)
        key, provision, bundle = read_signing(package, udid, temp)
        configured = os.getenv('WDA_BUNDLE_ID', '').strip()
        if configured and configured != bundle:
            raise RuntimeError('WDA_BUNDLE_ID 与证书描述文件不一致，请修正业务 .env')
        unsigned, signed = temp / 'unsigned.ipa', temp / 'signed.ipa'
        print(f'[ios] 打包预编译 WDA {version}，为设备 {udid} 签名')
        package_wda(source, unsigned)
        if password is None:
            password = getpass.getpass('P12 密码（输入不显示）：')
        # Do not print the command or signing output: these may contain the password.
        try:
            result = subprocess.run([str(executable), '-f', '-k', str(key), '-p', password,
                                     '-m', str(provision), '-b', bundle, '-o', str(signed), str(unsigned)],
                                    cwd=temp, capture_output=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError):
            raise RuntimeError('签名工具启动失败或超时，请检查 zsign') from None
        if result.returncode or not signed.is_file():
            raise RuntimeError(f'WDA 签名失败，退出码 {result.returncode}；请检查证书、P12 密码与预编译包')
        with ZipFile(signed) as archive:
            profiles = [name for name in archive.namelist() if re.fullmatch(r'Payload/[^/]+\.app/embedded\.mobileprovision', name)]
            if len(profiles) != 1 or archive.read(profiles[0]) != provision.read_bytes():
                raise RuntimeError('签名产物的描述文件验证失败')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.copy2(destination, destination.with_suffix('.ipa.backup'))
        shutil.copy2(signed, destination)
    print('[ios] 安装签名 WDA...')
    runtime._ios_command('install', f'--path={destination}', timeout=300, stream=True)
    apps = runtime._ios_command('apps', '--list')
    if not any(line.split() and line.split()[0] == bundle for line in apps.splitlines()):
        raise RuntimeError('安装命令结束，但设备应用列表中未找到签名 WDA')
    ios._DETECTED_WDA_BUNDLE_ID = bundle
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description='准备 USB iOS 设备、WDA 打包签名安装、隧道、镜像、驱动及转发；保持运行供采集复用')
    parser.add_argument('--udid', default=os.getenv('SPIDER_IOS_UDID', ''), help='多台设备时指定 UDID')
    parser.add_argument('--wda-version', default=os.getenv('SPIDER_WDA_VERSION', '16.15.3'), help='预编译 WDA archive 版本')
    parser.add_argument('--reinstall', action='store_true', help='重新打包签名并安装 WDA')
    options = parser.parse_args(argv)
    if not re.fullmatch(r'\d+\.\d+\.\d+', options.wda_version):
        parser.error('wda-version 必须为数字版本，例如 16.15.3')
    from automation.pdd.spider.initialize import initialize
    from automation.pdd.spider.logging_setup import console_logging
    from automation.pdd.spider.concurrency import workflow_lock
    initialize(config.BUSINESS_ROOT)
    runtime = Runtime()
    previous = os.environ.get('SPIDER_IOS_UDID')
    try:
        with console_logging(config.BUSINESS_ROOT):
            # Lock only while preparing; ranking --ios must be able to reuse it later.
            with workflow_lock('device-ios'):
                udid = select_device(runtime._ios_command('list'), options.udid)
                os.environ['SPIDER_IOS_UDID'] = udid
                print(f'[ios] 当前设备：{udid}')
                apps = runtime._ios_command('apps', '--list')
                installed = list(dict.fromkeys(match.group(1) for line in apps.splitlines()
                    if (match := re.match(r'^\s*(\S+)\s+(\S*WebDriverAgent\S*)', line))))
                configured = os.getenv('WDA_BUNDLE_ID', '').strip()
                if configured:
                    installed = [configured] if any(line.split() and line.split()[0] == configured for line in apps.splitlines()) else []
                if options.reinstall or not installed:
                    sign_and_install(runtime, udid, options.wda_version, os.getenv('SPIDER_P12_PASSWORD'))
                elif len(installed) == 1:
                    ios._DETECTED_WDA_BUNDLE_ID = installed[0]
                    print(f'[ios] 复用已安装 WDA：{installed[0]}')
                else:
                    raise RuntimeError('设备上有多个 WDA，请在业务 .env 指定 WDA_BUNDLE_ID')
                runtime.ensure_tunnel()
                runtime.ensure_image()
                runtime.ensure_driver()
                runtime.ensure_forward()
            print(f'[ios] 已就绪：{ios.WDA_URL}；保持本命令运行，Ctrl+C 停止本次启动的服务')
            while True:
                ios._WDA_PROCESSES.check()
                time.sleep(1)
    except KeyboardInterrupt:
        print('[ios] 已停止')
        return 0
    except Exception as error:
        print(f'[ios] 准备失败：{error}')
        return 1
    finally:
        runtime.close()
        if previous is None:
            os.environ.pop('SPIDER_IOS_UDID', None)
        else:
            os.environ['SPIDER_IOS_UDID'] = previous
