"""创建业务目录和不含凭据的默认配置，已有文件保持不变。"""
from automation.pdd.screenshot.config import runtime_root
from automation.pdd.screenshot.logs import report


ENV_TEMPLATE = '''# 相对文件路径以本 .env 所在的业务工作目录为基准。
# 填写飞书应用凭据，并向应用授权电子表格。
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_SPREADSHEET_TOKEN=
FEISHU_SHEET_ID=
FEISHU_HEADER_ROW=1
FEISHU_SHOP_FIELD=店铺名
FEISHU_ACCOUNT_FIELD=账号
FEISHU_PASSWORD_FIELD=密码

# 本地 Cookie 源目录，存放 {店铺名}cookie.json；也支持绝对路径。
COOKIES_DIR=./data/cookies/Cookie
COOKIES_SERVER_HOST=127.0.0.1
COOKIES_SERVER_PORT=8787
AUTOMATION_WS_PORT=8765
AUTOMATION_READY_TIMEOUT=120
AUTOMATION_PAGE_TIMEOUT=30
AUTOMATION_RESTART_LIMIT=3

# false：每天循环；true：完成当天一轮后退出。
PDD_SCREENSHOT_ONCE=false

# 影刀安装目录，请按本机实际路径修改。
SHADOWBOT_CLI_DIR="C:/Program Files/ShadowBot"

# 可选远程 CS；HOST 留空时使用上面的本地 Cookie 目录。
CS_SSH_HOST=
CS_SSH_PORT=22
CS_SSH_USER=
CS_SSH_PASSWORD=
# 支持绝对路径或相对业务工作目录的路径；留空使用默认私钥/SSH agent。
CS_SSH_KEY=
CS_SSH_REMOTE_PORT=8787
# 远程启动命令，配置 HOST 时必填，含空格或 # 的值请加引号。
CS_SSH_COMMAND=
'''


def initialize(project_root=None):
    root = runtime_root(project_root)
    for directory in ("data/cookies/Cookie", "logs", "state", "migrate"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    path = root / ".env"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(ENV_TEMPLATE)
    except FileExistsError:
        report("初始化", f"已有配置已保留：{path}")
    else:
        report("初始化", f"已创建配置：{path}")
    report("初始化", f"业务工作目录：{root}")
    report("初始化", "请填写 .env 中的飞书配置并核对影刀 CLI 路径；准备 data/店铺应收截图.xlsx 和 Cookie 文件（或远程 CS），安装 Chrome 插件及影刀报表应用后再运行。")
    return root
