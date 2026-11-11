"""榜单类（元素版）。"""
from automation.pdd.spider.rank.dom import (
    TAB_TO_CLASS,
    BestReviewed,
    BestSelling,
    Leaderboard,
    PriceDrop,
    get_leaderboard_name,
    get_period_info,
    get_tabs,
    normalize_url,
    read_leaderboards,
    set_active_tab,
)

__all__ = [
    # DOM 版
    "Leaderboard",
    "BestSelling",
    "BestReviewed",
    "PriceDrop",
    "TAB_TO_CLASS",
    # 榜单来源读取
    "read_leaderboards",
    "get_tabs",
    "get_leaderboard_name",
    "get_period_info",
    "set_active_tab",
    "normalize_url",
]
