"""Offline regressions for top detection, controlled motion and snapshot reuse."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from automation.pdd.spider import ios
from automation.pdd.spider.collectors import IosElementRanking, RankingItem
from automation.pdd.spider.runtime import Runtime
from automation.pdd.spider.workflow import FatalWorkflowError


def page(badge_y=300):
    return (f'<XCUIElementTypeApplication x="0" y="0" width="414" height="896">'
            '<XCUIElementTypeNavigationBar label="猫零食畅销榜"/>'
            f'<XCUIElementTypeOther x="10" y="{badge_y}" width="30" height="30"/>'
            f'<XCUIElementTypeStaticText label="热卖指数123" x="100" y="{badge_y}" width="120" height="20"/>'
            f'<XCUIElementTypeStaticText label="目标商品的完整测试标题内容" x="100" y="{badge_y + 40}" width="220" height="30"/>'
            '</XCUIElementTypeApplication>')


class MotionTests(unittest.TestCase):
    def test_unsupported_drag_falls_back_once_and_keeps_small_coordinates(self):
        client = Mock()
        client._session_http.post.side_effect = RuntimeError("unknown command: Unhandled endpoint /wda/drag")
        self.assertTrue(ios._swipe(client, 207.5, 450.2, 207.5, 400.1, duration=0.6))
        self.assertTrue(ios._swipe(client, 207, 400, 207, 350, duration=0.6))
        self.assertEqual(client._session_http.post.call_count, 1)
        self.assertEqual(client.swipe.call_count, 2)
        self.assertEqual(client.swipe.call_args_list[0].args, (207, 450, 207, 400))

    def test_timeout_does_not_attempt_second_gesture(self):
        client = Mock()
        client._session_http.post.side_effect = TimeoutError("response timed out")
        self.assertFalse(ios._swipe(client, 207, 450, 207, 400))
        client.swipe.assert_not_called()

    def test_unsupported_legacy_interface_is_fatal_and_keeps_original_error(self):
        client = Mock()
        client._session_http.post.side_effect = RuntimeError("unknown command /wda/drag")
        original = RuntimeError("Unhandled endpoint /wda/dragfromtoforduration")
        client.swipe.side_effect = original
        with self.assertRaisesRegex(FatalWorkflowError, "兼容回退失败") as caught:
            ios._swipe(client, 207, 450, 207, 400)
        self.assertIs(caught.exception.__cause__, original)

    def test_visible_first_card_skips_top_drag_without_known_title(self):
        xml = page()
        with patch.object(ios, "_get_window_size", return_value=(414, 896)), \
                patch.object(ios, "_current_page_source") as read, \
                patch.object(ios, "_swipe") as drag:
            self.assertEqual(ios._scroll_to_top(Mock(), initial_xml=xml), xml)
        read.assert_not_called()
        drag.assert_not_called()

    def test_stalled_top_stops_after_one_drag(self):
        xml = page(-500)
        with patch.object(ios, "_get_window_size", return_value=(414, 896)), \
                patch.object(ios, "_current_page_source", return_value=xml), \
                patch.object(ios, "_swipe", return_value=True) as drag, \
                patch.object(ios.time, "sleep"):
            ios._scroll_to_top(Mock(), initial_xml=xml)
        self.assertEqual(drag.call_count, 1)

    def test_occluded_badge_requires_small_correction_before_accepting(self):
        with patch.object(ios, "_get_window_size", return_value=(414, 896)), \
                patch.object(ios, "_current_page_source", side_effect=[page(250), page(360)]), \
                patch.object(ios, "_swipe", return_value=True) as drag, \
                patch.object(ios.time, "sleep"):
            result = ios._locate_card_centered(Mock(), "目标商品的完整测试标题内容", safe_top=320)
        self.assertEqual(result, (210, 415))
        self.assertEqual(drag.call_count, 1)
        args = drag.call_args.args
        self.assertGreater(args[4] - args[2], 0)
        self.assertLessEqual(abs(args[4] - args[2]), 896 * 0.12)

    def test_drag_uses_velocity_with_integer_coordinates(self):
        client = Mock()
        self.assertTrue(ios._swipe(client, 207.5, 450.2, 207.5, 400.1))
        payload = client._session_http.post.call_args.kwargs["data"]
        self.assertEqual(payload["velocity"], 220)
        self.assertEqual(payload["toY"], 400)
        client.swipe.assert_not_called()

    def test_source_configures_session_once_before_first_snapshot(self):
        client = Mock()
        client.http.post.return_value.sessionId = "fast-session"
        client.source.return_value = page()
        ios._current_page_source(client)
        ios._current_page_source(client)
        self.assertEqual(client.http.post.call_count, 1)
        self.assertEqual(client.session_id, "fast-session")
        caps = client.http.post.call_args.args[1]["capabilities"]["alwaysMatch"]
        self.assertFalse(caps["shouldWaitForQuiescence"])

    def test_top_snapshot_is_available_to_next_data_step(self):
        collector = IosElementRanking(Runtime())
        collector.runtime.client = Mock()
        collector._rank_source = "before"
        with patch.object(ios, "_scroll_to_top", return_value="after") as top:
            collector.scroll_top(RankingItem(""))
        self.assertEqual(top.call_args.kwargs["initial_xml"], "before")
        self.assertEqual(collector._rank_source, "after")

    def test_source_recovery_reapplies_idle_settings_and_session_id(self):
        client = Mock()
        client._source_configured = True
        client.http.post.return_value.sessionId = 'recovered-session'
        client.source.side_effect = [TimeoutError('slow page'), page()]
        self.assertEqual(ios._current_page_source(client), page())
        self.assertEqual(client.session_id, 'recovered-session')
        client._session_http.post.assert_called_once_with('/appium/settings', {'settings': {
            'waitForIdleTimeout': 0, 'animationCoolOffTimeout': 0,
        }})


if __name__ == "__main__":
    unittest.main()
