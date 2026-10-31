"""就地格式化本项目当天报表，保留单元格数据及其他工作簿内容。"""
from io import BytesIO
from copy import deepcopy
from pathlib import Path
import posixpath
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

from uuid import uuid4
import sqlite3
from tempfile import TemporaryDirectory
from xlsxwriter.image import Image

from automation.com.files import publish_file

S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
D = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
C = 'http://schemas.openxmlformats.org/package/2006/content-types'
W = 'http://www.wps.cn/officeDocument/2017/etCustomData'


def rich_image(parts, cell):
    """读取上一版标准单元格图片的完整关系链。"""
    metadata = ET.fromstring(parts['xl/metadata.xml'])
    value = metadata.find(f'{{{S}}}valueMetadata')[int(cell.get('vm')) - 1]
    index = int(value.find(f'{{{S}}}rc').get('v'))
    future = metadata.find(f'{{{S}}}futureMetadata[@name="XLRICHVALUE"]')[index]
    rich_index = int(next(item for item in future.iter() if item.tag.endswith('}rvb')).get('i'))
    rich = ET.fromstring(parts['xl/richData/rdrichvalue.xml'])[rich_index]
    rel_index = int(list(rich)[0].text)
    rel_id = ET.fromstring(parts['xl/richData/richValueRel.xml'])[rel_index].get(f'{{{R}}}id')
    rel_path = 'xl/richData/_rels/richValueRel.xml.rels'
    if rel_path not in parts:
        # 兼容本项目上一版遗漏关系文件的产物；仅恢复其固定命名的唯一图片。
        expected = f'xl/media/formatted_image{rel_index + 1}.'
        candidates = [name for name in parts if name.startswith(expected) and '/' not in name[len(expected):]]
        if rel_id != f'rId{rel_index + 1}' or len(candidates) != 1:
            raise ValueError('旧单元格图片缺少关系文件且无法唯一恢复，原文件未修改')
        return parts[candidates[0]]
    rels = ET.fromstring(parts[rel_path])
    rel = next(item for item in rels if item.get('Id') == rel_id)
    return parts[resolve('xl/richData/richValueRel.xml', rel.get('Target'))]


def embed_wps(parts, sheet, rows, images, relations):
    root = ET.fromstring(parts['xl/cellimages.xml']) if 'xl/cellimages.xml' in parts else ET.Element(f'{{{W}}}cellImages')
    rels_path = 'xl/_rels/cellimages.xml.rels'
    rels = ET.fromstring(parts[rels_path]) if rels_path in parts else ET.Element(f'{{{P}}}Relationships')
    used = {item.get('Id') for item in rels}
    for row_number, raw in images:
        row = rows[row_number]
        cell = next((item for item in row if item.get('r') == f'D{row_number}'), None)
        if cell is None:
            cell = ET.SubElement(row, f'{{{S}}}c', r=f'D{row_number}')
        elif not cell.get('vm') and (cell.find(f'{{{S}}}f') is not None or cell.find(f'{{{S}}}v') is not None):
            raise ValueError('图片目标单元格已有内容，未修改文件')
        identity = 'ID_' + uuid4().hex.upper()
        picture = Image(BytesIO(raw))
        filename = f'wps_{identity}.{picture.image_type.lower()}'
        parts['xl/media/' + filename] = raw
        number = 1
        while f'rId{number}' in used:
            number += 1
        rid = f'rId{number}'
        used.add(rid)
        ET.SubElement(rels, f'{{{P}}}Relationship', Id=rid, Type=R + '/image', Target='media/' + filename)
        pic = ET.SubElement(ET.SubElement(root, f'{{{W}}}cellImage'), f'{{{D}}}pic')
        nv = ET.SubElement(pic, f'{{{D}}}nvPicPr')
        ET.SubElement(nv, f'{{{D}}}cNvPr', id=str(len(root) + 1), name=identity, descr='数据截图')
        ET.SubElement(ET.SubElement(nv, f'{{{D}}}cNvPicPr'), f'{{{A}}}picLocks', noChangeAspect='1')
        fill = ET.SubElement(pic, f'{{{D}}}blipFill')
        ET.SubElement(fill, f'{{{A}}}blip', {f'{{{R}}}embed': rid})
        ET.SubElement(ET.SubElement(fill, f'{{{A}}}stretch'), f'{{{A}}}fillRect')
        props = ET.SubElement(pic, f'{{{D}}}spPr')
        transform = ET.SubElement(props, f'{{{A}}}xfrm')
        ET.SubElement(transform, f'{{{A}}}off', x='0', y='0')
        ET.SubElement(transform, f'{{{A}}}ext', cx=str(round(picture.width * 9525)), cy=str(round(picture.height * 9525)))
        ET.SubElement(ET.SubElement(props, f'{{{A}}}prstGeom', prst='rect'), f'{{{A}}}avLst')
        for child in list(cell):
            cell.remove(child)
        cell.attrib.pop('vm', None)
        cell.set('t', 'str')
        ET.SubElement(cell, f'{{{S}}}f').text = f'_xlfn.DISPIMG("{identity}",1)'
        ET.SubElement(cell, f'{{{S}}}v').text = f'=DISPIMG("{identity}",1)'
        row[:] = sorted(row, key=lambda item: (len(item.get('r', '').rstrip('0123456789')), item.get('r', '').rstrip('0123456789')))
    parts['xl/cellimages.xml'], parts[rels_path] = xml(root), xml(rels)
    if not any(item.get('Type') == 'http://www.wps.cn/officeDocument/2020/cellImage' for item in relations):
        used_ids = {item.get('Id') for item in relations}
        number = 1
        while f'rId{number}' in used_ids:
            number += 1
        ET.SubElement(relations, f'{{{P}}}Relationship', Id=f'rId{number}',
                      Type='http://www.wps.cn/officeDocument/2020/cellImage', Target='cellimages.xml')
    parts['xl/_rels/workbook.xml.rels'] = xml(relations)
    types = ET.fromstring(parts['[Content_Types].xml'])
    if not any(item.get('PartName') == '/xl/cellimages.xml' for item in types):
        ET.SubElement(types, f'{{{C}}}Override', PartName='/xl/cellimages.xml', ContentType='application/vnd.wps-officedocument.cellimage+xml')
    for _, raw in images:
        extension = Image(BytesIO(raw)).image_type.lower()
        if not any(item.get('Extension') == extension for item in types):
            ET.SubElement(types, f'{{{C}}}Default', Extension=extension, ContentType='image/' + ('jpeg' if extension == 'jpg' else extension))
    parts['[Content_Types].xml'] = xml(types)


