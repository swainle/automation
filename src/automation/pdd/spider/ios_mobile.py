"""Single-device iOS ranking and product collection."""
from types import SimpleNamespace


def run(*, only_webview=False):
    from automation.pdd.spider.commands import execute
    from automation.pdd.spider.weekly_mobile import collect_sources

    def collect(store, runtime):
        return collect_sources(store, runtime, only_webview=only_webview)
    return execute('全 iOS 每周榜单及商品采集', collect, args=SimpleNamespace(ios=True, mobile=True), role='rank')
