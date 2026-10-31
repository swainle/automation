"""通过 Sheets API 分批读取电子表格，不解析 Wiki。"""

import json
import os
import re
from urllib.parse import quote, urlparse


class SheetRecord(dict):
    """单元格字段保持原样，另存飞书原始行号，不占用表头字段。"""
    def __init__(self, fields, *, row_number):
        super().__init__(fields)
        self.row_number = row_number


def column_name(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def fetch_records(service):
    from automation.com.feishu.values import CredentialError
    import lark_oapi as lark

    token = os.getenv("FEISHU_SPREADSHEET_TOKEN", "").strip()
    url = urlparse(os.getenv("FEISHU_WIKI_URL", ""))
    if not token and url.scheme == "https" and (url.hostname or "").endswith(".feishu.cn"):
        token = url.path.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise CredentialError("请配置 FEISHU_SPREADSHEET_TOKEN 或有效的 FEISHU_WIKI_URL")
    client = (lark.Client.builder().app_id(service.app_id).app_secret(service.app_secret)
              .timeout(30).log_level(lark.LogLevel.CRITICAL).build())

    def get(uri):
        request = (lark.BaseRequest.builder().http_method(lark.HttpMethod.GET)
                   .uri(uri).token_types({lark.AccessTokenType.TENANT}).build())
        response = client.request(request)
        if not response.success():
            service.checked(response, "电子表格读取")
        payload = json.loads(response.raw.content)
        if not isinstance(payload.get("data"), dict):
            raise CredentialError("电子表格接口返回空数据")
        return payload["data"]

    base = f"/open-apis/sheets/v2/spreadsheets/{quote(token, safe='')}"
    sheets = get(base + "/metainfo").get("sheets", [])
    sheet_id = os.getenv("FEISHU_SHEET_ID", "").strip()
    if sheet_id:
        candidates = [sheet for sheet in sheets if sheet.get("sheetId") == sheet_id]
        if len(candidates) != 1:
            raise CredentialError("配置的 FEISHU_SHEET_ID 不在当前电子表格中")
        sheet = candidates[0]
    elif len(sheets) == 1:
        sheet = sheets[0]
    else:
        choices = "；".join(f"{s.get('title', '')} ({s.get('sheetId', '')})" for s in sheets)
        raise CredentialError(f"请配置 FEISHU_SHEET_ID。可用工作表：{choices or '无'}")
    return read_sheet_records(get, base, sheet, service)


def read_sheet_records(get, base, sheet, service):
    from automation.com.feishu.values import CredentialError, text_value

    sheet_id = sheet.get("sheetId")
    rows, columns = sheet.get("rowCount"), sheet.get("columnCount")
    if not sheet_id or not isinstance(rows, int) or not isinstance(columns, int) or rows < 1 or columns < 1:
        raise CredentialError("工作表元信息缺少有效的 sheetId 或行列数")
    try:
        header_row = int(os.getenv("FEISHU_HEADER_ROW", "1"))
    except ValueError:
        raise CredentialError("FEISHU_HEADER_ROW 必须为正整数") from None
    if not 1 <= header_row <= rows:
        raise CredentialError("FEISHU_HEADER_ROW 超出工作表行数")

    def values(area):
        data = get(base + "/values/" + quote(f"{sheet_id}!{area}", safe="!:"))
        result = data.get("valueRange", {}).get("values")
        if not isinstance(result, list) or any(not isinstance(row, list) for row in result):
            raise CredentialError("电子表格读取结果缺少有效 values 数组")
        return result

    header_values = values(f"A{header_row}:{column_name(columns)}{header_row}")
    headers = [text_value(cell).strip() for cell in header_values[0]] if header_values else []
    required = [service.shop_field, service.account_field, service.password_field]
    if len(set(required)) != 3:
        raise CredentialError("店铺名、账号、密码必须配置为三个不同列名")
    for name in required:
        if not name or headers.count(name) != 1:
            raise CredentialError(f"表头缺少或重复配置的列：{name}")
    positions = {name: headers.index(name) for name in required}
    if headers.count("店铺管理人员") == 1:
        positions["店铺管理人员"] = headers.index("店铺管理人员")
    first, last = min(positions.values()), max(positions.values())
    # 按元信息总行数完整读取，不因中间空行提前结束；每批最多 500 行。
    records = []
    for start in range(header_row + 1, rows + 1, 500):
        end = min(start + 499, rows)
        for row_number, row in enumerate(values(f"{column_name(first + 1)}{start}:{column_name(last + 1)}{end}"), start):
            record = SheetRecord({name: row[index - first] if index - first < len(row) else ""
                                  for name, index in positions.items()}, row_number=row_number)
            if text_value(record[service.shop_field]).strip():
                records.append(record)
    return records
