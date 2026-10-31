import unittest
from unittest.mock import AsyncMock, patch

from automation.pdd.login import select_login_tab, submit_login
from automation.com.extension_bridge import BridgeError
from automation.pdd.credentials import ShopCredential


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_pinduoduo_even_when_netease_is_open(self):
        tabs = {"tabs": [{"tab_id": 1, "url": "https://dun.163.com/"}, {"tab_id": 2, "url": "https://mms.pinduoduo.com/login/"}]}
        with patch("automation.pdd.login.request", new=AsyncMock(return_value=tabs)):
            self.assertEqual(await select_login_tab("ws://127.0.0.1:8765/"), 2)

    async def test_uncertain_submit_is_not_repeated(self):
        mock = AsyncMock(side_effect=[BridgeError("login_submission_outcome_unknown"), {"status": "logged_in"}])
        with patch("automation.pdd.login.request", mock):
            result = await submit_login("ws://127.0.0.1:8765/", 2, ShopCredential("fixture", "fake-account", "fake-password"))
        self.assertEqual(result, {"status": "logged_in", "submitted": None})
        self.assertEqual([call.args[1] for call in mock.call_args_list], ["login_submit", "login_status"])

    async def test_verification_stops_without_further_actions(self):
        mock = AsyncMock(return_value={"status": "verification_required", "submitted": False})
        with patch("automation.pdd.login.request", mock):
            result = await submit_login("ws://127.0.0.1:8765/", 2, ShopCredential("fixture", "fake-account", "fake-password"))
        self.assertEqual(result["status"], "verification_required")
        self.assertEqual(mock.call_count, 1)
