"""Align overlapping screenshots and crop a complete iOS leaderboard."""
from automation.pdd.spider.logging_setup import timed_operation
import hashlib
import json
from pathlib import Path
import time

import cv2
import numpy as np
from PIL import Image

from automation.pdd.spider import ios
from automation.pdd.spider.artifacts import write_json
from automation.pdd.spider.crop import elements_from_xml, logical_scale, save_product
from automation.pdd.spider.metrics import from_row


def fingerprint(rows):
    value = [(row['排行'], row['标题'], from_row(row)) for row in rows]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def image_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def geometry(xml, rows, image_size):
    """Use top-of-page rank badges, with strict checks before trusting offscreen bounds."""
    elements = elements_from_xml(xml)
    sx, sy, width = logical_scale(elements, image_size)
    factor = width / 414
    end_markers = [element['y'] for element in elements if element['type'] == 'StaticText'
                   and any(marker in element['text'] for marker in ios._END_MARKS)]
    if not end_markers:
        raise ValueError('没有找到榜单结束分界')
    end_y = min(end_markers)
    badges = [element for element in elements if element['type'] == 'Other'
              and 0 <= element['x'] < 70 * factor and 32 * factor <= element['w'] <= 60 * factor
              and 28 * factor <= element['h'] <= 52 * factor
              and 0 < element['y'] < end_y]
    if len(badges) != len(rows):
        raise ValueError(f'排行徽标数 {len(badges)} 与商品数 {len(rows)} 不一致，无法可靠裁剪')
    tops = [round((badge['y'] - 6 * factor) * sy) for badge in badges]
    if not tops or tops[0] < image_size[1] * .08 or tops[0] >= image_size[1] * .6:
        raise ValueError('当前不在榜单顶部，无法建立长图坐标')
    if any(right - left < image_size[0] * .15 for left, right in zip(tops, tops[1:])):
        raise ValueError('离屏排行坐标不连续，不能用该快照裁剪长图')
    ends = [element['y'] for element in elements if element['type'] == 'StaticText'
            and any(marker in element['text'] for marker in ios._END_MARKS) and element['y'] * sy > tops[-1]]
    if not ends:
        raise ValueError('没有在最后一个商品之后找到榜单结束分界')
    end = round(min(ends) * sy)
    for index, row in enumerate(rows):
        bottom = tops[index + 1] if index + 1 < len(tops) else end
        titles = [element for element in elements if element['type'] == 'StaticText'
                  and element['text'].replace('\u200b', '').strip() == row['标题']]
        # 榜单内可能存在同名商品（标题重复）：用排行徽标区间消歧，
        # 取落在本行位置区间内的那一个标题元素；若仍不唯一则报错。
        candidates = [t for t in titles if tops[index] <= t['y'] * sy < bottom]
        if len(candidates) != 1:
            raise ValueError(f"排行 {row['排行']} 标题不唯一，不能可靠裁剪")
        y = candidates[0]['y'] * sy
    return {'tops': tops, 'end': end, 'body_top': tops[0], 'scale': sy}


