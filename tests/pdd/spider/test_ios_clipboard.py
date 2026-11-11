import base64
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, call

from automation.pdd.spider import ios


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        sleep = patch.object(ios.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        bundle = patch.object(ios, "wda_bundle_id", return_value=ios._WDA_BUNDLE_ID)
        bundle.start()
        self.addCleanup(bundle.stop)

    def client(self, values):
        client = Mock()
        client._session_http.post.side_effect = [Mock(value=base64.b64encode(v.encode()).decode())
                                                 for v in values]
        client.http.get.return_value.value = {"bundleId": "com.xunmeng.pinduoduo"}
        return client

    def test_background_read_needs_no_switch(self):
        with patch.object(ios, "_activate_clipboard_app") as foreground:
            self.assertEqual(ios._read_clipboard(self.client(["link"])), "link")
            foreground.assert_not_called()

    def test_url_only_clipboard_is_read_after_plaintext_is_empty(self):
        client = self.client(["", "", "", "", "https://example.com/goods"])
        with patch.object(ios, "_activate_clipboard_app"), \
                patch.object(ios, "_wait_foreground", return_value=True), \
                patch.object(ios, "_allow_clipboard_paste", return_value=False), \
                patch.object(ios, "_restore_foreground"):
            self.assertEqual(ios._read_clipboard(client), "https://example.com/goods")
        self.assertEqual(client._session_http.post.call_args.args[1], {"contentType": "url"})

    def test_copy_extracts_url_from_share_text(self):
        with patch.object(ios, "_find_center", return_value=(100, 100)), \
                patch.object(ios, "_tap_xy", return_value=True), \
                patch.object(ios, "_read_clipboard", return_value="商品分享 https://example.com/goods 。"):
            self.assertEqual(ios._copy_product_link(Mock()), "https://example.com/goods")

    def test_foreground_read_restores_original_app(self):
        client = self.client(["", "link"])
        with patch.object(ios, "_activate_clipboard_app") as foreground, \
                patch.object(ios, "_wait_foreground", return_value=True), \
                patch.object(ios, "_restore_foreground") as restore, \
                patch.object(ios, "wda_bundle_id", return_value="app.libra9491.hedgehog124"):
            self.assertEqual(ios._read_clipboard(client), "link")
            self.assertEqual(foreground.call_args_list,
                             [call(client, "app.libra9491.hedgehog124")])
            restore.assert_called_once_with(client, "com.xunmeng.pinduoduo")
            client.app_launch.assert_not_called()

    def test_launch_timeout_still_restores_app(self):
        client = self.client([""])
        with patch.object(ios, "_activate_clipboard_app", side_effect=
                subprocess.TimeoutExpired("launch", 12)), \
                patch.object(ios, "_restore_foreground") as restore:
            self.assertEqual(ios._read_clipboard(client), "")
            restore.assert_called_once_with(client, "com.xunmeng.pinduoduo")

    def test_restore_failure_stops_collection(self):
        with patch.object(ios, "_activate_clipboard_app"), \
                patch.object(ios, "_wait_foreground", return_value=True), \
                patch.object(ios, "_restore_foreground", side_effect=RuntimeError("restore")):
            with self.assertRaisesRegex(RuntimeError, "无法恢复"):
                ios._read_clipboard(self.client(["", "link"]))

    def test_restore_verifies_and_falls_back_when_activation_does_nothing(self):
        client = Mock()
        with patch.object(ios, "_wait_foreground", side_effect=[False, True]), \
                patch.object(ios, "_foreground_app") as foreground:
            ios._restore_foreground(client, "pdd")
            client._session_http.post.assert_called_once_with(
                "/wda/apps/activate", {"bundleId": "pdd"}, timeout=5)
            foreground.assert_called_once_with("pdd")

    def test_unconfirmed_restore_is_an_error(self):
        with patch.object(ios, "_wait_foreground", return_value=False), \
                patch.object(ios, "_foreground_app"):
            with self.assertRaises(RuntimeError):
                ios._restore_foreground(Mock(), "pdd")

    def test_empty_clipboard_checks_alert_without_scanning_page(self):
        client = self.client(["", ""])
        with patch.object(ios, "_activate_clipboard_app"), \
                patch.object(ios, "_wait_foreground", return_value=True), \
                patch.object(ios, "_restore_foreground") as restore, \
                patch.object(ios, "_allow_clipboard_paste", return_value=False), \
                patch.object(ios, "_find_center") as find:
            self.assertEqual(ios._read_clipboard(client), "")
            restore.assert_called_once()
            find.assert_not_called()

    def test_external_launch_is_bounded(self):
        with patch.object(ios.subprocess, "run", return_value=Mock(returncode=0)) as run:
            ios._foreground_app("bundle")
            self.assertEqual(run.call_args.args[0], [str(ios.IOS_EXE), "launch", "bundle"])
            self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_empty_frontmost_read_retries_before_restoring(self):
        client = self.client(["", "", "https://example.com/goods"])
        with patch.object(ios, "_activate_clipboard_app"), \
                patch.object(ios, "_wait_foreground", return_value=True), \
                patch.object(ios, "_allow_clipboard_paste", return_value=False), \
                patch.object(ios, "_restore_foreground") as restore:
            self.assertEqual(ios._read_clipboard(client), "https://example.com/goods")
            self.assertEqual(client._session_http.post.call_count, 3)
            self.assertEqual(client._session_http.post.call_args.args[1], {"contentType": "plaintext"})
            restore.assert_called_once()

    def test_copy_again_on_empty_clipboard(self):
        with patch.object(ios, "_find_center", return_value=(100, 100)), \
                patch.object(ios, "_tap_xy", return_value=True), \
                patch.object(ios, "_read_clipboard", side_effect=["", "https://example.com/goods"]) as read:
            self.assertEqual(ios._copy_product_link(Mock()), "https://example.com/goods")
            self.assertEqual(read.call_count, 2)

    def test_repeated_empty_copy_is_not_a_success(self):
        with patch.object(ios, "_find_center", return_value=(100, 100)), \
                patch.object(ios, "_tap_xy", return_value=True), \
                patch.object(ios, "_read_clipboard", return_value="") as read:
            self.assertEqual(ios._copy_product_link(Mock()), "")
            self.assertEqual(read.call_count, 2)

    def test_frontmost_info_must_match_application_state_twice(self):
        client = Mock()
        client.http.get.return_value.value = {"bundleId": ios.PDD_BUNDLE_ID}
        client._session_http.post.side_effect = [Mock(value=3), Mock(value=4), Mock(value=4)]
        with patch.object(ios.time, "sleep"):
            self.assertTrue(ios._wait_foreground(client, ios.PDD_BUNDLE_ID))
        self.assertEqual(client.http.get.call_count, 3)
        self.assertTrue(all(c.args[0] == "/wda/activeAppInfo" for c in client.http.get.call_args_list))

    def test_transient_foreground_does_not_count_as_restored(self):
        client = Mock()
        client.http.get.side_effect = [
            Mock(value={"bundleId": ios.PDD_BUNDLE_ID}),
            Mock(value={"bundleId": ios._WDA_BUNDLE_ID}),
            Mock(value={"bundleId": ios.PDD_BUNDLE_ID}),
        ]
        client._session_http.post.return_value.value = 4
        with patch.object(ios.time, "sleep"):
            self.assertFalse(ios._wait_foreground(client, ios.PDD_BUNDLE_ID, attempts=3))

    def test_delayed_wda_activation_is_given_enough_time(self):
        client = Mock()
        client.http.get.side_effect = [Mock(value={"bundleId": bundle}) for bundle in
                                      [ios.PDD_BUNDLE_ID] * 3 + [ios._WDA_BUNDLE_ID] * 2]
        with patch.object(ios.time, "sleep"):
            self.assertTrue(ios._wait_foreground(client, ios._WDA_BUNDLE_ID))


if __name__ == "__main__":
    unittest.main()


def test_activation_retries_instruments_timeout():
    from unittest.mock import Mock, patch
    from automation.pdd.spider import ios
    with patch.object(ios.subprocess, 'run', side_effect=[
            Mock(returncode=1, stderr='Timed out waiting for response for message:1 channel:1', stdout=''),
            Mock(returncode=0)]) as run, patch.object(ios.time, 'sleep') as sleep:
        ios._foreground_app('wda.bundle')
    assert run.call_count == 2
    sleep.assert_any_call(2)


def test_activation_timeout_retries_are_bounded():
    import subprocess
    import pytest
    from unittest.mock import patch
    from automation.pdd.spider import ios
    with patch.object(ios.subprocess, 'run', side_effect=subprocess.TimeoutExpired('launch', 30)) as run, \
            patch.object(ios.time, 'sleep'), pytest.raises(RuntimeError, match='超时'):
        ios._foreground_app('wda.bundle')
    assert run.call_count == 3


def test_activation_does_not_retry_missing_app():
    import pytest
    from unittest.mock import Mock, patch
    from automation.pdd.spider import ios
    with patch.object(ios.subprocess, 'run', return_value=Mock(returncode=1, stderr='Application not found', stdout='')) as run, \
            pytest.raises(RuntimeError, match='Application not found'):
        ios._foreground_app('wda.bundle')
    run.assert_called_once()


def test_clipboard_activation_uses_wda_before_go_ios():
    with patch.object(ios, '_wait_foreground', return_value=True), \
            patch.object(ios, '_foreground_app') as launch, patch.object(ios.time, 'sleep'):
        client = Mock()
        ios._activate_clipboard_app(client, 'wda.bundle')
    client._session_http.post.assert_called_once_with('/wda/apps/activate', {'bundleId': 'wda.bundle'}, timeout=5)
    launch.assert_not_called()
