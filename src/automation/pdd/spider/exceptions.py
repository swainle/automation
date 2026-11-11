"""自定义异常。"""


class RiskControlError(Exception):
    """疑似风控：页面长时间未加载出内容，应停止抓取而非反复刷新。"""
    pass
