"""从本地记录组装报表，保留各调用方的导出时机。"""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from automation.com.files import publish_file
from automation.pdd.verification import VERIFICATION_LABELS
from automation.pdd.screenshot.shops import read_source_shops, shop_identity
from automation.pdd.screenshot.workbook import export_receivables


def report_row(shop, manager, record, state):
    status = state.get("status", "pending")
    reason = "" if status == "success" else VERIFICATION_LABELS.get(status, state.get("reason") or status)
    return {"shop": "【拼多多】" + shop_identity(shop), "manager": manager,
            "amount": record.get("amount"), "image_path": record.get("image_path", ""), "exception": reason}


def format_daily_xlsx(root, day, *, database=None):
    """从当天数据库及人员来源表重建，不读取旧输出表。"""
    from automation.pdd.screenshot.format_xlsx import format_report_file
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
        image = Path(record['image_path']) if record['image_path'] else None
        if image and not image.is_absolute():
            image = root / image
        rows.append(report_row(key, source['manager'],
                               {'amount': record['amount'], 'image_path': str(image) if image else ''}, dict(record)))
    folder = root / 'data' / day
    folder.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.rebuild-', dir=folder) as temporary:
        temp_root = Path(temporary)
        export_receivables(temp_root, day, rows)
        generated = format_report_file(temp_root, day)
        return publish_file(generated, folder / '店铺应收截图.xlsx')
