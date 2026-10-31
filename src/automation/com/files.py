"""发布最新文件前，将旧版本按递增编号归档。"""
from pathlib import Path


def publish_file(temp, target):
    temp, target = Path(temp), Path(target)
    archived = None
    if target.exists():
        numbers = [int(path.stem[len(target.stem) + 1:])
                   for path in target.parent.iterdir()
                   if path.is_file() and path.suffix == target.suffix and path.stem.startswith(target.stem + ".")
                   and path.stem[len(target.stem) + 1:].isdigit()]
        number = max(numbers, default=0) + 1
        archived = target.with_name(f"{target.stem}.{number}{target.suffix}")
        target.rename(archived)
    try:
        temp.replace(target)
    except BaseException:
        if archived is not None:
            archived.rename(target)
        raise
    return target.resolve()


def write_versioned(target, raw):
    target = Path(target)
    temp = target.with_name(target.name + ".tmp")
    try:
        temp.write_bytes(raw)
        return publish_file(temp, target)
    finally:
        temp.unlink(missing_ok=True)
