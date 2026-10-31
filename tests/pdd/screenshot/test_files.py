from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from automation.com.files import publish_file, write_versioned


def test_old_versions_are_numbered_and_original_name_is_latest():
    with TemporaryDirectory() as folder:
        for filename in ('数据截图.jpg', '店铺应收截图.xlsx', 'cookies.json', 'new.json', '[店铺].json'):
            target = Path(folder) / filename
            write_versioned(target, b'first')
            write_versioned(target, b'second')
            write_versioned(target, b'latest')
            assert target.read_bytes() == b'latest'
            assert target.with_name(f'{target.stem}.1{target.suffix}').read_bytes() == b'first'
            assert target.with_name(f'{target.stem}.2{target.suffix}').read_bytes() == b'second'


def test_failed_publish_restores_original_file():
    with TemporaryDirectory() as folder:
        target = Path(folder) / 'cookies.json'
        temp = Path(folder) / 'cookies.json.tmp'
        target.write_bytes(b'old')
        temp.write_bytes(b'new')
        with patch.object(Path, 'replace', side_effect=OSError('fixture')):
            with pytest.raises(OSError):
                publish_file(temp, target)
        assert target.read_bytes() == b'old'


def test_numbering_continues_above_existing_archives():
    with TemporaryDirectory() as folder:
        target = Path(folder) / 'new.json'
        target.write_bytes(b'old')
        (Path(folder) / 'new.7.json').write_bytes(b'archive')
        write_versioned(target, b'latest')
        assert (Path(folder) / 'new.8.json').read_bytes() == b'old'
        assert (Path(folder) / 'new.7.json').read_bytes() == b'archive'
