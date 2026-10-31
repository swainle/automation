"""仅爬取并保存本地数据。"""
from automation.pdd.screenshot import main

if __name__ == "__main__":
    raise SystemExit(main(crawl_only=True))
