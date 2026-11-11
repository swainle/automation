from pathlib import Path
import os
import sys
import unittest
from unittest.mock import Mock, patch

from automation.pdd.spider import ios
from automation.pdd.spider.collectors import IosElementRanking
from automation.pdd.spider.runtime import Runtime
from automation.pdd.spider.workflow import parse_flow


MOUNTED = '{"msg":"image signature","signature":"mounted-signature"}'


class WdaBundleIdTests(unittest.TestCase):
    def setUp(self):
        patch.object(ios, "_DETECTED_WDA_BUNDLE_ID", None).start()
        self.addCleanup(patch.stopall)

    def test_environment_variable_wins_without_probe(self):
        with patch.dict(os.environ, {"WDA_BUNDLE_ID": "env.bundle.id"}), \
                patch.object(ios, "_ios") as probe:
            self.assertEqual(ios.wda_bundle_id(), "env.bundle.id")
            probe.assert_not_called()

    def test_bundle_id_detected_once_from_app_list(self):
        output = ("app.libra9491.hedgehog124 WebDriverAgentRunner-Runner 1.0\n"
                  "com.xunmeng.pinduoduo 拼多多 6.0\n")
        with patch.object(ios, "_ios", return_value=output) as probe:
            self.assertEqual(ios.wda_bundle_id(), "app.libra9491.hedgehog124")
            self.assertEqual(ios.wda_bundle_id(), "app.libra9491.hedgehog124")
        probe.assert_called_once_with("apps", "--list")

    def test_missing_wda_falls_back_to_default(self):
        with patch.object(ios, "_ios", return_value="com.xunmeng.pinduoduo 拼多多 6.0\n"):
            self.assertEqual(ios.wda_bundle_id(), ios._WDA_BUNDLE_ID)

    def test_probe_failure_falls_back_to_default(self):
        with patch.object(ios, "_ios", side_effect=RuntimeError("no device")):
            self.assertEqual(ios.wda_bundle_id(), ios._WDA_BUNDLE_ID)

    def test_multiple_wda_apps_require_explicit_choice(self):
        output = ("com.foo.WebDriverAgentRunner.xctrunner WebDriverAgentRunner 1.0\n"
                  "app.bar.hedgehog WebDriverAgentRunner-Runner 1.0\n")
        with patch.object(ios, "_ios", return_value=output):
            with self.assertRaisesRegex(RuntimeError, "WDA_BUNDLE_ID"):
                ios.wda_bundle_id()


