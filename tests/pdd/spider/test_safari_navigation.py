from unittest.mock import Mock, patch

import pytest
from wda.exceptions import WDARequestError
from wda.usbmux.exceptions import HTTPError

from automation.pdd.spider import ios
from automation.pdd.spider.navigation import BoardUnavailableError, _accept_app_prompt, is_target_board, open_app_board


SOURCE = {'name': '猫粮畅销榜', 'url': 'https://example.com/rank'}
TARGET = '<root><XCUIElementTypeNavigationBar label="猫粮畅销榜" /></root>'


def client():
    value = Mock()
    value._session_http.post.return_value.value = 1
    value.session.return_value.session_id = 'safari-session'
    value.http.get.return_value.value = {'bundleId': ios.PDD_BUNDLE_ID}
    return value


def test_source_names_accept_annotations_but_reject_other_boards():
    elements = [{'type': 'NavigationBar', 'text': '猫粮好评榜'}]
    assert is_target_board(elements, '猫粮【畅销榜、好评榜】')
    assert is_target_board(elements, '猫粮畅销榜')
    assert not is_target_board(elements, '猫砂畅销榜')


def test_safari_opens_url_after_termination_and_clears_previous_snapshot():
    value = client()
    value._rank_last_elements = ['old board']
    value._source_configured = True
    with patch('automation.pdd.spider.navigation._accept_app_prompt', return_value=False), \
            patch.object(ios, '_current_page_source', return_value=TARGET):
        assert open_app_board(value, SOURCE) == TARGET
    assert [call.args[0] for call in value._session_http.post.call_args_list] == [
        '/wda/apps/terminate', '/wda/apps/state', '/appium/settings']
    value.session.assert_called_once_with(ios.SAFARI_BUNDLE_ID)
    value.session.return_value.open_url.assert_called_once_with(SOURCE['url'])
    assert value.session_id == 'safari-session'
    assert '_rank_last_elements' not in value.__dict__
    assert value._source_configured is True
    value.home.assert_not_called()


def test_safari_disables_idle_wait_before_first_alert_probe():
    value = client()
    value._source_configured = True  # Previous App session must not skip setup.

    def probe(client):
        assert client._source_configured is True
        client._session_http.post.assert_called_with('/appium/settings', {'settings': {
            'waitForIdleTimeout': 0, 'animationCoolOffTimeout': 0,
        }})
        caps = client.http.post.call_args.args[1]['capabilities']['alwaysMatch']
        assert caps['shouldWaitForQuiescence'] is False
        assert caps['waitForIdleTimeout'] == 0
        return False

    with patch('automation.pdd.spider.navigation._accept_app_prompt', side_effect=probe), \
            patch.object(ios, '_current_page_source', return_value=TARGET):
        assert open_app_board(value, SOURCE) == TARGET


@pytest.mark.parametrize('label', ['打开拼多多APP', '在App打开', '在 App 打开'])
def test_webpage_and_top_right_buttons_then_system_confirmation(label):
    value = client()
    order = Mock()
    order.attach_mock(value._session_http.post, 'stop')
    order.attach_mock(value.session, 'session')
    value.http.get.side_effect = [Mock(value={'bundleId': ios.SAFARI_BUNDLE_ID}),
                                 Mock(value={'bundleId': ios.PDD_BUNDLE_ID})]
    page = f'<root><XCUIElementTypeButton label="{label}" x="300" y="50" width="60" height="30" /></root>'
    with patch('automation.pdd.spider.navigation._accept_app_prompt', side_effect=[False, True]) as prompt, \
            patch.object(ios, '_current_page_source', side_effect=[page, TARGET]), \
            patch.object(ios, '_tap_xy', return_value=True) as tap, \
            patch('automation.pdd.spider.navigation.time.sleep'):
        assert open_app_board(value, SOURCE) == TARGET
    assert prompt.call_count == 2
    tap.assert_called_once_with(value, 330, 65, label)
    assert [call[0] for call in order.mock_calls][:4] == [
        'stop', 'stop', 'session', 'session().open_url']


def test_failed_termination_prevents_next_url():
    value = client()
    value._session_http.post.return_value.value = 4
    with pytest.raises(RuntimeError, match='未确认结束'):
        open_app_board(value, SOURCE)
    value.session.assert_not_called()


@pytest.mark.parametrize('message', ['在“拼多多”中打开此页？', 'Open this page in “拼多多”?'])
def test_safari_dialog_not_exposed_by_alert_api_is_accepted(message):
    value = client()
    value.http.get.side_effect = [Mock(value={'bundleId': ios.SAFARI_BUNDLE_ID}),
                                 Mock(value={'bundleId': ios.PDD_BUNDLE_ID})]
    page = f'''<root><XCUIElementTypeOther name="SFDialogView">
        <XCUIElementTypeTextView label="{message}" />
        <XCUIElementTypeButton label="打开" x="301" y="388" width="34" height="33" />
        </XCUIElementTypeOther>
        <XCUIElementTypeButton label="打开拼多多APP" x="10" y="100" width="200" height="40" /></root>'''
    with patch('automation.pdd.spider.navigation._accept_app_prompt', return_value=False), \
            patch.object(ios, '_current_page_source', side_effect=[page, TARGET]), \
            patch.object(ios, '_tap_xy', return_value=True) as tap, \
            patch('automation.pdd.spider.navigation.time.sleep'):
        assert open_app_board(value, SOURCE) == TARGET
    tap.assert_called_once_with(value, 318, 404.5, '打开')


