"""参考灰度二值化与按列亮暗边界计数，识别易盾拼图缺口。"""

import math
from pathlib import Path


def analyze_gap(image_path, probe, output_dir, *, threshold=100):
    import numpy as np
    from PIL import Image, ImageDraw

    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 0 < threshold < 255:
        raise ValueError("threshold 必须为 1 到 254 的整数")
    image = Image.open(image_path).convert("RGB")
    if image.width * image.height > 2_000_000:
        raise ValueError("验证码图片过大")
    geometry = probe["geometry"]
    bg, piece, button, track = (geometry[k] for k in ("background", "piece", "button", "track"))
    for rect in (bg, piece, button, track):
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in rect.values()) or min(rect["width"], rect["height"]) <= 0:
            raise ValueError("组件几何尺寸无效")
    alpha = probe.get("alpha_bounds") or {"left": 0, "top": 0, "right": 1, "bottom": 1}
    sx, sy = image.width / bg["width"], image.height / bg["height"]
    current_left = piece["x"] - bg["x"] + alpha["left"] * piece["width"]
    piece_width = (alpha["right"] - alpha["left"]) * piece["width"]
    top = max(0, int((piece["y"]-bg["y"] + alpha["top"]*piece["height"]) * sy))
    bottom = min(image.height, math.ceil((piece["y"]-bg["y"] + alpha["bottom"]*piece["height"]) * sy))
    piece_travel = bg["width"] - piece["width"]
    button_travel = track["width"] - button["width"]
    if piece_travel <= 0 or button_travel <= 0 or bottom <= top:
        raise ValueError("轨道或拼图移动范围无效")
    ratio = button_travel / piece_travel
    remaining = track["x"] + track["width"] - button["x"] - button["width"]
    left = max(1, math.ceil((current_left + piece_width + 2) * sx))
    right = min(image.width-1, math.floor((current_left + remaining / ratio) * sx)+1)
    if right <= left:
        raise ValueError("缺口搜索区域为空")
    rgb = np.asarray(image, dtype=float)
    gray = rgb[:,:,0]*0.2126 + rgb[:,:,1]*0.7152 + rgb[:,:,2]*0.0722
    binary = gray > threshold
    changes = ((~binary[top:bottom,1:]) & binary[top:bottom,:-1]).sum(axis=0)
    scores = [0] + [int(v) for v in changes]
    ranked = sorted(range(left, right), key=lambda x: scores[x], reverse=True)
    best = ranked[0]
    alternate = next((x for x in ranked if abs(x-best) >= max(5, piece_width*sx*0.6)), None)
    best_score = scores[best]
    runner_score = scores[alternate] if alternate is not None else 0
    status = "recognized"
    if best_score < max(5, (bottom-top)*0.2):
        status = "weak_edge"
    elif runner_score >= best_score * 0.95:
        status = "ambiguous"
    distance = (best/sx-current_left)*ratio
    if not 0 < distance <= remaining:
        status = "out_of_range"
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    Image.fromarray(binary.astype("uint8")*255).save(folder / "binary.png")
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    draw.rectangle((left, top, right, bottom-1), outline="#1565c0", width=1)
    draw.line((best, 0, best, image.height-1), fill="#e53935", width=2)
    if alternate is not None:
        draw.line((alternate, top, alternate, bottom-1), fill="#ff9800", width=1)
    marked.save(folder / "gap.png")
    return {"status": status, "threshold": threshold, "gap_x": best, "distance": round(distance, 3),
            "edge_score": best_score, "runner_score": runner_score, "column_scores": scores,
            "search_bounds": [left, top, right, bottom], "coordinate_scale": sx,
            "track_ratio": ratio, "alpha_bounds_available": probe.get("alpha_bounds") is not None,
            "method": "grayscale_binary_white_to_dark_columns"}
