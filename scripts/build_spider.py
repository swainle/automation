"""Build the standalone Windows spider executable into dist/."""
from pathlib import Path
import shutil
import sys

import PyInstaller.__main__


def main():
    root = Path(__file__).resolve().parents[1]
    if sys.platform != 'win32':
        raise SystemExit('请在 Windows 上构建 Windows EXE')
    PyInstaller.__main__.run([
        '--noconfirm', '--onefile', '--console', '--name', 'automation.pdd.spider',
        '--distpath', str(root / 'dist'), '--workpath', str(root / 'build/spider'),
        '--specpath', str(root / 'build'), '--paths', str(root / 'src'),
        '--add-data', f'{root / "src/automation/pdd/spider/mobile_schema.sql"}:automation/pdd/spider',
        '--add-data', f'{root / "src/automation/pdd/spider/schema.sql"}:automation/pdd/spider',
        '--add-data', f'{root / "src/automation/pdd/spider/assets"}:automation/pdd/spider/assets',
        '--collect-all', 'psycopg_binary',
        str(root / 'scripts/spider_entry.py'),
    ])
    from automation.pdd.spider.initialize import initialize
    business = root / 'dist/automation.pdd.spider'
    initialize(business)
    # go-ios is a public helper; certificates, secrets and existing business data stay external.
    from automation.pdd.spider.config import TOOLS_ROOT
    source = TOOLS_ROOT / 'vendor/go-ios/ios.exe'
    if source.is_file():
        target = business / 'tools/vendor/go-ios/ios.exe'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f'已生成：{root / "dist/automation.pdd.spider.exe"}')
    print(f'外部配置和数据目录：{business}')


if __name__ == '__main__':
    main()