def test_unrelated_safari_dialog_is_not_accepted():
    value = client()
    value.http.get.return_value.value = {'bundleId': ios.SAFARI_BUNDLE_ID}
    page = '''<root><XCUIElementTypeOther name="SFDialogView">
        <XCUIElementTypeTextView label="在其他应用中打开此页？" />
        <XCUIElementTypeButton label="打开" x="301" y="388" width="34" height="33" />
        </XCUIElementTypeOther></root>'''
    with patch('automation.pdd.spider.navigation._accept_app_prompt', return_value=False), \
            patch.object(ios, '_current_page_source', return_value=page), \
            patch.object(ios, '_tap_xy') as tap, \
            patch('automation.pdd.spider.navigation.time.monotonic', side_effect=[0, 0, 100]), \
            patch('automation.pdd.spider.navigation.time.sleep'), \
            pytest.raises(BoardUnavailableError):
        open_app_board(value, SOURCE)
    tap.assert_not_called()


def test_previous_board_is_never_accepted_and_timeout_is_reported():
    value = client()
    old_page = TARGET.replace('猫粮', '旧榜单')
    with patch('automation.pdd.spider.navigation._accept_app_prompt', return_value=False), \
            patch.object(ios, '_current_page_source', return_value=old_page), \
            patch('automation.pdd.spider.navigation.time.monotonic', side_effect=[0, 0, 100]), \
            patch('automation.pdd.spider.navigation.time.sleep'), \
            pytest.raises(BoardUnavailableError, match='Safari 未打开目标榜单'):
        open_app_board(value, SOURCE)


@pytest.mark.parametrize('message', ['Open in “拼多多”?', 'Open this page in “拼多多”?', '在“拼多多”中打开？'])
def test_chinese_and_english_prompts_use_default_open_action(message):
    value = Mock()
    value.http.get.return_value.value = message
    assert _accept_app_prompt(value)
    value.http.post.assert_called_once_with('/alert/accept', {}, timeout=5)


def test_absent_or_unrelated_alert_is_not_accepted():
    value = Mock()
    value.http.get.side_effect = WDARequestError(110, {'error': 'no such alert'})
    assert not _accept_app_prompt(value)
    value.http.get.side_effect = None
    value.http.get.return_value.value = '是否保存密码？\n打开拼多多APP'
    assert not _accept_app_prompt(value)
    value.http.post.assert_not_called()


def test_alert_connection_errors_are_not_hidden():
    value = Mock()
    value.http.get.side_effect = TimeoutError('WDA unavailable')
    with pytest.raises(TimeoutError):
        _accept_app_prompt(value)


@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('stage', ['prompt', 'foreground'])
def test_slow_board_retries_request_timeout_without_reopening_url(wrapped, stage):
    value = client()
    error = TimeoutError('timed out')
    if wrapped:
        error = HTTPError(error)
    if stage == 'foreground':
        value.http.get.side_effect = [error, Mock(value={'bundleId': ios.PDD_BUNDLE_ID})]
    with patch('automation.pdd.spider.navigation._accept_app_prompt',
               side_effect=[error, False] if stage == 'prompt' else [False, False]), \
            patch.object(ios, '_current_page_source', return_value=TARGET), \
            patch('automation.pdd.spider.navigation.time.sleep'):
        assert open_app_board(value, SOURCE) == TARGET
    value.session.return_value.open_url.assert_called_once_with(SOURCE['url'])


def test_persistent_timeout_stops_at_navigation_deadline():
    value = client()
    error = HTTPError(TimeoutError('timed out'))
    value.http.get.side_effect = error
    with patch('automation.pdd.spider.navigation._accept_app_prompt', side_effect=error), \
            patch('automation.pdd.spider.navigation.time.monotonic', side_effect=[0, 0, 100]), \
            patch('automation.pdd.spider.navigation.time.sleep'), \
            pytest.raises(HTTPError) as caught:
        open_app_board(value, SOURCE)
    assert caught.value is error


def test_navigation_does_not_retry_connection_refused():
    value = client()
    error = HTTPError(ConnectionRefusedError('connection refused'))
    with patch('automation.pdd.spider.navigation._accept_app_prompt', side_effect=error) as probe, \
            patch('automation.pdd.spider.navigation.time.sleep'), \
            pytest.raises(HTTPError):
        open_app_board(value, SOURCE)
    probe.assert_called_once()