def resolve(part, target):
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target)) if not target.startswith('/') else target.lstrip('/')


def xml(root):
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def format_report_file(root, day):
    target = Path(root) / 'data' / day / '店铺应收截图.xlsx'
    if not target.is_file():
        raise ValueError(f'当天报表不存在：{target}')
    with ZipFile(target) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    workbook = ET.fromstring(parts['xl/workbook.xml'])
    sheets = workbook.findall(f'{{{S}}}sheets/{{{S}}}sheet')
    selected = [sheet for sheet in sheets if sheet.get('name') == '店铺应收截图']
    if len(selected) != 1:
        raise ValueError('未找到唯一的“店铺应收截图”工作表')
    relations = ET.fromstring(parts['xl/_rels/workbook.xml.rels'])
    relation = next(item for item in relations if item.get('Id') == selected[0].get(f'{{{R}}}id'))
    sheet_path = resolve('xl/workbook.xml', relation.get('Target'))
    sheet = ET.fromstring(parts[sheet_path])
    data = sheet.find(f'{{{S}}}sheetData')
    rows = {int(row.get('r')): row for row in data}
    images = []
    drawing = sheet.find(f'{{{S}}}drawing')
    if drawing is not None:
        rel_path = posixpath.join(posixpath.dirname(sheet_path), '_rels', posixpath.basename(sheet_path) + '.rels')
        rels = ET.fromstring(parts[rel_path])
        drawing_rel = next(item for item in rels if item.get('Id') == drawing.get(f'{{{R}}}id'))
        drawing_path = resolve(sheet_path, drawing_rel.get('Target'))
        drawing_root = ET.fromstring(parts[drawing_path])
        drawing_rels_path = posixpath.join(posixpath.dirname(drawing_path), '_rels', posixpath.basename(drawing_path) + '.rels')
        drawing_rels = ET.fromstring(parts[drawing_rels_path])
        for anchor in list(drawing_root):
            start = anchor.find(f'{{{D}}}from')
            picture = anchor.find(f'{{{D}}}pic')
            if start is None or picture is None or start.findtext(f'{{{D}}}col') != '3':
                continue
            row_number = int(start.findtext(f'{{{D}}}row')) + 1
            if row_number < 3:
                continue
            blip = picture.find(f'.//{{{A}}}blip')
            rel = next(item for item in drawing_rels if item.get('Id') == blip.get(f'{{{R}}}embed'))
            images.append((row_number, parts[resolve(drawing_path, rel.get('Target'))]))
            drawing_root.remove(anchor)
        parts[drawing_path] = xml(drawing_root)
        if not len(drawing_root):
            sheet.remove(drawing)
    if len({row for row, _ in images}) != len(images):
        raise ValueError('同一图片单元格存在多张浮动图片，未修改文件')
    for cell in sheet.findall(f'.//{{{S}}}c'):
        if cell.get('vm') and cell.get('r', '').startswith('D'):
            images.append((int(cell.get('r')[1:]), rich_image(parts, cell)))
    if len({row for row, _ in images}) != len(images):
        raise ValueError('同一图片单元格存在多张图片，未修改文件')
    if images:
        embed_wps(parts, sheet, rows, images, relations)
    cols = sheet.find(f'{{{S}}}cols')
    if cols is not None:
        sheet.remove(cols)
    cols = ET.Element(f'{{{S}}}cols')
    for index, width in enumerate((20,) * 6, 1):
        ET.SubElement(cols, f'{{{S}}}col', min=str(index), max=str(index), width=str(width), customWidth='1')
    sheet.insert(list(sheet).index(data), cols)
    for number, row in rows.items():
        row.set('ht', str(24 if number == 1 else 42 if number == 2 else 26))
        row.set('customHeight', '1')
    styles = ET.fromstring(parts['xl/styles.xml'])
    formats = styles.find(f'{{{S}}}cellXfs')
    replacements = {}
    for cell in sheet.findall(f'.//{{{S}}}c'):
        original = int(cell.get('s', '0'))
        if original not in replacements:
            style = deepcopy(formats[original])
            alignment = style.find(f'{{{S}}}alignment')
            if alignment is None:
                alignment = ET.SubElement(style, f'{{{S}}}alignment')
            alignment.set('wrapText', '0')
            style.set('applyAlignment', '1')
            replacements[original] = len(formats)
            formats.append(style)
        cell.set('s', str(replacements[original]))
    formats.set('count', str(len(formats)))
    parts['xl/styles.xml'] = xml(styles)
    parts[sheet_path] = xml(sheet)
    temp = target.with_name(target.name + '.format.tmp')
    try:
        with ZipFile(temp, 'w', ZIP_DEFLATED) as output:
            for name, raw in parts.items():
                output.writestr(name, raw)
        return publish_file(temp, target)
    finally:
        temp.unlink(missing_ok=True)


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


