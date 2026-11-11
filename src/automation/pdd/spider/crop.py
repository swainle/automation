"""Crop saved iOS screenshots using their paired element coordinates."""
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import xml.etree.ElementTree as ET

from PIL import Image


def elements_from_xml(source):
    result = []
    for element in ET.fromstring(source).iter():
        text = next((element.get(key, "").strip() for key in ("label", "name", "value")
                     if element.get(key, "").strip()), "")
        try:
            x, y, width, height = (float(element.get(key, 0)) for key in ("x", "y", "width", "height"))
        except ValueError:
            continue
        result.append({"type": element.tag.replace("XCUIElementType", ""), "text": text,
                       "x": x, "y": y, "w": width, "h": height,
                       "visible": element.get("visible", "unknown")})
    return result


def logical_scale(elements, image_size):
    windows = [e for e in elements if e["type"] in ("Window", "Application")
               and e["x"] == 0 and e["y"] == 0 and e["w"] > 0 and e["h"] > 0]
    if not windows:
        raise ValueError("元素快照缺少可确认的屏幕尺寸")
    window = max(windows, key=lambda e: e["w"] * e["h"])
    sx, sy = image_size[0] / window["w"], image_size[1] / window["h"]
    if abs(sx - sy) > 0.01:
        raise ValueError("截图与元素快照宽高比例不一致")
    return sx, sy, window["w"]


def banner_rectangle(image):
    """Detect a wide light-background transition below a colored header."""
    image = image.convert("RGB")
    width, height = image.size
    pixels = image.load()
    run = 0
    seen_header = False
    for y in range(height):
        samples = [pixels[x, y] for x in range(0, width, 4)]
        light = sum(min(pixel) >= 232 and max(pixel) - min(pixel) <= 35 for pixel in samples)
        fraction = light / len(samples)
        seen_header |= fraction < 0.7
        run = run + 1 if fraction >= 0.98 and seen_header else 0
        if run >= 6:
            bottom = y - run + 1
            if bottom < 20:
                raise ValueError("顶部区域不足以确认横幅")
            return (0, 0, width, bottom)
    raise ValueError("未识别到横幅下边界，不使用固定高度兜底")


def _normalized(text):
    return re.sub(r"\s+|\u200b", "", text)


def card_rectangle(image_size, elements, title, *, bottom_extra_pixels=10, allow_outside=False):
    sx, sy, logical_width = logical_scale(elements, image_size)
    normalized = _normalized(title)
    if not normalized:
        raise ValueError("缺少当前商品标题，无法确认目标卡片")
    texts = [e for e in elements if e["type"] == "StaticText" and e["w"] > 0 and e["h"] > 0]
    candidates = [e for e in texts if _normalized(e["text"]) == normalized]
    if not candidates:
        candidates = [e for e in texts if normalized[:12] in _normalized(e["text"])]
    unique = {(e["x"], e["y"], e["w"], e["h"]): e for e in candidates}
    if len(unique) != 1:
        raise ValueError("截图元素中未唯一匹配当前商品标题")
    target = next(iter(unique.values()))
    factor = logical_width / 414
    badges = [e for e in elements if e["type"] == "Other"
              and 0 <= e["x"] < 60 * factor
              and target["y"] - 0.3 * logical_width <= e["y"] < target["y"]
              and e["y"] + e["h"] <= target["y"] + 3 * factor
              and 20 * factor <= e["w"] <= 60 * factor
              and 20 * factor <= e["h"] <= 60 * factor]
    if not badges:
        raise ValueError("未找到当前商品对应的排行徽标矩形")
    anchor = max(badges, key=lambda e: e["y"])
    top = round(anchor["y"] * sy)
    width, height = image_size
    box = (0, top, width, top + round(width / 2) + bottom_extra_pixels)
    if not allow_outside and (top < 0 or box[3] > height):
        raise ValueError("商品卡片超出截图边界，请重新定位后截图")
    if not (box[1] <= target["y"] * sy and (target["y"] + target["h"]) * sy <= box[3]):
        raise ValueError("固定裁剪框无法完整包含当前商品标题")
    return box, anchor


@dataclass(frozen=True)
class Banner:
    png: bytes
    width: int
    height: int

    @classmethod
    def from_image(cls, image):
        box = banner_rectangle(image)
        crop = image.crop(box).convert("RGB")
        stream = BytesIO()
        crop.save(stream, format="PNG")
        return cls(stream.getvalue(), crop.width, crop.height)


def compose_product(image, elements, title, banner):
    image = image.convert("RGB")
    if image.width != banner.width:
        raise ValueError("横幅与商品截图宽度不同，请重新截取当前子榜顶部")
    box, anchor = card_rectangle(image.size, elements, title)
    try:
        current_header_bottom = min(banner_rectangle(image)[3], banner.height)
    except ValueError:
        current_header_bottom = banner.height
    if box[1] < current_header_bottom:
        raise ValueError("当前商品被顶部横幅遮挡，请重新定位后截图")
    card = image.crop(box)
    result = Image.new("RGB", (image.width, banner.height + card.height), "white")
    with Image.open(BytesIO(banner.png)) as header:
        result.paste(header, (0, 0))
    result.paste(card, (0, banner.height))
    return result, {"card_box": list(box), "anchor": anchor,
                    "banner_height": banner.height, "bottom_extra_pixels": 10,
                    "product_size": list(result.size)}


def save_product(image, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            image.save(stream, format="JPEG", quality=95)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
