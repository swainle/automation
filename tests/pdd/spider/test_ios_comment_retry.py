from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest

from automation.pdd.spider.ios_product import EntryNotFound, IosProduct


def collector():
    product = IosProduct(Mock())
    product.negative = []
    product.snapshot = Mock()
    product.find = Mock(return_value=None)
    product.tap = Mock()
    return product


def test_missing_entry_returns_empty_after_two_top_retries():
    product = collector()
    product.find_entry = Mock(return_value=None)
    product.seek = Mock(side_effect=EntryNotFound('missing'))
    product._comments_top = Mock()
    product.comments()
    assert product.seek.call_count == 3
    assert product._comments_top.call_count == 2
    assert product.negative == []
    product.tap.assert_not_called()


def test_missing_more_entry_can_be_found_after_returning_to_top():
    product = collector()
    product.seek = Mock()
    entry = object()
    product.seek_entry = Mock(side_effect=[EntryNotFound('missing'), entry])
    product._comments_top = Mock()
    assert product._open_comments() is True
    product._comments_top.assert_called_once()
    product.tap.assert_called_once_with(entry)


@pytest.mark.parametrize('succeeds', [True, False])
def test_other_errors_retry_twice_and_preserve_progress(succeeds):
    product = collector()
    saved = {'ui_fingerprint': 'saved', 'time': '2026-11-13'}
    product.negative = [saved]
    error = RuntimeError('read failed')
    product._comments_once = Mock(side_effect=[error, error, None if succeeds else error])
    if succeeds:
        product.comments()
    else:
        with pytest.raises(RuntimeError, match='read failed'):
            product.comments()
    assert product._comments_once.call_count == 3
    assert product.negative == [saved]


def test_page_read_failure_is_never_treated_as_missing_entry():
    product = collector()
    product.find_entry = Mock(return_value=None)
    product.seek = Mock(side_effect=RuntimeError('WDA disconnected'))
    product._comments_top = Mock()
    with pytest.raises(RuntimeError, match='WDA disconnected'):
        product.comments()
    assert product.seek.call_count == 3
    product._comments_top.assert_not_called()


def test_retry_closes_existing_panel_before_reentering():
    product = collector()
    product.close_panel = Mock()

    def collect_once():
        if product._comments_once.call_count == 1:
            product._comments_panel_open = True
            raise RuntimeError('cleanup failed')
        assert product._comments_panel_open is False

    product._comments_once = Mock(side_effect=collect_once)
    product.comments()
    product.close_panel.assert_called_once()


def test_return_to_top_swipes_down_until_page_stops_changing():
    product = collector()
    product.root = ET.fromstring('<App><Text label="商品" y="10" visible="true"/></App>')
    product.swipe = Mock()
    product._comments_top()
    product.swipe.assert_called_once_with(up=False)


def test_return_to_top_failure_does_not_mark_product_as_no_reviews():
    product = collector()
    product.find_entry = Mock(return_value=None)
    product.seek = Mock(side_effect=EntryNotFound('missing'))
    product._comments_top = Mock(side_effect=RuntimeError('swipe failed'))
    with pytest.raises(RuntimeError, match='swipe failed'):
        product.comments()
    assert product._comments_top.call_count == 3


def test_user_interrupt_is_not_retried():
    product = collector()
    product._comments_once = Mock(side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        product.comments()
    product._comments_once.assert_called_once()