def read_managers(path):
    return {key: value['manager'] for key, value in read_source_shops(path).items()}


def format_daily_xlsx(root, day, *, database=None):
    """从当天数据库及人员来源表重建，不读取旧输出表。"""
    from automation.pdd.screenshot.export import export_receivables
    from automation.pdd.screenshot.state import VERIFICATION_LABELS
    root = Path(root)
    source_shops = read_source_shops(root / 'data/店铺应收截图.xlsx')
    database = Path(database) if database is not None else root / 'data/sqlite.db'
    if not database.is_file():
        raise ValueError(f'数据库不存在：{database}')
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        records = connection.execute(
            'SELECT shop,amount,image_path,status,reason FROM daily_shops WHERE day=? '
            'ORDER BY row_number IS NULL,row_number,rowid', (day,)).fetchall()
    finally:
        connection.close()
    if not records:
        raise ValueError(f'数据库没有当天店铺记录：{day}')
    by_shop = {}
    for record in records:
        if record['shop'].strip().startswith('【') and not record['shop'].strip().startswith('【拼多多】'):
            continue
        key = shop_identity(record['shop'])
        if key not in source_shops:
            continue
        if key in by_shop:
            raise ValueError(f'数据库存在同名店铺记录：{key}')
        by_shop[key] = record
    rows = []
    for key, source in source_shops.items():
        record = by_shop.get(key, {'status': 'pending', 'reason': '当天无数据', 'image_path': '', 'amount': None})
        reason = '' if record['status'] == 'success' else VERIFICATION_LABELS.get(record['status'], record['reason'] or record['status'])
        image = Path(record['image_path']) if record['image_path'] else None
        if image and not image.is_absolute():
            image = root / image
        rows.append({'shop': '【拼多多】' + key, 'amount': record['amount'],
                     'image_path': str(image) if image else '', 'manager': source['manager'], 'exception': reason})
    folder = root / 'data' / day
    folder.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.rebuild-', dir=folder) as temporary:
        temp_root = Path(temporary)
        export_receivables(temp_root, day, rows)
        generated = format_report_file(temp_root, day)
        return publish_file(generated, folder / '店铺应收截图.xlsx')
