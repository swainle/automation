from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

import pytest

from automation.pdd.spider import config, ios
from automation.pdd.spider import ios_prepare as prepare


UDID = '00008030-0011223344556677'


def certificate(path, udid=UDID, expired=False):
    profile = plistlib.dumps({
        'ExpirationDate': datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=-1 if expired else 30),
        'ProvisionedDevices': [udid],
        'Entitlements': {'application-identifier': 'TEAM.dev.example.wda'},
    })
    with ZipFile(path, 'w') as archive:
        archive.writestr('../../certificate.p12', b'private fixture')
        archive.writestr('nested/profile.mobileprovision', b'CMS header' + profile + b'CMS footer')
    return b'CMS header' + profile + b'CMS footer'


def test_device_selection_requires_unambiguous_connected_device():
    assert prepare.select_device(json.dumps({'deviceList': [UDID]})) == UDID
    output = json.dumps({'deviceList': [UDID, '00008030-8899001122334455']})
    with pytest.raises(RuntimeError, match='多设备'):
        prepare.select_device(output)
    assert prepare.select_device(output, UDID) == UDID
    with pytest.raises(RuntimeError, match='指定 USB'):
        prepare.select_device('error', UDID)


def test_certificate_selection_requires_exact_udid_and_unique_package(tmp_path):
    (tmp_path / f'certificate_{UDID}.zip').touch()
    (tmp_path / f'certificate_{UDID}AA.zip').touch()
    assert prepare.signing_zip(tmp_path, UDID).name == f'certificate_{UDID}.zip'
    (tmp_path / f'other_{UDID}.zip').touch()
    with pytest.raises(RuntimeError, match='唯一'):
        prepare.signing_zip(tmp_path, UDID)


def test_signing_files_are_extracted_to_fixed_names_and_device_is_validated(tmp_path):
    package = tmp_path / 'certificate.zip'
    certificate(package)
    target = tmp_path / 'private'
    target.mkdir()
    key, profile, bundle = prepare.read_signing(package, UDID, target)
    assert key == target / 'certificate.p12'
    assert profile == target / 'profile.mobileprovision'
    assert bundle == 'dev.example.wda'
    with pytest.raises(RuntimeError, match='未授权'):
        prepare.read_signing(package, 'wrong-device', target)


def test_expired_profile_is_rejected_before_private_key_is_written(tmp_path):
    package = tmp_path / 'certificate.zip'
    certificate(package, expired=True)
    with pytest.raises(RuntimeError, match='过期'):
        prepare.read_signing(package, UDID, tmp_path)
    assert not (tmp_path / 'certificate.p12').exists()


def test_sign_pack_install_verifies_embedded_profile_and_installed_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv('WDA_BUNDLE_ID', raising=False)
    signing = tmp_path / 'ios/signing'
    signing.mkdir(parents=True)
    profile = certificate(signing / f'certificate_{UDID}.zip')
    source = tmp_path / 'ios/archive/WebDriverAgentRunner-Runner.app.v16.15.3'
    for name in ('Info.plist', 'WebDriverAgentRunner-Runner', 'PlugIns/WebDriverAgentRunner.xctest/Info.plist'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
    executable = tmp_path / 'vendor/zsign' / ('zsign.exe' if os.name == 'nt' else 'zsign')
    executable.parent.mkdir(parents=True)
    executable.touch()
    def sign(command, **kwargs):
        with ZipFile(command[-1]) as unsigned:
            assert 'Payload/WebDriverAgentRunner-Runner.app/Info.plist' in unsigned.namelist()
        with ZipFile(command[command.index('-o') + 1], 'w') as signed:
            signed.writestr('Payload/WebDriverAgentRunner-Runner.app/embedded.mobileprovision', profile)
        return SimpleNamespace(returncode=0)
    runtime = Mock()
    runtime._ios_command.side_effect = ['', 'dev.example.wda WebDriverAgentRunner-Runner 1.0']
    with patch.object(config, 'TOOLS_ROOT', tmp_path), patch.object(prepare.subprocess, 'run', side_effect=sign), \
            patch.object(ios, '_DETECTED_WDA_BUNDLE_ID', None):
        assert prepare.sign_and_install(runtime, UDID, '16.15.3', 'fixture-password') == 'dev.example.wda'
        assert ios._DETECTED_WDA_BUNDLE_ID == 'dev.example.wda'
    assert runtime._ios_command.call_args_list[0].args[0] == 'install'
    assert not list(tmp_path.rglob('certificate.p12'))


def test_preparation_reuses_installed_wda_and_runs_all_services_in_order(tmp_path, monkeypatch):
    monkeypatch.delenv('WDA_BUNDLE_ID', raising=False)
    runtime = Mock()
    runtime._ios_command.side_effect = [json.dumps({'deviceList': [UDID]}), 'dev.example.wda WebDriverAgentRunner-Runner 1.0']
    with patch.object(config, 'BUSINESS_ROOT', tmp_path), patch.object(prepare, 'Runtime', return_value=runtime), \
            patch.object(prepare, 'sign_and_install') as install, patch.object(prepare.time, 'sleep', side_effect=KeyboardInterrupt), \
            patch.object(ios._WDA_PROCESSES, 'check'), patch.object(ios, '_DETECTED_WDA_BUNDLE_ID', None):
        assert ios.main([]) == 0
    install.assert_not_called()
    calls = [call[0] for call in runtime.mock_calls]
    assert calls[-5:] == ['ensure_tunnel', 'ensure_image', 'ensure_driver', 'ensure_forward', 'close']


def test_selected_device_is_used_for_commands_and_forwarding(monkeypatch):
    monkeypatch.setenv('SPIDER_IOS_UDID', UDID)
    assert ios.device_args(['forward', '8100', '8100'])[-1] == f'--udid={UDID}'
    assert ios.device_args(['list']) == ['list']
    assert ios.device_args(['tunnel', 'start']) == ['tunnel', 'start']
    with patch.object(ios, '_ios', return_value='{"udid":"other-device","rsdPort":1234}'):
        assert not ios.tunnel_up()


def test_help_does_not_touch_device_or_files():
    with patch.object(prepare, 'Runtime') as runtime, pytest.raises(SystemExit) as result:
        ios.main(['--help'])
    assert result.value.code == 0
    runtime.assert_not_called()
