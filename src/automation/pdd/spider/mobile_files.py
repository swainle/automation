"""Keep only the public mobile ranking artifacts."""
from contextlib import ExitStack
import csv
from datetime import date
import os
from pathlib import Path
import re

from PIL import Image

from automation.pdd.spider import config
from automation.pdd.spider.crop import save_product


def checked_paths(directory):
    root = Path(directory)
    resolved = root.resolve()
    output = config.OUTPUT_ROOT.resolve()
    if root.is_symlink() or root.is_junction() or resolved == output or not resolved.is_relative_to(output):
        raise ValueError(f'清理目录必须位于榜单输出目录内：{root}')
    if date.fromisoformat(root.name).isoformat() != root.name:
        raise ValueError(f'清理目录必须是榜单日期目录：{root}')
    paths = []
    for parent, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(resolved):
                raise ValueError(f'清理目录包含外部链接：{path}')
            paths.append(path)
    return root, paths


def allowed_file(relative):
    parts = relative.parts
    if len(parts) == 1:
        return parts[0] in {'榜单数据.xlsx', '榜单长图.jpg'}
    if not re.fullmatch(r'榜\d{2,}', parts[0]):
        return False
    if len(parts) == 2:
        return parts[1] in {'产品榜单截图.jpg', '产品截图.jpg', 'SKU.csv', '差评.csv'}
    if len(parts) == 3:
        folder, name = parts[1:]
        if folder == '主图':
            return bool(re.fullmatch(r'\d{3,}\.jpg', name))
        if folder in {'SKU', '详情'}:
            return bool(re.fullmatch(r'\d{4,}\.jpg', name)) or (folder == '详情' and name == '长图.jpg')
    return False


def planned_renames(root):
    changes = []
    legacy_metadata = (root / '榜单长图.meta.json').is_file()
    for folder in root.glob('榜[0-9]*'):
        if not folder.is_dir():
            continue
        card, page = folder / '产品截图.jpg', folder / '产品页截图.jpg'
        target = folder / '产品榜单截图.jpg'
        legacy = legacy_metadata or page.is_file() or any(folder.glob('*.json')) or any(folder.glob('*.xml'))
        if legacy and card.is_file() and not target.exists():
            changes.append((card, target))
        if page.is_file():
            changes.append((page, card))
        for old_folder, new_folder, prefix, width in [('主图', '主图', '主图_', 3),
                ('SKU', 'SKU', 'SKU_', 4), ('详情图片', '详情', '详情_', 4)]:
            for old in (folder / old_folder).glob('*.jpg'):
                match = re.fullmatch(re.escape(prefix) + r'(\d+)\.jpg', old.name)
                if match:
                    target = folder / new_folder / f'{int(match[1]):0{width}d}.jpg'
                    changes.append((old, target))
        for old_long in (folder / '详情图片/长图.jpg', folder / '长图.jpg'):
            if old_long.is_file():
                changes.append((old_long, folder / '详情/长图.jpg'))
    return changes


def cleanup_ranking(directory):
    root, paths = checked_paths(directory)
    changes = planned_renames(root)
    for old, new in changes:
        new.parent.mkdir(parents=True, exist_ok=True)
        if not new.exists():
            old.rename(new)
    # Existing SKU exports must follow renamed image files.
    for table in root.glob('榜[0-9]*/SKU.csv'):
        mappings = [(old.relative_to(table.parent), new.relative_to(table.parent)) for old, new in changes
                    if old.is_relative_to(table.parent) and new.is_relative_to(table.parent)]
        if not mappings:
            continue
        with table.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        for row in rows:
            for index, value in enumerate(row):
                for old, new in mappings:
                    value = value.replace(str(old), str(new)).replace(old.as_posix(), new.as_posix())
                row[index] = value
        with table.open('w', encoding='utf-8-sig', newline='') as stream:
            csv.writer(stream).writerows(rows)
    # Re-enumerate after renaming, and validate every target before deleting any.
    root, paths = checked_paths(root)
    removed = 0
    for path in paths:
        if path.is_file() and not allowed_file(path.relative_to(root)):
            path.unlink()
            removed += 1
    for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


def merge_details(paths, output):
    paths = sorted(paths, key=lambda p: int(Path(p).stem))
    if not paths:
        return
    with ExitStack() as stack:
        images = [stack.enter_context(Image.open(path)) for path in paths]
        width = max(image.width for image in images)
        heights = [max(1, round(image.height * width / image.width)) for image in images]
        # libjpeg rejects either dimension above 65500; originals stay untouched.
        if max(width, sum(heights)) > 65500:
            scale = min(65500 / width, 65500 / sum(heights))
            width = max(1, int(width * scale))
            heights = [max(1, int(height * scale)) for height in heights]
            if sum(heights) > 65500:
                raise ValueError('详情图片数量过多，无法合成 JPEG 长图')
        with Image.new('RGB', (width, sum(heights)), 'white') as canvas:
            top = 0
            for image, height in zip(images, heights):
                with image.convert('RGB').resize((width, height), Image.Resampling.LANCZOS) as scaled:
                    canvas.paste(scaled, (0, top))
                top += height
            save_product(canvas, Path(output))
