```cmd
uv run python -m automation.pdd.spider.sogou
uv run python -m automation.pdd.spider.ranking
uv run python -m automation.pdd.spider.ranking --ios
uv run python -m automation.pdd.spider.product
uv run python -m automation.pdd.screenshot --init
uv run python -m automation.pdd.screenshot
uv run python -m automation.pdd.screenshot --once
uv run python -m automation.pdd.screenshot --skip-sms-shop
uv run python -m automation.pdd.screenshot --once --skip-sms-shop
uv run python -m automation.pdd.screenshot.crawl
uv run python -m automation.pdd.screenshot.crawl --once
uv run python -m automation.pdd.screenshot.crawl --skip-sms-shop
uv run python -m automation.pdd.screenshot.crawl --once --skip-sms-shop
uv run python -m automation.pdd.screenshot.export
uv run python -m automation.pdd.screenshot.report
uv run python -m automation.pdd.screenshot.cookies
uv run python -u -m automation.pdd.screenshot.cookies --remote
uv run python -u -m automation.pdd.screenshot.cookies --remote --lease 30
uv run python -u -m automation.pdd.screenshot.remote --root "..\automation.pdd.screenshot" --port 8787 --lease 30
uv run python -m automation.pdd.screenshot --help
uv run python -m automation.pdd.screenshot.crawl --help
uv run python -m automation.pdd.screenshot.export --help
uv run python -m automation.pdd.screenshot.report --help
uv run python -m automation.pdd.screenshot.cookies --help
uv run python -m automation.pdd.screenshot.remote --help
```
