"""滑块候选位置诊断，仅分析图片，不操作浏览器。"""

from io import BytesIO
from pathlib import Path


def analyze_candidates(background_bytes: bytes, piece_bytes: bytes, output: Path):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    background = Image.open(BytesIO(background_bytes)).convert("RGB")
    piece = Image.open(BytesIO(piece_bytes)).convert("RGBA")
    rgba = np.array(piece)
    alpha = rgba[:, :, 3] > 128
    ys, xs = np.where(alpha)
    if not len(xs) or alpha.all():
        raise ValueError("滑块图片没有可用透明轮廓，无法比较候选形状")
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    mask = alpha[y0:y1, x0:x1].astype(np.uint8)
    template = rgba[y0:y1, x0:x1, :3]
    bg = np.array(background)
    gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY)
    h, w = mask.shape
    options = []
    # 暗缺口先生成候选，避免把山体等密集纹理区域当作缺口。
    top, bottom = max(0, y0 - h // 2), min(background.height, y1 + h // 2)
    search = gray[top:bottom]
    for threshold in range(30, 131, 10):
        binary = (search < threshold).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
        for label in range(1, count):
            x, y, cw, ch, area = (int(v) for v in stats[label])
            if x < x0 + w or not (w * 0.45 <= cw <= w * 1.5 and h * 0.5 <= ch <= h * 1.8):
                continue
            if abs(top + y + ch / 2 - (y0 + h / 2)) > h * 0.5 or area / (cw * ch) < 0.3:
                continue
            component = labels[y:y+ch, x:x+cw] == label
            resized_mask = cv2.resize(mask, (cw, ch), interpolation=cv2.INTER_NEAREST) > 0
            union = np.logical_or(component, resized_mask).sum()
            shape = float(np.logical_and(component, resized_mask).sum() / union) if union else 0.0
            inside = cv2.erode(resized_mask.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
            if inside.sum() < 10:
                continue
            resized_piece = cv2.resize(template, (cw, ch))
            source = resized_piece[inside].astype(float).ravel()
            target = bg[top+y:top+y+ch, x:x+cw][inside].astype(float).ravel()
            source -= source.mean()
            target -= target.mean()
            denom = np.linalg.norm(source) * np.linalg.norm(target)
            texture = max(0.0, float(np.dot(source, target) / denom)) if denom > 1e-9 else 0.0
            score = 0.6 * shape + 0.4 * texture
            options.append({"x": x, "y": top + y, "width": cw, "height": ch,
                            "shape_score": round(shape, 4), "texture_score": round(texture, 4),
                            "score": round(score, 4), "natural_distance": round(x + cw / 2 - (x0 + w / 2), 2)})
    selected = []
    for item in sorted(options, key=lambda item: item["score"], reverse=True):
        if all(abs(item["x"] - previous["x"]) >= max(12, w * 0.8) for previous in selected):
            selected.append(item)
        if len(selected) == 2:
            break
    if not selected:
        raise ValueError("未找到可比较的滑块候选位置")
    for rank, item in enumerate(selected, 1):
        item["rank"] = rank
    # 保留候选编号；按用户规则在已筛出的两个候选中优先选综合分较低者。
    margin = selected[0]["score"] - selected[1]["score"] if len(selected) == 2 else 0.0
    recommendation = min(selected, key=lambda item: item["score"])["rank"] if margin > 0 else None
    scale = 2
    canvas = Image.new("RGB", (max(background.width * scale, 700), background.height * scale + 170), "white")
    canvas.paste(background.resize((background.width * scale, background.height * scale)), (0, 0))
    draw = ImageDraw.Draw(canvas)
    colors = ("#e53935", "#1565c0")
    for item, color in zip(selected, colors):
        x, y = item["x"] * scale, item["y"] * scale
        draw.rectangle((x, y, x + item["width"] * scale, y + item["height"] * scale), outline=color, width=3)
        draw.text((x, max(0, y - 15)), f'#{item["rank"]}', fill=color)
        label = (f'#{item["rank"]} total={item["score"]:.3f} '
                 f'shape={item["shape_score"]:.3f} texture={item["texture_score"]:.3f} '
                 f'dx={item["natural_distance"]} source px')
        draw.text((10, background.height * scale + 15 + 25 * (item["rank"] - 1)), label, fill=color)
    verdict = (f'Selected: #{recommendation} (user rule: lower total score first)'
               if recommendation else 'No lower-score choice: tie or only one candidate')
    draw.text((10, background.height * scale + 75), verdict, fill="black")
    draw.text((10, background.height * scale + 100), 'Scores are similarities, NOT pass probabilities. Image analysis only.', fill="black")
    canvas.save(output)
    return {"candidates": selected, "recommended_rank": recommendation, "selection_rule": "lower_total_score_first", "score_margin": round(margin, 4),
            "piece_bounds": [x0, y0, x1, y1], "background_width": background.width}
