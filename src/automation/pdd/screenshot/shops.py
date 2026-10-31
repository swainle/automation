"""店铺名称归一化及人员来源表读取。"""
from zipfile import ZipFile
from xml.etree import ElementTree as ET

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def shop_identity(name):
    return name.strip().removeprefix('【拼多多】').strip()


def read_source_shops(path):
    if not path.is_file():
        raise ValueError(f'管理人员来源表不存在：{path}')
    shops = {}
    found = False
    with ZipFile(path) as archive:
        strings = [''.join(t.text or '' for t in item.iter(f'{{{S}}}t'))
                   for item in ET.fromstring(archive.read('xl/sharedStrings.xml'))] if 'xl/sharedStrings.xml' in archive.namelist() else []
        for name in archive.namelist():
            if not name.startswith('xl/worksheets/sheet') or not name.endswith('.xml'):
                continue
            columns = None
            for row in ET.fromstring(archive.read(name)).findall(f'{{{S}}}sheetData/{{{S}}}row'):
                values = {}
                for cell in row:
                    value = cell.findtext(f'{{{S}}}v', '')
                    if cell.get('t') == 's':
                        value = strings[int(value)]
                    elif cell.get('t') == 'inlineStr':
                        value = ''.join(item.text or '' for item in cell.iter(f'{{{S}}}t'))
                    values[cell.get('r').rstrip('0123456789')] = value.strip()
                if '店铺名称' in values.values() and '店铺管理人员' in values.values():
                    columns = {value: col for col, value in values.items()}
                    found = True
                    continue
                if columns is None:
                    continue
                shop = values.get(columns['店铺名称'], '')
                platform = values.get(columns.get('平台名称'), '')
                if not shop or (platform and platform != '拼多多') or (shop.startswith('【') and not shop.startswith('【拼多多】')):
                    continue
                key = shop_identity(shop)
                manager = values.get(columns['店铺管理人员'], '')
                if key in shops:
                    raise ValueError(f'来源表同一店铺重复或管理人员不一致：{shop}')
                shops[key] = {'manager': manager, 'row_number': int(row.get('r'))}
    if not found:
        raise ValueError('来源表缺少“店铺名称”和“店铺管理人员”表头')
    if not shops:
        raise ValueError('来源表没有拼多多店铺')
    return shops
