"""Create an editable business configuration without overwriting existing files."""
ENV_TEMPLATE = '''# All relative paths are resolved against this business directory.
SPIDER_SOURCE_XLSX=data/grab/pdd/拼多多榜单.xlsx
SPIDER_OUTPUT_DIR=data/grab/pdd
SPIDER_TOOLS_DIR=tools
SPIDER_SOGOU_EXECUTABLE="D:/Program Files/Sogou/SogouExplorer/SogouExplorer.exe"
SPIDER_SOGOU_USER_DATA_DIR="C:/Users/YD003/AppData/Local/Sogou/SogouExplorer/User Data"
# Shared Sogou extension settings for sogou/ranking/product.
# Public WS port is configured by AUTOMATION_WS_PORT in the project .env.
SPIDER_RANKING_READY_TIMEOUT=120
SPIDER_RANKING_WIDTH=363
SPIDER_RANKING_HEIGHT=684
SPIDER_RANKING_SCALE=0.5
SPIDER_RETRY_ATTEMPTS=3
SPIDER_RETRY_DELAY=2
SPIDER_PRODUCT_RETRY_DELAY=300
# Required primary database; tables use the spider schema and t_pdd_ prefix.
SPIDER_POSTGRES_URL=
# Optional upstream proxy. Browser response capture itself requires no standalone mitm.
SPIDER_PROXY=
SPIDER_MITM_PORT=8080
# Leave empty to discover the installed WDA bundle ID.
WDA_BUNDLE_ID=
# Optional USB device UDID; required when more than one device is connected.
SPIDER_IOS_UDID=
SPIDER_WDA_VERSION=16.15.3
# Native Android App (reuses ADB and UiAutomator).
SPIDER_ANDROID_SERIAL=
SPIDER_ANDROID_PORT=7912
# Optional absolute ADB executable path; default tools/vendor/android/adb.exe or PATH.
SPIDER_ADB_EXE=
# P12 password is prompted securely; optionally set SPIDER_P12_PASSWORD in the environment.
'''


def initialize(root):
    for relative in ('data/grab/pdd', 'logs', 'state', 'tools', 'tools/ios/signing', 'tools/ios/archive', 'tools/releases/wda'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    path = root / '.env'
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(ENV_TEMPLATE)
    except FileExistsError:
        pass
