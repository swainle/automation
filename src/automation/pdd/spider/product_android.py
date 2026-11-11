"""Native Android runtime; App, ADB, UiAutomator and forwards stay reusable."""
from automation.pdd.spider.logging_setup import timed_operation
from automation.pdd.spider.runtime import Runtime


class AndroidRuntime(Runtime):
    native_android = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.android = None

    def connect_product_browser(self):
        self.ensure_android()

    @timed_operation('[Android] 准备原生连接')
    def ensure_android(self):
        if self.android is None:
            from automation.pdd.spider.android import AndroidConnection
            connection = AndroidConnection()
            connection.prepare()
            self.android = connection.device
        return self.android

    def close(self):
        # Do not stop the shared device-side service, App or ADB server.
        self.android = None
        super().close()
