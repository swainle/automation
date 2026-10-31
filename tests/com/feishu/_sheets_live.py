"""飞书电子表格只读实测：uv run python tests/test_feishu_sheets_live.py。"""

import json
import os
from pathlib import Path
import re
import unittest
import unicodedata
from urllib.parse import quote, urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]


def ascii_table(headers, rows):
    """ASCII 边框，按中文显示宽度对齐；控制字符不进入终端。"""
    def clean(value):
        return "".join(c if c.isprintable() else " " for c in str(value))

    def width(value):
        return sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in value)

    values = [[clean(cell) for cell in row] for row in [headers, *rows]]
    widths = [max(width(row[i]) for row in values) for i in range(len(headers))]
    border = "+" + "+".join("-" * (size + 2) for size in widths) + "+"
    lines = [border]
    for index, row in enumerate(values):
        lines.append("| " + " | ".join(cell + " " * (size - width(cell))
                                      for cell, size in zip(row, widths)) + " |")
        if index == 0:
            lines.append(border)
    lines.append(border)
    return "\n".join(lines)


@unittest.skipUnless(
    __name__ == "__main__" or os.getenv("RUN_FEISHU_LIVE_TEST") == "1",
    "真实飞书请求仅在直接运行此文件或 RUN_FEISHU_LIVE_TEST=1 时执行",
)
class FeishuSheetsLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_dotenv(ROOT / ".env")
        import lark_oapi as lark

        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        token = os.getenv("FEISHU_SPREADSHEET_TOKEN", "").strip()
        if not token:
            # 对照影刀：直接使用链接末尾的标识，不调用 Wiki 或 Bitable。
            url = urlparse(os.getenv("FEISHU_WIKI_URL", ""))
            if url.scheme == "https" and (url.hostname or "").endswith(".feishu.cn"):
                token = url.path.rstrip("/").rsplit("/", 1)[-1]
        if not app_id or not secret or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise AssertionError("请在根目录 .env 配置飞书应用凭据及表格标识/链接")
        cls.token = token
        cls.client = (lark.Client.builder().app_id(app_id).app_secret(secret)
                      .timeout(30).log_level(lark.LogLevel.CRITICAL).build())

    def get_data(self, uri):
        import lark_oapi as lark

        request = (lark.BaseRequest.builder().http_method(lark.HttpMethod.GET)
                   .uri(uri).token_types({lark.AccessTokenType.TENANT}).build())
        try:
            response = self.client.request(request)
        except Exception as exc:
            self.fail(f"请求失败（{type(exc).__name__}），异常详情已隐藏以避免泄露凭据")
        if not response.success():
            scopes = sorted(set(re.findall(r"\b(?:sheets|drive|wiki):[a-z_:]+", response.msg or "")))
            self.fail(f"Sheets API 失败：HTTP {response.raw.status_code}，code={response.code}"
                      + ("，可选权限=" + ", ".join(scopes) if scopes else ""))
        try:
            payload = json.loads(response.raw.content)
        except (ValueError, TypeError):
            self.fail("接口未返回有效 JSON，响应内容不输出")
        self.assertTrue(isinstance(payload.get("data"), dict), "接口缺少 data 对象")
        return payload["data"]

    def test_list_worksheets_v3(self):
        """直接以影刀 Spreadsheet 标识查询工作表，不经过 Wiki。"""
        data = self.get_data(f"/open-apis/sheets/v3/spreadsheets/{quote(self.token, safe='')}/sheets/query")
        sheets = data.get("sheets")
        self.assertTrue(isinstance(sheets, list) and bool(sheets), "未返回工作表列表")
        print(f"\nSheets v3 查询成功：共 {len(sheets)} 个工作表（不输出单元格内容）", flush=True)
        rows = []
        for sheet in sheets:
            grid = sheet.get("grid_properties") or {}
            rows.append([sheet.get("title", ""), sheet.get("sheet_id", ""),
                         grid.get("row_count", "-"), grid.get("column_count", "-")])
        print(ascii_table(["工作表名称", "Sheet ID", "行数", "列数"], rows), flush=True)

    def test_read_cells_v2(self):
        """通过 v2 元信息获取 sheetId，再读取与影刀相同的 A1:B2 区域。"""
        token = quote(self.token, safe="")
        metadata = self.get_data(f"/open-apis/sheets/v2/spreadsheets/{token}/metainfo")
        sheets = metadata.get("sheets")
        self.assertTrue(isinstance(sheets, list) and bool(sheets), "v2 元信息未返回工作表")
        sheet_id = os.getenv("FEISHU_SHEET_ID", "").strip()
        if sheet_id:
            self.assertTrue(any(s.get("sheetId") == sheet_id for s in sheets), "配置的 FEISHU_SHEET_ID 不在当前表格中")
        else:
            sheet_id = sheets[0].get("sheetId")
        self.assertTrue(isinstance(sheet_id, str) and bool(sheet_id), "工作表缺少 sheetId")
        area = os.getenv("FEISHU_TEST_RANGE", "A1:B2").strip()
        self.assertTrue(bool(re.fullmatch(r"[A-Z]+[1-9]\d*:[A-Z]+[1-9]\d*", area)), "测试区域格式应为 A1:B2")
        cell_range = quote(f"{sheet_id}!{area}", safe="!:")
        data = self.get_data(f"/open-apis/sheets/v2/spreadsheets/{token}/values/{cell_range}")
        values = data.get("valueRange", {}).get("values")
        self.assertTrue(isinstance(values, list), "读取结果缺少 values 数组")
        rows = len(values)
        columns = max((len(row) for row in values if isinstance(row, list)), default=0)
        print(f"\nSheets v2 读取成功：区域 {area}，返回 {rows} 行、最多 {columns} 列；账号密码不输出", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
