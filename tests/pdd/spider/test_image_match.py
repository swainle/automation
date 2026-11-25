"""Regression coverage for the retired image matcher used by offline benchmarks."""
from pathlib import Path
import random

from PIL import Image

from automation.pdd.spider.image_match import ImageIndex, choose_match
from tests.pdd.spider.test_analysis import write_images


def candidate(index, directory, pid, row_id):
    return dict(id=row_id, platform_product_id=pid, images=index.images(directory))


def test_image_matching_requires_multiple_categories_and_unique_platform_id(tmp_path):
    current, old = tmp_path / 'current', tmp_path / 'old'
    write_images(current)
    write_images(old)
    index = ImageIndex(tmp_path / 'cache.json')
    images = index.images(current)
    source = candidate(index, old, '123', 1)
    result, matched, evidence = choose_match(images, [source])
    assert result == 'matched' and matched['id'] == 1 and len(evidence['images']) == 4
    assert choose_match(images[:1], [source])[0] == 'insufficient'
    other_link = dict(source, id=2, platform_product_id='456')
    assert choose_match(images, [source, other_link])[0] == 'conflict'
    index.save()
    assert ImageIndex(index.path).images(current) == images


def test_one_image_repeated_in_many_files_cannot_prove_identity(tmp_path):
    write_images(tmp_path / 'current')
    index = ImageIndex(tmp_path / 'cache.json')
    image = index.images(tmp_path / 'current')[0]
    repeated = [dict(image, kind=kind, name=f'{n}.png') for n, kind in enumerate(('main', 'sku', 'detail'))]
    assert choose_match(repeated, [dict(id=1, platform_product_id='123', images=repeated)])[0] != 'matched'


def test_jpeg_recompression_matches_with_enough_independent_images(tmp_path):
    old, current = tmp_path / 'old', tmp_path / 'current'
    for n, kind in enumerate(('主图', '主图', 'SKU', '详情', '详情', 'SKU')):
        rng = random.Random(n + 100)
        image = Image.new('RGB', (32, 32))
        image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(1024)])
        image = image.resize((256, 256), Image.Resampling.NEAREST)
        (old / kind).mkdir(parents=True, exist_ok=True)
        (current / kind).mkdir(parents=True, exist_ok=True)
        image.save(old / kind / f'{n}.png')
        image.save(current / kind / f'{n}.jpg', quality=90)
    index = ImageIndex(tmp_path / 'cache.json')
    images = index.images(current)
    result, source, evidence = choose_match(images, [candidate(index, old, '123', 1)])
    assert result == 'matched' and all(e['method'] == 'similar' for e in evidence['images'])


