"""保存插件检测结果及本地标注图；不操作浏览器。"""

import json
from pathlib import Path

from PIL import Image, ImageDraw

from automation.com.extension.client import save_png


def save_detection(result, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    report = {key: value for key, value in result.items() if key != "screenshot"}
    screenshot = result.get("screenshot")
    if screenshot:
        report["screenshot"] = {key: value for key, value in screenshot.items() if key != "data"}
    (folder / "detection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not screenshot:
        return report
    raw_path = save_png(screenshot, folder / "screenshot.png")
    with Image.open(raw_path) as source:
        image = source.convert("RGB")
    width, height = screenshot["width"], screenshot["height"]
    if width <= 0 or height <= 0:
        raise ValueError("截图尺寸无效")
    scale_x, scale_y = image.width / width, image.height / height
    origin_x, origin_y = screenshot.get("origin_x", 0), screenshot.get("origin_y", 0)
    groups = (("button", result.get("buttons", []), "#e53935"),
              ("background", result.get("backgrounds", []), "#1565c0"),
              ("piece", result.get("pieces", []), "#8e24aa"),
              ("track", result.get("tracks", []), "#00897b"))
    # 图例放在侧边，避免背景、拼图和轨道嵌套时文字互相遮挡。
    canvas = Image.new("RGB", (image.width + 240, max(image.height, 50 + sum(len(items) for _, items, _ in groups) * 36)), "white")
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    legend_x, legend_y = image.width + 12, 12
    draw.text((legend_x, legend_y), "Status: " + str(result.get("status", "unknown")), fill="black")
    legend_y += 28
    for name, items, color in groups:
        for index, item in enumerate(items, 1):
            rect = item["rect"]
            draw.text((legend_x, legend_y), f"{name} #{index}", fill=color)
            draw.text((legend_x, legend_y + 14), f"{rect['width']:.0f} x {rect['height']:.0f} CSS px", fill="#555555")
            legend_y += 36
            x, y = (rect["page_x"] - origin_x) * scale_x, (rect["page_y"] - origin_y) * scale_y
            right, bottom = x + rect["width"] * scale_x, y + rect["height"] * scale_y
            if right < 0 or bottom < 0 or x > image.width or y > image.height:
                continue
            draw.rectangle((max(0, x), max(0, y), min(image.width - 1, right), min(image.height - 1, bottom)), outline=color, width=2)
    canvas.save(folder / "diagnostic.png")
    return report
