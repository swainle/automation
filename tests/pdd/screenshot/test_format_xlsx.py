import base64
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import pytest
import xlsxwriter

from automation.pdd.screenshot.workbook import export_receivables
from automation.pdd.screenshot.format_xlsx import format_report_file as format_daily_xlsx, S


def test_formats_existing_report_without_local_image_and_preserves_archive(tmp_path):
    day = '2026-10-31'
    image = tmp_path / 'image.png'
    image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1sAAAAASUVORK5CYII='))
    target = export_receivables(tmp_path, day, [{'shop': '测试店', 'amount': '12.34', 'image_path': str(image), 'manager': '管理人员'}])
    original = target.read_bytes()
    image.unlink()  # 从表内提取图片，不依赖数据库或本地截图。
    assert format_daily_xlsx(tmp_path, day) == target
    assert target.with_name('店铺应收截图.1.xlsx').read_bytes() == original
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        cell = sheet.find(f'.//{{{S}}}c[@r="D3"]')
        assert cell.get('vm') is None
        assert cell.find(f'{{{S}}}f').text.startswith('_xlfn.DISPIMG(')
        assert sheet.find(f'.//{{{S}}}c[@r="C3"]/{{{S}}}v').text == '12.34'
        assert sheet.find(f'.//{{{S}}}row[@r="3"]').get('ht') == '26'
        assert [col.get('width') for col in sheet.find(f'{{{S}}}cols')] == ['20'] * 6
        assert sheet.find(f'{{{S}}}drawing') is None
        assert 'xl/cellimages.xml' in archive.namelist()
        assert any(name.startswith('xl/media/wps_') for name in archive.namelist())
        assert '测试店' in archive.read('xl/sharedStrings.xml').decode()
        formats = ET.fromstring(archive.read('xl/styles.xml')).find(f'{{{S}}}cellXfs')
        for cell in sheet.findall(f'.//{{{S}}}c'):
            assert formats[int(cell.get('s'))].find(f'{{{S}}}alignment').get('wrapText') == '0'
    format_daily_xlsx(tmp_path, day)  # 已嵌入图片时重复格式化不丢图。
    with ZipFile(target) as archive:
        assert 'xl/cellimages.xml' in archive.namelist()
    assert target.with_name('店铺应收截图.2.xlsx').is_file()


def test_missing_report_does_not_create_files(tmp_path):
    with pytest.raises(ValueError, match='当天报表不存在'):
        format_daily_xlsx(tmp_path, '2026-10-31')
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('missing_relationships', [False, True])
def test_previous_standard_embedded_images_convert_to_wps(tmp_path, missing_relationships):
    day = '2026-10-31'
    folder = tmp_path / 'data' / day
    folder.mkdir(parents=True)
    image = tmp_path / 'shot.png'
    raw = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1sAAAAASUVORK5CYII=')
    image.write_bytes(raw)
    with xlsxwriter.Workbook(folder / '店铺应收截图.xlsx') as book:
        sheet = book.add_worksheet('店铺应收截图')
        sheet.embed_image(2, 3, str(image))
        sheet.write_number(2, 2, 25.68)
    if missing_relationships:
        target = folder / '店铺应收截图.xlsx'
        with ZipFile(target) as source:
            parts = {name.replace('xl/media/', 'xl/media/formatted_'): source.read(name)
                     for name in source.namelist() if name != 'xl/richData/_rels/richValueRel.xml.rels'}
        with ZipFile(target, 'w') as output:
            for name, data in parts.items():
                output.writestr(name, data)
    format_daily_xlsx(tmp_path, day)
    with ZipFile(folder / '店铺应收截图.xlsx') as archive:
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        cell = sheet.find(f'.//{{{S}}}c[@r="D3"]')
        assert cell.get('vm') is None
        assert cell.find(f'{{{S}}}f').text.startswith('_xlfn.DISPIMG(')
        names = [name for name in archive.namelist() if name.startswith('xl/media/wps_')]
        assert len(names) == 1
        assert archive.read(names[0]) == raw
