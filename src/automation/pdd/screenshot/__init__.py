"""每日店铺截图业务；运行时按需加载。"""
from importlib import import_module

__all__ = ["main", "run", "run_cycle", "run_interruptible", "watch_date"]


def __getattr__(name):
    if name in __all__:
        return getattr(import_module(".runtime", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
