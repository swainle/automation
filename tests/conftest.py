"""Keep real browser tests opt-in through an explicit pytest file or node ID."""
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    selected = {str(Path(arg.split("::", 1)[0]).resolve()) for arg in config.args}
    for item in items:
        if item.get_closest_marker("integration") and str(item.path.resolve()) not in selected:
            item.add_marker(pytest.mark.skip(reason="请显式指定测试文件或 ::test_函数，以操作真实浏览器"))
