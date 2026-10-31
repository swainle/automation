"""Shared Feishu cell conversion and safe errors."""
import math


class CredentialError(Exception):
    """可安全输出的配置或接口错误。"""


def text_value(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float):
            if not math.isfinite(value):
                return ""
            if value.is_integer():
                return str(int(value))
        return str(value)
    if isinstance(value, list) and all(isinstance(v, dict) and isinstance(v.get("text"), str) for v in value):
        return "".join(v["text"] for v in value)
    return ""


