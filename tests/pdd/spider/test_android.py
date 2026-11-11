from unittest.mock import Mock, patch

import pytest

from automation.pdd.spider.android import AndroidConnection
from automation.pdd.spider.product_android import AndroidRuntime


def connection(tmp_path, monkeypatch):
    adb = tmp_path / 'adb.exe'
    adb.touch()
    monkeypatch.setenv('SPIDER_ADB_EXE', str(adb))
    monkeypatch.delenv('SPIDER_ANDROID_SERIAL', raising=False)
    monkeypatch.setattr('automation.pdd.spider.android.config.BUSINESS_ROOT', tmp_path)
    return AndroidConnection()


def test_android_reuses_native_service_and_existing_forward(tmp_path, monkeypatch):
    value = connection(tmp_path, monkeypatch)
    with patch.object(value, 'command', side_effect=[
            'serial device', 'package:/pdd.apk', f'serial tcp:{value.port} tcp:9008']) as command, \
            patch.object(value, 'ping', return_value=True), \
            patch('uiautomator2.connect') as connect:
        assert value.prepare() == f'http://127.0.0.1:{value.port}'
    connect.assert_called_once_with('serial', port=9008)
    assert not any('--no-rebind' in call.args or 'kill-server' in call.args for call in command.call_args_list)


def test_background_service_is_started_only_if_shared_endpoint_is_missing(tmp_path, monkeypatch):
    value = connection(tmp_path, monkeypatch)
    value.serial = 'serial'
    with patch.object(value, 'ping', side_effect=[False, True]), \
            patch('automation.pdd.spider.android.subprocess.Popen') as spawn:
        value.ensure_service()
    assert '--serve' in spawn.call_args.args[0]
    assert spawn.call_args.kwargs['env']['SPIDER_ADB_EXE'] == value.adb
    with patch.object(value, 'ping', return_value=True), \
            patch('automation.pdd.spider.android.subprocess.Popen') as spawn:
        value.ensure_service()
    spawn.assert_not_called()


@pytest.mark.parametrize('devices', ['serial unauthorized', 'a device\nb device', ''])
def test_android_requires_unambiguous_authorized_device(tmp_path, monkeypatch, devices):
    value = connection(tmp_path, monkeypatch)
    with patch.object(value, 'command', return_value=devices), pytest.raises(RuntimeError, match='授权'):
        value.prepare()


def test_foreign_forward_is_not_overwritten(tmp_path, monkeypatch):
    value = connection(tmp_path, monkeypatch)
    with patch('uiautomator2.connect'), patch.object(value, 'command', side_effect=[
            'serial device', 'package:/pdd.apk',
            f'other tcp:{value.port} localabstract:chrome_devtools_remote']) as command, \
            pytest.raises(RuntimeError, match='其他设备'):
        value.prepare()
    assert not any('--no-rebind' in call.args for call in command.call_args_list)


def test_native_runtime_reuses_connection_and_does_not_stop_device():
    runtime = AndroidRuntime()
    with patch('automation.pdd.spider.android.AndroidConnection') as connect:
        first = runtime.ensure_android()
        assert runtime.ensure_android() is first
        connect.assert_called_once()
        runtime.close()
        first.app_stop.assert_not_called()
        first.uiautomator.stop.assert_not_called()


def test_product_android_flag_dispatches_without_browser_start():
    from automation.pdd.spider import product
    with patch('automation.pdd.spider.commands.execute', return_value=0) as execute:
        assert product.main(['--android']) == 0
    assert execute.call_args.kwargs['args'].android


def test_android_help_has_no_device_side_effects():
    from automation.pdd.spider import android
    with patch.object(android, 'AndroidConnection') as connect, pytest.raises(SystemExit):
        android.main(['--help'])
    connect.assert_not_called()
