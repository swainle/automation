"""常量配置。"""
from pathlib import Path
import os

from dotenv import load_dotenv
from automation.config import load_environment

load_environment()

BUSINESS_ROOT = Path(os.getenv("PDD_SPIDER_ROOT", str(Path.cwd().resolve().parent / "automation.pdd.spider"))).expanduser().resolve()
load_dotenv(BUSINESS_ROOT / ".env")


def configured_path(name, default):
    path = Path(os.getenv(name, str(default))).expanduser()
    return (path if path.is_absolute() else BUSINESS_ROOT / path).resolve()


TOOLS_ROOT = configured_path("SPIDER_TOOLS_DIR", "tools")
# Historical SQLite input for one-time migration; never used by current collection.
DB_PATH = BUSINESS_ROOT / "data/sqlite.db"
POSTGRES_URL = os.getenv('SPIDER_POSTGRES_URL', '')

# 搜狗浏览器路径（仅用于手动启动命令，代码本身不启动浏览器）
SOGOU_EXECUTABLE = r"D:\Program Files\Sogou\SogouExplorer\SogouExplorer.exe"

# 搜狗用户数据目录（手动启动命令用，保证登录态/缩放等设置生效）
USER_DATA_DIR = r"C:\Users\YD003\AppData\Local\Sogou\SogouExplorer\User Data"

DEBUG_PORT = 9222
SOGOU_EXECUTABLE = os.getenv("SPIDER_SOGOU_EXECUTABLE", SOGOU_EXECUTABLE)
USER_DATA_DIR = os.getenv("SPIDER_SOGOU_USER_DATA_DIR", USER_DATA_DIR)
DEBUG_PORT = int(os.getenv("SPIDER_DEBUG_PORT", str(DEBUG_PORT)))
OUTPUT_ROOT = configured_path("SPIDER_OUTPUT_DIR", "data/grab/pdd")

# 榜单来源 Excel（B 来源、C 主榜、D 子榜、E 链接）
EXCEL_PATH = configured_path("SPIDER_SOURCE_XLSX", OUTPUT_ROOT / "拼多多榜单.xlsx")

# 抓取的产品数量上限（None = 全部）；调试时可改成 1 或 2
LIMIT = None

# 榜单固定列（所有榜单共用）
COMMON_HEADER = ["排行", "产品ID", "产品链接", "标题", "价格", "上架日期"]

# 产品页 URL 模板
GOODS_URL = "https://mobile.yangkeduo.com/goods.html?goods_id={}"

# 产品页截图裁剪宽度（居中，高度不变）
PRODUCT_CROP_WIDTH = 386

# 垂直滚动条宽度预设值（物理像素，获取不到时使用）
SCROLLBAR_WIDTH_PRESET = 17

# 产品页选择器
MAIN_IMG_XPATH = '//*[@id="main"]/div/div[1]/div[1]/div/div[{}]/img'
SKU_PANEL = '#main .sku-plus1'
# SKU 选项选中态标记 class（选项 class F7sZG3xe 之外多出此 class 表示已选中）
SKU_SELECTED_CLASS = "hr353bdX"

# 图片下载请求头
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/116.0.5845.97 Safari/537.36"
    ),
    "Referer": "https://mobile.yangkeduo.com/",
}

# ---- iOS 采集等待时间（网速慢可调大）----
# 打开网页后等待页面加载的时间（秒）
PAGE_LOAD_WAIT = 20.0
# 等待 landing_page 接口响应的超时（秒）
LANDING_WAIT = 20.0

# ---- mitmproxy 上游动态代理（绕过 IP 风控）----
# HTTP/HTTPS 代理，格式 http://user:pass@host:port；留空表示直连
MITM_UPSTREAM = ""
# 青果短效代理 key（优先于 MITM_UPSTREAM，自动提取短效 IP 作为上游）
MITM_KEY = ""
# 青果账密模式的 AuthPwd（配合 MITM_KEY，留空表示白名单模式）
MITM_PASSWORD = ""
# 切换 IP 的 API（留空表示不切换）；风控时调用后重试
SWITCH_IP_API = ""
SWITCH_IP_METHOD = "GET"  # GET / POST

MITM_UPSTREAM = os.getenv("SPIDER_PROXY", MITM_UPSTREAM)
MITM_PORT = int(os.getenv("SPIDER_MITM_PORT", "8080"))
RETRY_ATTEMPTS = int(os.getenv("SPIDER_RETRY_ATTEMPTS", "3"))
RETRY_DELAY = float(os.getenv("SPIDER_RETRY_DELAY", "2"))
PRODUCT_RETRY_DELAY = float(os.getenv("SPIDER_PRODUCT_RETRY_DELAY", "30"))
RANKING_WS_PORT = int(os.getenv('AUTOMATION_WS_PORT', '8765'))
RANKING_READY_TIMEOUT = float(os.getenv('SPIDER_RANKING_READY_TIMEOUT', '120'))
RANKING_WIDTH = int(os.getenv('SPIDER_RANKING_WIDTH', '363'))
RANKING_HEIGHT = int(os.getenv('SPIDER_RANKING_HEIGHT', '684'))
RANKING_SCALE = float(os.getenv('SPIDER_RANKING_SCALE', '0.5'))
