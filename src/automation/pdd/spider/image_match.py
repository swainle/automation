"""Conservative historical product matching using local image fingerprints."""
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

from automation.pdd.spider.point import list_images


@lru_cache(maxsize=20000)
def _fingerprint(path, size, modified):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        exact = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
        gray = image.convert('L').resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(gray.get_flattened_data())
        bits = sum((pixels[y*9+x] > pixels[y*9+x+1]) << (y*8+x) for y in range(8) for x in range(8))
        color = [round(v) for v in ImageStat.Stat(image.resize((1, 1))).mean]
        return {'exact': exact, 'hash': bits, 'color': color, 'ratio': image.width / image.height,
                'informative': ImageStat.Stat(gray).var[0] >= 64}


class ImageIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.dirty = False
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.entries = data['images'] if data.get('version') == 1 else {}
        except (OSError, ValueError, KeyError, TypeError):
            self.entries = {}

    def images(self, directory):
        if not Path(directory).is_dir():
            raise FileNotFoundError(f'商品图片目录不存在：{directory}')
        result = []
        for kind, name, filename in list_images(str(directory), 100000):
            path = Path(filename).resolve()
            stat = path.stat()
            stamp = [stat.st_size, stat.st_mtime_ns]
            saved = self.entries.get(str(path))
            if not saved or saved.get('stamp') != stamp:
                saved = dict(stamp=stamp, **_fingerprint(str(path), *stamp))
                self.entries[str(path)] = saved
                self.dirty = True
            result.append(dict(kind=kind, name=name, **saved))
        return result

    def save(self):
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'version': 1, 'images': self.entries}), encoding='utf-8')
            temporary.replace(self.path)
            self.dirty = False


def signature(images):
    return hashlib.sha256(json.dumps(sorted((i['kind'], i['name'], i['exact']) for i in images)).encode()).hexdigest()


def similar(a, b):
    if a['exact'] == b['exact']:
        return True
    # shortcut: crops and substantial layout changes remain unmatched; use calibrated image features if needed.
    return ((a['hash'] ^ b['hash']).bit_count() <= 3
            and max(abs(x-y) for x, y in zip(a['color'], b['color'])) <= 12
            and abs(a['ratio'] - b['ratio']) / max(a['ratio'], b['ratio']) <= .04)


def compare(current, previous, common):
    current = list({(i['kind'], i['exact']): i for i in current if i.get('informative') and i['exact'] not in common}.values())
    previous = list({(i['kind'], i['exact']): i for i in previous if i.get('informative') and i['exact'] not in common}.values())
    edges = [(a['exact'] != b['exact'], a, b) for a in current for b in previous
             if a['kind'] == b['kind'] and similar(a, b)]
    edges.sort(key=lambda e: e[0])
    used_current, used_previous, evidence = set(), set(), []
    for approximate, a, b in edges:
        if a['exact'] in used_current or b['exact'] in used_previous:
            continue
        used_current.add(a['exact'])
        used_previous.add(b['exact'])
        evidence.append({'image_type': a['kind'], 'file': a['name'], 'source_file': b['name'],
                         'method': 'similar' if approximate else 'exact'})
    coverage = len(evidence) / max(1, min(len(current), len(previous)))
    kinds = {e['image_type'] for e in evidence}
    qualified = len(evidence) >= 3 and len(kinds) >= 2 and coverage >= .6
    if any(e['method'] == 'similar' for e in evidence):
        qualified = qualified and len(evidence) >= 5 and coverage >= .8
    return evidence if qualified else []


def choose_match(images, candidates):
    owners = defaultdict(set)
    for candidate in candidates:
        for image in candidate['images']:
            owners[image['exact']].add(candidate['platform_product_id'])
    common = {key for key, ids in owners.items() if len(ids) >= 3}
    matches = []
    for candidate in candidates:
        evidence = compare(images, candidate['images'], common)
        if evidence:
            matches.append((candidate, evidence))
    ids = {candidate['platform_product_id'] for candidate, _ in matches}
    if len(ids) > 1:
        return 'conflict', None, {'candidate_ids': sorted(c['id'] for c, _ in matches)}
    if not matches:
        informative = [i for i in images if i.get('informative')]
        result = 'insufficient' if len({i['exact'] for i in informative}) < 3 or len({i['kind'] for i in informative}) < 2 else 'unmatched'
        return result, None, {}
    candidate, evidence = max(matches, key=lambda pair: (len(pair[1]), pair[0]['id']))
    return 'matched', candidate, {'images': evidence}
