"""待到账金额校验及每日截图清单导出；归档旧表，原名始终为最新。"""
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import date
import re

import xlsxwriter
from xlsxwriter.image import Image
from automation.com.files import publish_file


def parse_amount(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        raise ValueError("receivable_amount_invalid")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("receivable_amount_invalid") from None
    if not amount.is_finite() or amount < 0 or amount >= Decimal("1000000000000"):
        raise ValueError("receivable_amount_invalid")
    return format(amount, ".2f")


def export_receivables(root, day, records):
    folder = Path(root) / "data" / day
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "店铺应收截图.xlsx"
    path = target.with_name(target.name + ".tmp")
    stream = path.open("wb")
    try:
        with stream, xlsxwriter.Workbook(stream, {"strings_to_formulas": False, "strings_to_urls": False}) as book:
            sheet = book.add_worksheet("店铺应收截图")
            title = book.add_format({"bg_color": "#83DAD5", "align": "center", "valign": "vcenter"})
            header = book.add_format({"border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
            cell = book.add_format({"border": 1, "valign": "vcenter", "text_wrap": True})
            money = book.add_format({"border": 1, "valign": "vcenter", "num_format": "#,##0.00"})
            report_day = date.fromisoformat(day)
            sheet.merge_range(0, 0, 0, 5, f"截止到{report_day.month}.{report_day.day}", title)
            sheet.write_row(1, 0, ["平台名称", "店铺名称", "平台店铺实际盘点应收金额（待到账金额）",
                                   "附注图片/退店备注", "店铺管理人员", "异常情况"], header)
            sheet.set_row(0, 24)
            sheet.set_row(1, 42)
            for column, width in enumerate((14, 38, 30, 65, 18, 36)):
                sheet.set_column(column, column, width)
            sheet.freeze_panes(2, 0)
            for index, row in enumerate(records, 2):
                sheet.write_row(index, 0, ["拼多多", row["shop"], "", "", row.get("manager", ""), row.get("exception", "")], cell)
                if row["amount"] is not None:
                    sheet.write_number(index, 2, float(Decimal(parse_amount(row["amount"]))), money)
                image = Path(row["image_path"]) if row.get("image_path") else None
                if image and image.is_file():
                    sheet.set_row(index, 150)
                    picture = Image(str(image))
                    scale = min(447 / picture.width, 192 / picture.height, 1)
                    sheet.insert_image(index, 3, str(image), {"x_offset": 4, "y_offset": 4,
                        "object_position": 1, "x_scale": scale, "y_scale": scale})
                elif row.get("image_path"):
                    sheet.write_string(index, 5, "; ".join(filter(None, [row.get("exception", ""), "截图文件不存在"])), cell)
            sheet.autofilter(1, 0, len(records) + 1, 5)
        return publish_file(path, target)
    except BaseException:
        stream.close()
        path.unlink(missing_ok=True)
        raise