def match_shift(previous, current):
    """Estimate vertical movement using several textured bands, ignoring side overlays."""
    if previous.size != current.size:
        raise ValueError('连续截图尺寸变化，无法拼接')
    width, height = previous.size
    ratio = min(1, 160 / width)
    size = (round(width * ratio), round(height * ratio))
    a = np.asarray(previous.convert('L').resize(size), dtype=np.uint8)
    b = np.asarray(current.convert('L').resize(size), dtype=np.uint8)
    left, right = round(size[0] * .08), round(size[0] * .87)
    band = max(12, round(size[1] * .13))
    matches = []
    for fraction in (.65, .8, .98):
        bottom = min(size[1], round(size[1] * fraction))
        top = bottom - band
        template = a[top:bottom, left:right]
        if template.std() < 10:
            continue
        result = cv2.matchTemplate(b[:, left:right], template, cv2.TM_SQDIFF_NORMED)
        score, _, location, _ = cv2.minMaxLoc(result)
        shift = (top - location[1]) / ratio
        if score <= .10 and -2 / ratio <= shift <= height * .85:
            matches.append((shift, score, fraction))
    if len(matches) < 2:
        raise ValueError('截图重叠区域不足或内容变化，未生成可能错位的长图')
    shifts = sorted(value[0] for value in matches)
    median = shifts[len(shifts) // 2]
    agreeing = [(shift, score, fraction) for shift, score, fraction in matches if abs(shift - median) <= 4 / ratio]
    if len(agreeing) < 2:
        raise ValueError('不同区域的拼接位移不一致，未生成错位长图')
    # Refine at native resolution to prevent small rounding errors accumulating
    # over a long leaderboard and moving the final card crops by dozens of pixels.
    full_a = np.asarray(previous.convert('L'), dtype=np.uint8)
    full_b = np.asarray(current.convert('L'), dtype=np.uint8)
    full_left, full_right = round(width * .08), round(width * .87)
    refined = []
    radius = max(4, round(4 / ratio))
    for shift, _, fraction in agreeing:
        bottom = min(height, round(height * fraction))
        top = bottom - max(24, round(height * .13))
        guess = top - round(shift)
        lo, hi = max(0, guess - radius), min(height, guess + radius + bottom - top)
        template = full_a[top:bottom, full_left:full_right]
        region = full_b[lo:hi, full_left:full_right]
        if region.shape[0] < template.shape[0]:
            continue
        result = cv2.matchTemplate(region, template, cv2.TM_SQDIFF_NORMED)
        score, _, location, _ = cv2.minMaxLoc(result)
        if score <= .10:
            refined.append((top - lo - location[1], score))
    if len(refined) < 2 or max(value[0] for value in refined) - min(value[0] for value in refined) > 3:
        raise ValueError('原始分辨率的重叠校验失败，停止拼接')
    shift = max(0, round(sum(value[0] for value in refined) / len(refined)))
    return shift, min(value[1] for value in refined)


def compose(frames, shifts, body_top, body_bottom, end):
    if len(frames) != len(shifts) + 1:
        raise ValueError('截图与拼接位移数量不一致')
    width = frames[0].width
    height = body_bottom + sum(shifts)
    if end > height or end <= body_top or width * end > 100_000_000:
        raise ValueError('长图未覆盖榜单末尾或尺寸超出上限')
    result = Image.new('RGB', (width, end), 'white')
    result.paste(frames[0].crop((0, 0, width, min(body_bottom, end))), (0, 0))
    cursor = body_bottom
    body_height = body_bottom - body_top
    for frame, shift in zip(frames[1:], shifts):
        if not 0 < shift < body_height:
            raise ValueError('无效的图片拼接位移')
        strip = frame.crop((0, body_bottom - shift, width, body_bottom))
        result.paste(strip, (0, cursor))
        cursor += shift
    # Recover the floating "top" button region from another overlapping screen,
    # rather than repeating that fixed overlay through the long image.
    offsets = [0]
    for shift in shifts:
        offsets.append(offsets[-1] + shift)
    overlay_top = round(frames[0].height * .84)
    overlay_left = round(width * .82)
    for index in range(1, len(frames)):
        start, stop = offsets[index] + overlay_top, min(end, offsets[index] + body_bottom)
        if stop <= start:
            continue
        for other in [*range(index + 1, len(frames)), *range(index - 1, -1, -1)]:
            lo, hi = start - offsets[other], stop - offsets[other]
            if body_top <= lo < hi <= body_bottom and (other == 0 or hi <= overlay_top):
                patch = frames[other].crop((overlay_left, lo, width, hi))
                result.paste(patch, (overlay_left, start))
                break
    return result


def drag_for_screenshot(client, x, y1, y2):
    """Controlled drag with a short end hold to avoid flinging past overlap."""
    if not client.__dict__.get('_longshot_legacy_drag', False):
        try:
            client._session_http.post('/wda/pressAndDragWithVelocity', data={
                'fromX': int(x), 'fromY': int(y1), 'toX': int(x), 'toY': int(y2),
                'pressDuration': .05, 'holdDuration': .15, 'velocity': 400,
            })
            return
        except Exception as error:
            if not any(marker in str(error).lower() for marker in ('unknown command', 'unhandled endpoint', 'unsupported operation')):
                raise
            client._longshot_legacy_drag = True
    if not ios._swipe(client, x, y1, x, y2, duration=.1, velocity=400):
        raise RuntimeError('滑动截图失败，未标记长图完成')


@timed_operation('[榜单] 截取长图并裁剪商品卡片')
def capture(client, rows, xml, directory, *, max_frames=60, compact=False):
    """Capture overlapping screens; screenshot alignment replaces repeated XML reads."""
    directory = Path(directory)
    frame_dir = directory / '榜单分屏'
    directory.mkdir(parents=True, exist_ok=True)
    if not compact:
        frame_dir.mkdir(parents=True, exist_ok=True)
    ios.require_pdd_foreground(client)
    first = client.screenshot().convert('RGB')
    if not compact:
        (directory / '榜单元素.xml').write_text(xml, encoding='utf-8')
    layout = geometry(xml, rows, first.size)
    width, height = ios._get_window_size(client)
    top = layout['body_top']
    bottom = first.height - round(22 * layout['scale'])
    if bottom - top < first.height * .25:
        raise ValueError('可滚动截图区域过小')
    frames, shifts, scores, files = [first], [], [], []
    path = frame_dir / '0001.png'
    if not compact:
        first.save(path)
        files.append(str(path.relative_to(directory)))
    covered = bottom
    while covered < layout['end']:
        if len(frames) >= max_frames:
            raise RuntimeError('超过截图页数上限，未完成榜单截图')
        travel = (bottom - top) / layout['scale'] * .5
        y1 = min(height - 60, bottom / layout['scale'] - 25)
        y2 = max(top / layout['scale'] + 25, y1 - travel)
        ios.require_pdd_foreground(client)
        drag_for_screenshot(client, width / 2, y1, y2)
        time.sleep(.4)
        ios.require_pdd_foreground(client)
        shot = client.screenshot().convert('RGB')
        if shot.size != first.size:
            raise ValueError('设备截图尺寸变化，停止拼接')
        previous_body = frames[-1].crop((0, top, first.width, bottom))
        current_body = shot.crop((0, top, first.width, bottom))
        shift, score = match_shift(previous_body, current_body)
        if shift <= 3:
            raise RuntimeError('页面已不再移动，但尚未覆盖榜单末尾')
        shifts.append(shift)
        scores.append(score)
        frames.append(shot)
        covered += shift
        path = frame_dir / f'{len(frames):04d}.png'
        if not compact:
            shot.save(path)
            files.append(str(path.relative_to(directory)))
        print(f'[长图] 已截 {len(frames)} 屏，覆盖 {covered}/{layout["end"]} px')
    image = compose(frames, shifts, top, bottom, layout['end'])
    output = directory / '榜单长图.jpg'
    save_product(image, output)
    crops = []
    header = image.crop((0, 0, image.width, top))
    for index, row in enumerate(rows):
        start = layout['tops'][index]
        end = layout['tops'][index + 1] if index + 1 < len(rows) else layout['end']
        if end <= start:
            raise ValueError('商品卡片裁剪边界无效')
        card = image.crop((0, start, image.width, end))
        result = Image.new('RGB', (image.width, top + card.height), 'white')
        result.paste(header, (0, 0))
        result.paste(card, (0, top))
        crop_name = '产品榜单截图.jpg' if compact else '产品截图.jpg'
        path = directory / f"榜{int(row['排行']):02d}/{crop_name}"
        save_product(result, path)
        crops.append(str(path.relative_to(directory)))
    metadata = {'version': 1, 'fingerprint': fingerprint(rows), 'sha256': image_digest(output),
                'layout': layout, 'frames': files, 'shifts': shifts, 'scores': scores, 'crops': crops,
                'crop_sha256': {path: image_digest(directory / path) for path in crops},
                'snapshot_metrics': [row.get('metrics', {}) for row in rows]}
    if not compact:
        write_json(directory / '榜单长图.meta.json', metadata)
    for frame in frames:
        frame.close()
    return [str(output), *([] if compact else [str(directory / '榜单长图.meta.json')]), *(str(directory / path) for path in crops)]


def reusable(directory, rows):
    """Reuse a verified long image, repairing missing or damaged card crops."""
    directory = Path(directory)
    try:
        metadata = json.loads((directory / '榜单长图.meta.json').read_text(encoding='utf-8'))
        output = directory / '榜单长图.jpg'
        if metadata['fingerprint'] != fingerprint(rows) or metadata['sha256'] != image_digest(output):
            return False
        layout = metadata['layout']
        tops, end, top = layout['tops'], layout['end'], layout['body_top']
        with Image.open(output) as image:
            if (len(tops) != len(rows) or not tops or top != tops[0]
                    or not 0 <= top < end <= image.height
                    or any(a >= b for a, b in zip(tops, [*tops[1:], end]))):
                return False
            crops = [f"榜{int(row['排行']):02d}/产品截图.jpg" for row in rows]
            hashes = metadata.get('crop_sha256', {})
            for index, path in enumerate(crops):
                target = directory / path
                try:
                    if hashes.get(path) == image_digest(target):
                        continue
                except OSError:
                    pass
                bottom = tops[index + 1] if index + 1 < len(tops) else end
                result = Image.new('RGB', (image.width, top + bottom - tops[index]), 'white')
                result.paste(image.crop((0, 0, image.width, top)), (0, 0))
                result.paste(image.crop((0, tops[index], image.width, bottom)), (0, top))
                save_product(result, target)
                result.close()
                hashes[path] = image_digest(target)
            metadata.update(crops=crops, crop_sha256={path: hashes[path] for path in crops})
        write_json(directory / '榜单长图.meta.json', metadata)
        return True
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False
