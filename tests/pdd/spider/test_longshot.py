import json
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
import pytest

from automation.pdd.spider import longshot


def scene():
    rng = np.random.default_rng(42)
    return Image.fromarray(rng.integers(0, 255, size=(1000, 240, 3), dtype=np.uint8))


def frame(world, offset, header=50, height=320):
    image = Image.new('RGB', (world.width, height), 'white')
    image.paste(world.crop((0, offset, world.width, offset + height - header)), (0, header))
    return image


def test_overlap_matching_and_composition_preserve_every_pixel():
    world = scene()
    frames = [frame(world, offset) for offset in (0, 90, 180, 270)]
    bodies = [image.crop((0, 50, image.width, 298)) for image in frames]
    shifts = [longshot.match_shift(a, b)[0] for a, b in zip(bodies, bodies[1:])]
    assert all(abs(shift - 90) <= 2 for shift in shifts)
    result = longshot.compose(frames, [90, 90, 90], 50, 298, 550)
    assert np.array_equal(np.asarray(result.crop((0, 50, 240, 550))), np.asarray(world.crop((0, 0, 240, 500))))


def test_geometry_excludes_recommendation_badges():
    xml = '<XCUIElementTypeApplication x="0" y="0" width="414" height="896">'
    rows = []
    for rank, y in ((1, 229), (2, 451)):
        title = f'商品{rank}完整标题'
        rows.append({'排行': rank, '标题': title})
        xml += f'<XCUIElementTypeOther x="6" y="{y}" width="43" height="39"/>'
        xml += f'<XCUIElementTypeStaticText label="{title}" x="192" y="{y+60}" width="200" height="60"/>'
    xml += '<XCUIElementTypeStaticText label="更多上榜商品推荐" x="140" y="673" width="150" height="30"/>'
    xml += '<XCUIElementTypeOther x="6" y="900" width="43" height="39"/></XCUIElementTypeApplication>'
    layout = longshot.geometry(xml, rows, (828, 1792))
    assert layout['tops'] == [446, 890]
    assert layout['end'] == 1346


def test_unrelated_screens_are_rejected():
    a = scene().crop((0, 0, 240, 300))
    b = Image.new('RGB', a.size, 'black')
    with pytest.raises(ValueError, match='重叠|位移'):
        longshot.match_shift(a, b)


def test_gesture_timeout_does_not_send_a_second_swipe():
    client = Mock()
    client._session_http.post.side_effect = TimeoutError('timed out')
    with patch.object(longshot.ios, '_swipe') as legacy, pytest.raises(TimeoutError):
        longshot.drag_for_screenshot(client, 100, 600, 300)
    legacy.assert_not_called()


def test_unsupported_velocity_endpoint_is_cached_and_uses_legacy():
    client = Mock()
    client._session_http.post.side_effect = RuntimeError('Unhandled endpoint')
    with patch.object(longshot.ios, '_swipe', return_value=True) as legacy:
        longshot.drag_for_screenshot(client, 100, 600, 300)
        longshot.drag_for_screenshot(client, 100, 600, 300)
    assert client._session_http.post.call_count == 1
    assert legacy.call_count == 2


def test_capture_uses_screenshots_without_source_reads_and_creates_all_crops(tmp_path):
    world = scene()
    client = Mock()
    client.http.get.return_value.value = {'bundleId': longshot.ios.PDD_BUNDLE_ID}
    client.screenshot.side_effect = [frame(world, offset) for offset in (0, 90, 180, 270, 360, 450)]
    rows = [{'排行': index + 1, '标题': f'商品{index}'} for index in range(3)]
    layout = {'body_top': 50, 'tops': [50, 270, 490], 'end': 700, 'scale': 1}
    with patch.object(longshot, 'geometry', return_value=layout), \
            patch.object(longshot.ios, '_get_window_size', return_value=(240, 320)), \
            patch.object(longshot.ios, '_swipe', return_value=True), patch.object(longshot.time, 'sleep'), \
            patch.object(longshot.ios, '_current_page_source') as source:
        files = longshot.capture(client, rows, 'paired-xml', tmp_path)
    source.assert_not_called()
    assert len(files) == 5
    assert all((tmp_path / f'榜{rank:02d}/产品截图.jpg').is_file() for rank in (1, 2, 3))
    assert longshot.reusable(tmp_path, rows)
    (tmp_path / '榜01/产品截图.jpg').unlink()
    assert longshot.reusable(tmp_path, rows)
    assert (tmp_path / '榜01/产品截图.jpg').is_file()
    (tmp_path / '榜02/产品截图.jpg').write_bytes(b'broken')
    assert longshot.reusable(tmp_path, rows)
    with Image.open(tmp_path / '榜02/产品截图.jpg') as repaired:
        repaired.verify()
    (tmp_path / '榜单长图.jpg').write_bytes(b'broken')
    assert not longshot.reusable(tmp_path, rows)
