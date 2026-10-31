import base64
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import pytest
import xlsxwriter
from types import SimpleNamespace
from unittest.mock import patch

from automation.pdd.screenshot.state import StateStore
from automation.pdd.screenshot.format_xlsx import format_daily_xlsx, S
from automation.pdd.screenshot.runner import load_shops, shop_folder_name


def source(path, rows):
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet()
        sheet.write_row(1, 0, ['店铺名称', '店铺管理人员'])
        for index, row in enumerate(rows, 2):
            sheet.write_row(index, 0, row)


def test_collection_uses_source_whitelist_order_and_actual_row_numbers(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    source(data / '店铺应收截图.xlsx', [['【拼多多】第二家', '甲'], ['【天猫】忽略', '乙'],
                                     ['【拼多多】第一家', '丙'], ['缺飞书账号店', '丁']])
    service = SimpleNamespace(shop_field='店铺名', account_field='账号', password_field='密码',
        fetch_records=lambda: [{'店铺名': '第一家', '账号': 'a', '密码': 'p'},
                               {'店铺名': '不在原表', '账号': '', '密码': ''},
                               {'店铺名': '第二家', '账号': 'b', '密码': 'q'},
                               {'店铺名': '【天猫】忽略'}])
    with patch('automation.pdd.screenshot.runner.FeishuCredentials', return_value=service):
        shops = load_shops(tmp_path)
    assert [shop.shop_name for shop in shops] == ['第二家', '第一家', '缺飞书账号店']
    assert [shop.row_number for shop in shops] == [3, 5, 6]
    assert [shop.manager for shop in shops] == ['甲', '丙', '丁']
    assert shop_folder_name(shops[1]) == '005第一家'
    assert shops[0].account == 'b'


def test_rebuild_ignores_old_report_and_reads_database_and_manager_source(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    source(data / '店铺应收截图.xlsx', [['【拼多多】有福之猫', '人员甲'], [' 缺图店 ', '人员乙']])
    image = data / 'shot.png'
    image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1sAAAAASUVORK5CYII='))
    store = StateStore(data / 'sqlite.db')
    store.seed('2026-10-31', ['有福之猫', '【拼多多】缺图店', '未匹配店'])
    store.save_receivable('2026-10-31', '有福之猫', image, '123.45')
    store.finish('2026-10-31', '有福之猫', 'success')
    store.finish('2026-10-31', '【拼多多】缺图店', 'sms_required')
    store.db.execute("UPDATE daily_shops SET row_number=2 WHERE shop='【拼多多】缺图店'")
    store.db.execute("UPDATE daily_shops SET row_number=3 WHERE shop='有福之猫'")
    store.db.commit()
    store.close()
    folder = data / '2026-10-31'
    folder.mkdir()
    target = folder / '店铺应收截图.xlsx'
    target.write_bytes(b'old output deliberately invalid')
    assert format_daily_xlsx(tmp_path, '2026-10-31') == target.resolve()
    assert target.with_name('店铺应收截图.1.xlsx').read_bytes() == b'old output deliberately invalid'
    with ZipFile(target) as archive:
        strings = [''.join(t.itertext()) for t in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        def value(ref):
            cell = sheet.find(f'.//{{{S}}}c[@r="{ref}"]')
            raw = cell.findtext(f'{{{S}}}v')
            return strings[int(raw)] if cell.get('t') == 's' else raw
        assert value('B3') == '【拼多多】有福之猫'
        assert value('E3') == '人员甲'
        assert value('C3') == '123.45'
        assert value('B4') == '【拼多多】缺图店'
        assert value('E4') == '人员乙'
        assert value('F4') == '需要短信验证'
        assert sheet.find(f'.//{{{S}}}c[@r="B5"]') is None
        assert 'xl/cellimages.xml' in archive.namelist()


def test_conflicting_manager_mapping_leaves_existing_output_untouched(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    source(data / '店铺应收截图.xlsx', [['店铺', '甲'], ['【拼多多】店铺', '乙']])
    folder = data / '2026-10-31'
    folder.mkdir()
    target = folder / '店铺应收截图.xlsx'
    target.write_bytes(b'original')
    with pytest.raises(ValueError, match='管理人员不一致'):
        format_daily_xlsx(tmp_path, '2026-10-31')
    assert target.read_bytes() == b'original'
    assert not target.with_name('店铺应收截图.1.xlsx').exists()
