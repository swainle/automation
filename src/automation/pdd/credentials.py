"""只读飞书电子表格及店铺账号匹配，不记录账号密码。"""

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


from automation.com.feishu.values import CredentialError, text_value


@dataclass(frozen=True)
class ShopCredential:
    shop_name: str
    account: str = field(repr=False)
    password: str = field(repr=False)
    row_number: int = 0
    manager: str = ""


@dataclass(frozen=True)
class MatchResult:
    cookie_path: Path
    shop_name: str
    status: str
    credential: ShopCredential | None = field(default=None, repr=False)


def cookie_shop_name(path: Path) -> str:
    if path.suffix.lower() != ".json":
        raise CredentialError("Cookie 文件必须为 JSON")
    if path.stem.lower() == "cookies":
        # data/{店铺名}/{日期}/cookies.json
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.parent.name):
            raise CredentialError("cookies.json 需要位于 店铺名/日期 目录下")
        name = path.parent.parent.name.strip()
    else:
        name = re.sub(r"(?:[_ -]?cookies?)$", "", path.stem.strip(), flags=re.I).strip()
    if not name:
        raise CredentialError("无法从 Cookie 文件名提取店铺名")
    return name


def match_records(paths, records, shop_field, account_field, password_field):
    shops = defaultdict(list)
    for fields in records:
        name = text_value(fields.get(shop_field)).strip()
        if name:
            shops[name].append(fields)
    results = []
    for path in paths:
        name = cookie_shop_name(path)
        candidates = shops.get(name, [])
        if not candidates:
            results.append(MatchResult(path, name, "未找到对应店铺，已跳过"))
        elif len(candidates) != 1:
            results.append(MatchResult(path, name, "存在重复店铺记录，已跳过"))
        else:
            account = text_value(candidates[0].get(account_field)).strip()
            password = text_value(candidates[0].get(password_field))
            if not account or not password.strip():
                results.append(MatchResult(path, name, "账号或密码为空/不是文本，已跳过"))
            else:
                credential = ShopCredential(name, account, password, getattr(candidates[0], "row_number", 0))
                results.append(MatchResult(path, name, "已匹配账号密码", credential))
    return results


class FeishuCredentials:
    def __init__(self, root: Path):
        load_dotenv(root / ".env")
        self.app_id = os.getenv("FEISHU_APP_ID", "").strip()
        self.app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        self.configured = bool(self.app_id and self.app_secret)
        self.shop_field = os.getenv("FEISHU_SHOP_FIELD", "店铺名").strip()
        self.account_field = os.getenv("FEISHU_ACCOUNT_FIELD", "账号").strip()
        self.password_field = os.getenv("FEISHU_PASSWORD_FIELD", "密码").strip()

    @staticmethod
    def checked(response, operation):
        if not response.success():
            # 不打印接口响应、SDK 异常原文或字段值。
            if response.code == 99991672:
                scopes = sorted(set(re.findall(r"\b(?:sheets|drive):[a-z_:]+", response.msg or "")))
                hint = "；可选权限：" + "、".join(scopes) if scopes else ""
                raise CredentialError(f"飞书{operation}失败：应用缺少接口权限（99991672）{hint}。请在飞书开放平台开通并发布应用版本")
            raise CredentialError(f"飞书{operation}失败（错误码 {response.code}），请检查应用权限及文档授权")
        if response.data is None:
            raise CredentialError(f"飞书{operation}返回空数据")
        return response.data

    def fetch_records(self):
        if not self.configured:
            raise CredentialError("请在 .env 填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        from automation.com.feishu.sheets import fetch_records

        return fetch_records(self)

    def match(self, paths):
        try:
            return match_records(paths, self.fetch_records(), self.shop_field, self.account_field, self.password_field)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(f"飞书读取异常（{type(exc).__name__}），请检查网络及配置") from None


def match_shop_credentials(root: Path, paths, required=False):
    service = FeishuCredentials(root)
    if not service.configured and not required:
        print("未配置飞书应用凭据，暂仅使用 Cookie。", flush=True)
        return None
    results = service.match(paths)
    for result in results:
        print(f"{result.shop_name}：{result.status}", flush=True)
    return results