class IosPreparationTests(unittest.TestCase):
    def test_existing_image_is_reused_and_checked_once(self):
        runtime = Runtime()
        with patch.object(runtime, "_ios_command", return_value=MOUNTED) as command:
            runtime.ensure_image()
            runtime.ensure_image()
        command.assert_called_once_with("image", "list")

    def test_missing_image_is_mounted_with_project_cache_then_verified(self):
        runtime = Runtime()
        with patch.object(runtime, "_ios_command", side_effect=["", "download completed", MOUNTED]) as command:
            runtime.ensure_image()
        self.assertEqual(command.call_args_list[0].args, ("image", "list"))
        self.assertEqual(command.call_args_list[1].args[:2], ("image", "auto"))
        self.assertIn("devimages", command.call_args_list[1].args[2])
        self.assertEqual(command.call_args_list[1].kwargs, {"timeout": 300, "stream": True})
        self.assertEqual(command.call_args_list[2].args, ("image", "list"))

    def test_image_failure_stops_flow_before_driver_and_forward(self):
        runtime = Runtime()
        collector = IosElementRanking(runtime)
        with patch.object(runtime, "ensure_tunnel"), \
                patch.object(runtime, "_ios_command", side_effect=["", RuntimeError("mount failed")]), \
                patch.object(runtime, "ensure_driver") as driver, \
                patch.object(runtime, "ensure_forward") as forward, patch.object(runtime, "close"):
            with self.assertRaisesRegex(RuntimeError, "mount failed"):
                collector.run("ensure_tunnel,ensure_image,ensure_wda,ensure_forward")
        driver.assert_not_called()
        forward.assert_not_called()
        self.assertFalse(runtime._image_checked)

    def test_older_ios_skips_remote_pairing_tunnel(self):
        runtime = Runtime()
        with patch("automation.pdd.spider.ios.device_connected", return_value=True), \
                patch("automation.pdd.spider.ios.tunnel_up", return_value=False), \
                patch.object(runtime, "_ios_command", return_value='"16.7.1"'), \
                patch("automation.pdd.spider.ios._spawn") as spawn:
            runtime.ensure_tunnel()
        spawn.assert_not_called()

    def test_running_driver_is_reused_without_launching(self):
        runtime = Runtime()
        with patch("automation.pdd.spider.ios.wda_ready", return_value=False), \
                patch("automation.pdd.spider.ios.wda_running", return_value=True), \
                patch("automation.pdd.spider.ios._spawn") as spawn, \
                patch.object(runtime, "ensure_image") as image:
            runtime.ensure_driver()
        spawn.assert_not_called()
        image.assert_not_called()

    def test_driver_starts_runwda_with_detected_bundle_id(self):
        runtime = Runtime()
        with patch("automation.pdd.spider.ios.wda_ready", return_value=False), \
                patch("automation.pdd.spider.ios.wda_running", return_value=False), \
                patch("automation.pdd.spider.ios.wda_bundle_id", return_value="app.libra9491.hedgehog124"), \
                patch("automation.pdd.spider.ios._spawn") as spawn, \
                patch("automation.pdd.spider.ios._wait"), \
                patch.object(runtime, "_ios_command", return_value="[]"), \
                patch.object(runtime, "ensure_tunnel"), \
                patch.object(runtime, "ensure_image"):
            runtime.ensure_driver()
        self.assertEqual(spawn.call_args_list[0].args, ("runwda", [
            "runwda", "--bundleid=app.libra9491.hedgehog124",
            "--testrunnerbundleid=app.libra9491.hedgehog124",
            "--xctestconfig=WebDriverAgentRunner.xctest", "--env=USE_PORT=8100"]))

    def test_rsd_failures_stop_after_three_attempts(self):
        runtime = Runtime()
        with patch.object(ios, "wda_ready", return_value=False), \
                patch.object(ios, "wda_running", return_value=False), \
                patch.object(ios, "wda_bundle_id", return_value="wda.bundle"), \
                patch.object(runtime, "ensure_tunnel"), patch.object(runtime, "ensure_image"), \
                patch.object(runtime, "_ios_command", side_effect=RuntimeError("RSD EOF")) as probe, \
                patch("automation.pdd.spider.runtime.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "WDA 启动失败"):
                runtime.ensure_driver()
        self.assertEqual(probe.call_count, 3)

    def test_rsd_recovery_preserves_external_tunnel(self):
        for owned in (False, True):
            with self.subTest(owned=owned):
                runtime = Runtime()
                manager = Mock()
                manager.groups = {"tunnel": Mock()} if owned else {}
                with patch.object(ios, "_WDA_PROCESSES", manager), \
                        patch.object(ios, "wda_ready", return_value=False), \
                        patch.object(ios, "wda_running", return_value=False), \
                        patch.object(ios, "wda_bundle_id", return_value="wda.bundle"), \
                        patch.object(ios, "_spawn"), patch.object(ios, "_wait") as wait, \
                        patch.object(runtime, "ensure_tunnel"), patch.object(runtime, "ensure_image"), \
                        patch.object(runtime, "_ios_command", side_effect=[RuntimeError("RSD EOF"), "[]"]), \
                        patch("automation.pdd.spider.runtime.time.sleep"):
                    runtime.ensure_driver()
                    self.assertIs(wait.call_args.args[0], ios.wda_ready)
                self.assertEqual("tunnel" in [call.args[0] for call in manager.stop.call_args_list], owned)

    def test_forwarding_is_independent_from_client_initialization(self):
        runtime = Runtime()
        with patch("automation.pdd.spider.ios.wda_ready", return_value=False), \
                patch.object(runtime, "ensure_driver") as driver, \
                patch.object(runtime, "ensure_wda") as client, \
                patch("automation.pdd.spider.ios._spawn") as spawn, patch("automation.pdd.spider.ios._wait"):
            runtime.ensure_forward()
        driver.assert_called_once()
        client.assert_not_called()
        spawn.assert_called_once_with("forward", ["forward", "8100", "8100"])

    def test_automatic_client_prepares_services_in_order(self):
        runtime = Runtime()
        order = []
        with patch("automation.pdd.spider.ios.wda_ready", return_value=False), \
                patch.object(runtime, "ensure_tunnel", side_effect=lambda: order.append("tunnel")), \
                patch.object(runtime, "ensure_image", side_effect=lambda: order.append("image")), \
                patch.object(runtime, "ensure_driver", side_effect=lambda: order.append("driver")), \
                patch.object(runtime, "ensure_forward", side_effect=lambda: order.append("forward")), \
                patch("wda.Client") as client:
            self.assertIs(runtime.ensure_wda(), client.return_value)
        self.assertEqual(order, ["tunnel", "image", "driver", "forward"])

    def test_ranking_prepares_four_services_before_collecting(self):
        from tempfile import TemporaryDirectory
        from automation.pdd.spider.database import Store
        from automation.pdd.spider.ranking import Ranking
        with TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'sqlite.db')
            runtime = Mock()
            Ranking(store, runtime, type('Device', (), {'name': 'ios'})()).run([], day='2026-10-31')
            self.assertEqual([call[0] for call in runtime.method_calls],
                             ['ensure_tunnel', 'ensure_image', 'ensure_driver', 'ensure_forward', 'close'])
            store.close()


if __name__ == "__main__":
    unittest.main()
