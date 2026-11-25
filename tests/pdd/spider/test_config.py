"""业务目录跟随启动工作目录，显式配置仍优先。"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("override", [False, True])
def test_business_root(tmp_path, override):
    env = dict(os.environ)
    env.pop("PDD_SPIDER_ROOT", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    expected = tmp_path / "automation.pdd.spider"
    if override:
        expected = tmp_path / "custom"
        env["PDD_SPIDER_ROOT"] = str(expected)
    result = subprocess.run(
        [sys.executable, "-c", "from automation.pdd.spider.config import BUSINESS_ROOT; print(BUSINESS_ROOT)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    assert Path(result.stdout.strip()) == expected


@pytest.mark.parametrize('override', [None, 'custom'])
def test_frozen_business_root_uses_executable_directory(tmp_path, override):
    env = dict(os.environ)
    env.pop('PDD_SPIDER_ROOT', None)
    if override:
        env['PDD_SPIDER_ROOT'] = override
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[3] / 'src')
    executable = tmp_path / 'dist/automation.pdd.spider.exe'
    code = ('import sys; sys.frozen=True; sys.executable=sys.argv[1]; '
            'from automation.pdd.spider.config import BUSINESS_ROOT; print(BUSINESS_ROOT)')
    result = subprocess.run([sys.executable, '-c', code, str(executable)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert Path(result.stdout.strip()) == executable.parent / (override or 'automation.pdd.spider')
