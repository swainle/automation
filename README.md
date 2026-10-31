# automation

通用基础服务位于 `src/automation/com/`，拼多多业务位于 `src/automation/pdd/`。
原每日逐店流程现为 `pdd.screenshot`；登录、截图、金额采集、SQLite 恢复及 Excel 导出行为保持一致。

## 统一入口

在代码项目目录执行 `uv run`。截图业务四个入口统一读取 `../automation.pdd.screenshot/.env`，以该文件所在目录为业务根目录，不再使用 `RPA_ROOT`。配置缺失时明确报错，不回退读取项目 `.env`；已有环境变量优先。影刀 CLI 目录由 `SHADOWBOT_CLI_DIR` 指定，开发机为 `C:/Program Files/ShadowBot`。

首次使用，在代码项目目录执行：

```powershell
uv run python -m automation.pdd.screenshot --init
```

初始化会创建相邻业务目录中的 `data/cookies/Cookie/`、`logs/`、`state/` 和内置模板 `.env`，不需要预先配置环境。已有目录和 `.env` 不覆盖，可以重复执行；完成后退出，不启动爬取、浏览器、服务或影刀任务。填写飞书配置、核对影刀 CLI 路径，并准备 `data/店铺应收截图.xlsx`、Cookie 文件（或远程 CS）、Chrome 插件及影刀应用后，再运行完整流程。通用模块独立运行仍沿用其原有工作目录配置规则。

```text
D:/repo/automation/                         项目代码
D:/repo/automation.pdd.screenshot/.env        业务配置
D:/repo/automation.pdd.screenshot/
  data/店铺应收截图.xlsx                    店铺及管理人员来源表
  data/YYYY-MM-DD/                         当天截图、Cookie、导出报表
  logs/YYYY-MM-DD/                         当天日志
  state/sqlite.db                         跨日数据库
  state/automation.lock                   业务共享运行锁
```

旧数据不自动搬动。停止旧流程后，将项目 `data/店铺应收截图.xlsx` 复制至业务 `data/`，通过 SQLite 备份方式将旧 `data/sqlite.db` 迁至业务 `state/sqlite.db`，按需复制历史日期目录。数据库内已有绝对图片路径继续引用旧文件，因此核实前保留旧目录；相对图片路径以新业务根目录为基准。CS Cookie 源目录仍由 `COOKIES_DIR` 配置，截图业务中的相对路径以业务工作目录为基准。

```powershell
# 完整流程：爬取 → 导出 → 同步运行影刀报表
uv run python -m automation.pdd.screenshot
# 仅爬取到本地
uv run python -m automation.pdd.screenshot.crawl --skip-sms-shop --once
# 仅从当天本地数据库导出
uv run python -m automation.pdd.screenshot.export
# 仅同步运行影刀报表
uv run python -m automation.pdd.screenshot.report
# 独立启动 Cookie 服务，读取相邻业务目录 .env
uv run python -m automation.pdd.screenshot.cookies
```

完整流程和爬取入口默认常驻、北京时间跨日重启，均支持 `--once`、`--skip-sms-shop`。完整流程启动前检查影刀路径，每天正常爬取结束后导出，成功后调用影刀应用 `f00b7263-9e1b-4e17-af4c-cc35676fc44d`。影刀使用 CLI 默认同步模式等待结束（默认最长 10 分钟）；失败退出并保留本地结果。爬取异常或验证上限退出时，本地状态仍保存，可单独导出；不会启动影刀。单独爬取、导出不要求影刀配置。

```powershell
uv sync --locked
uv run automation com.cookies_server
uv run automation com.extension_bridge
uv run python -m automation.pdd.screenshot
```

工作流默认常驻；在 `.env` 设置 `PDD_SCREENSHOT_ONCE=true` 可完成当天一轮后退出。
截图功能仅使用独立模块入口，统一 CLI 不再提供 `pdd.screenshot` 子命令。可运行 `uv run python -m automation.pdd.screenshot --skip-sms-shop`，跳过当天已有 `sms_required` 记录的店铺；不加参数则重新处理。首次遇到短信验证仍保存状态并继续下一家，其他验证及连续第 6 家退出规则保持不变。
运行 `uv run python -m automation.pdd.screenshot.export` 从业务 `state/sqlite.db` 只读查询北京时间当天全部店铺记录，以来源表拼多多店铺为白名单并按原表顺序，使用数据库金额、状态、图片路径重新生成 `data/日期/店铺应收截图.xlsx`，不参考旧输出表格，不启动浏览器、CS、WS，不读取飞书。管理人员仅从 `data/店铺应收截图.xlsx` 的“店铺名称”“店铺管理人员”列匹配；匹配忽略 `【拼多多】` 前缀和首尾空格，原表外店铺及天猫不输出，原表内当天无记录保留行并注明当天无数据，同店铺重复或人员冲突时报错。输出店铺名统一为 `【拼多多】店铺名`。WPS 图片使用 `cellimages.xml` 和 `DISPIMG`，数据行高 26 磅，A～F 列宽统一为 20，文字不自动换行；标题行和表头行分别为 24、42 磅。关闭 WPS 中已打开的目标文件后再运行；重建成功后旧文件归档为 `.n.xlsx`，原名保存最新结果。人员来源表、数据库或当天记录缺失时报错，保留旧输出。
验证状态分为 `slider_required`（滑块）、`sms_required`（短信/手机验证码）、`verification_other`（其他身份验证）。本轮不重试并继续下一家；当天重新运行跳过 `success`、`login_error`，重新处理这三类验证状态并更新当天原记录。连续计数从 0 开始，三类合计连续第 6 家时退出；报表分别显示“需要滑块验证”“需要短信验证”“需要其他身份验证”。同时出现滑块和短信提示时，优先记录滑块验证。
旧 `verification_required` 记录执行 `../automation.pdd.screenshot/migrate/20261031134959_split_verification_status.sql` 归为 `verification_other`，保留原异常原因；旧三表数据库先执行合表迁移，再执行此脚本。迁移前停止程序并备份数据库，插件需重新加载。
进入后台并核对店铺后，会先关闭通知弹窗、点击“查看金额”，等金额显示后再截图。
通知清理支持“今日平台已支持您以下申诉，打款成功…”弹窗，通过内容及弹窗内关闭按钮定位，不依赖 `/html/body/div[5]` 顺序；关闭后复查弹窗已消失。
同样支持标题为“请选择您的主要发货地”的弹窗，仅点击关闭按钮，不选择或提交发货地；关闭按钮不唯一时停止该动作，避免误点。
支持“现有 N 笔订单，您可一键发起维权申诉！”提示（订单数可变），仅关闭，不发起申诉。
截图前仅匹配顶部同时包含“安全守护”和“安装”的推广横幅：优先关闭，没有关闭按钮则临时隐藏，截图完成或失败后恢复隐藏前的样式。当前采用文本和尺寸限定，纯图片且无文字的横幅需要进一步定位。
已确认的 `.mms-main-safe-tip` 和 `.mms-main-safe-content` 会同时临时隐藏，包括自身高度为 0、子内容溢出的展开层，截图后分别恢复原样式。
每日流程生成的 JPG、Excel、`cookies.json`、`new.json` 始终使用原文件名保存最新版；已有同名文件先归档为 `文件名.1.扩展名`、`文件名.2.扩展名`，编号递增。新文件生成失败时不归档原文件，发布失败时尝试恢复原文件。
每日 Excel 按飞书顺序列出平台名称、店铺名称、平台店铺实际盘点应收金额（待到账金额）、附注图片/退店备注、店铺管理人员、异常情况；截图按比例嵌入报表，失败店铺也保留行。
店铺名称保留飞书原文（含 `【拼多多】` 前缀），后台身份核对及 Cookie 文件查找去掉此前缀；管理人员读取飞书的 `店铺管理人员` 列。
SQLite 仅使用 `daily_shops`，保存日期、店铺名、飞书行号、管理人员、处理状态、异常原因、尝试次数、清理结果、金额、图片路径和更新时间。过程步骤及事件写日志。
连续实际处理的第 6 家店铺需要验证码时，先保存状态并清理，再退出程序；需要报表时单独运行 export；跳过的店铺不参与计数，其他处理结果归零。每个日期轮次重新计数。
旧三表数据库需先停止程序、备份数据库，再执行 `../automation.pdd.screenshot/migrate/20261031134259_merge_daily_shops.sql`（仅执行一次，适用于包含 `manager`、`row_number` 的上一版结构）；程序会提示迁移，不自动删除旧表。迁移保留任务及金额图片记录，旧事件明细仅保留在备份中。
`daily_shops.row_number` 保存飞书原始行号；每次读取店铺列表后更新当天记录，包括已完成并跳过的店铺。旧库自动补列，未重新读取的历史记录保持 NULL。
WS 端口统一使用 `AUTOMATION_WS_PORT`，其他现有配置名继续保留，见 `--init` 生成的业务 `.env`。
完整工作流自行管理 CS、WS 与 Chrome，启动前先停止单独运行的 CS、WS；启动时会关闭现有 Chrome 并准备配置。

`uvx automation ...` 要求包已发布到所用索引或安装为本机工具。本地使用本项目版本，可在仓库执行：

```powershell
uv tool install --editable .
uvx automation com.cookies_server
uvx automation com.extension_bridge
uv run python -m automation.pdd.screenshot
```

也可不安装工具，使用 `uvx --from . automation com.cookies_server` 指定本地源码。
不要在尚未安装本项目时直接调用裸 `uvx automation`，以免解析到索引中的其他同名包。

## pytest

```powershell
# 本地回归，默认不操作真实浏览器或飞书
uv run pytest

# 显式指定这些节点才操作真实浏览器；参数从 .env 读取
uv run pytest tests/pdd/test_screenshot.py::test_open_browser
uv run pytest tests/pdd/test_screenshot.py::test_extension_connection
uv run pytest tests/pdd/test_screenshot.py::test_close_notification -s
uv run pytest tests/pdd/test_screenshot.py::test_screenshot -s

# Chrome 插件本地回归
node --test tests/extension/*.mjs
```

上述浏览器测试分别执行打开浏览器、检查插件连接、保存截图，不自动启动整个工作流。
旧的单功能诊断脚本仍可直接执行，路径已迁移到下述目录。

## 目录

```text
src/automation/
├─ cli.py                  统一命令分发
├─ config.py               当前工作目录和 .env 配置
├─ com/
│  ├─ browser/             Chrome 启动及 CDP 连接
│  ├─ cookies_server/      Cookie 文件 HTTP API
│  ├─ extension_bridge/    WebSocket 服务
│  ├─ extension/           Python 插件客户端及图片保存
│  └─ feishu/              飞书表格读取
└─ pdd/
   ├─ screenshot/          每日逐店工作流、服务管理、SQLite、Excel
   ├─ storage/             拼多多 Cookie 和 localStorage 操作
   ├─ credentials.py       店铺账号匹配
   ├─ login.py             拼多多登录
   └─ captcha/             验证码图像分析与演示页诊断
extension/                 独立 Chrome 插件
tests/
├─ com/                    通用服务测试
├─ pdd/                    拼多多业务与工作流测试
└─ extension/              插件单项与集成测试
业务数据和日志位于 ../automation.pdd.screenshot/
```

以下为各模块详细说明；`python -m automation...` 是保留的模块诊断入口。

Python 3.14，单一 uv 项目。先执行 `uv sync --locked`。

## 模块职责

| 模块 | 功能 |
|---|---|
| `automation.pdd.screenshot` | 服务管理、飞书店铺列表、每日 SQLite 状态、登录与工作台截图 |
| `automation.com.browser` | 默认只打开本机 Chrome，不设置用户目录、不读取飞书、不导入 Cookie、不导航后台 |
| `automation.com.extension_bridge` | 默认启动本机 WebSocket 服务，转发插件任务 |
| `automation.com.extension` | 插件 Python 调用侧；Chrome 插件源码在 `extension/` |
| `automation.com.feishu` | Sheets 读取和店铺账号匹配，通过测试调用 |
| `automation.pdd.captcha` | 候选缺口识别；保留旧 CDP 诊断测试 |
| `automation.com.cookies_server` | Cookie 文件 HTTP 接口：列表、下载、上传 |

`automation.pdd.screenshot` 提供统一的每日逐店流程；其他模块仍可单独运行，具体操作也可通过 `tests/{模块}/test_xxx.py` 验证。

## 默认启动

```powershell
uv run python -m automation.com.browser
uv run python -m automation.com.extension_bridge
```

browser 调用本机 Chrome，可复用日常配置中的已安装插件，不依赖 Playwright 启动浏览器。
bridge 默认监听 `ws://127.0.0.1:8765/`，可在 `.env` 中通过 `AUTOMATION_WS_PORT` 修改。插件安装后自动连接，断线每 3 秒重试，不需要 Token；使用自定义端口时也需修改插件连接地址。
Chrome 插件修改后请在 `chrome://extensions` 点击重新加载。安装方法见 [插件说明](extension/README.md)。

browser 可选参数：`--url 地址`、`--headless`、`--debug-port 9222`、`--user-data-dir 路径`。
这些选项均不默认添加。需要调试端口时，必须显式指定独立用户目录；普通默认目录 Chrome 不能直接用于 CDP Cookie 测试。

```powershell
uv run python -m automation.com.browser --debug-port 9222 --user-data-dir "data/User Data" --url "https://mms.pinduoduo.com/home/"
```

若相同用户目录已被 Chrome 使用，先手动关闭对应实例。`--headless` 只影响新实例，不能改变已经运行的浏览器模式。

## Cookie 文件 HTTP 服务

`cookies_server` 提供 Cookie 文件的列表、下载与上传接口，仅监听本机、无鉴权。

```powershell
uv run automation com.cookies_server
```

默认监听 `http://127.0.0.1:8787/`，Swagger 文档位于 `http://127.0.0.1:8787/docs`；数据目录 `data/cookies/`，均可用 `.env` 覆盖：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `COOKIES_DIR` | `data/cookies` | 数据目录，支持绝对路径或相对项目根目录的相对路径 |
| `COOKIES_SERVER_PORT` | `8787` | 监听端口 |
| `COOKIES_SERVER_HOST` | `127.0.0.1` | 监听地址（固定本机） |

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/cookies` | 列出顶层 Cookie 文件，`?dir=子目录` 列子目录；列表不含 value |
| GET | `/cookies/{name}` | 直接下载原始 JSON（含 value），`name` 为相对路径含 `.json` |
| POST | `/cookies` | multipart 上传，字段 `file` 必填，`dir`/`name` 可选，同名覆盖 |

```powershell
# 列出顶层文件
curl http://127.0.0.1:8787/cookies
# 列出子目录
curl "http://127.0.0.1:8787/cookies?dir=黄金万两"
# 下载
curl -o 一往无前宠物用品店cookie.json http://127.0.0.1:8787/cookies/一往无前宠物用品店cookie.json
# 上传到顶层 / 子目录
curl -F "file=@data/cookies/一往无前宠物用品店cookie.json" http://127.0.0.1:8787/cookies
curl -F "dir=黄金万两" -F "file=@data/cookies/一往无前宠物用品店cookie.json" http://127.0.0.1:8787/cookies
```

上传要求合法 JSON 且为对象（含非空 `cookies` 数组）或非空数组；`dir`/`name` 拒绝绝对路径、盘符、`..` 及 `\`，并限定在数据目录内，保留中文文件名。规则测试见 `tests/com/cookies_server/`。

## 单功能手动测试

先保持 WS 服务和已安装插件的 Chrome 运行，再开终端执行：

```powershell
# 只检查 WS 和插件是否连接
uv run python tests/com/extension_bridge/test_connection.py

# 只读取元素信息，不截图
uv run python tests/extension/test_get_element.py --selector ".captcha-wrapper"

# 只截图，不额外执行元素查询测试
uv run python tests/extension/test_screenshot.py
uv run python tests/extension/test_screenshot.py --selector ".captcha-wrapper"
uv run python tests/extension/test_screenshot.py --full-page
```

元素测试参数：`--port 8765`、`--tab-id 数字`、`--selector body`、`--selector-type css|xpath`、`--text`、`--output-dir 路径`。
截图测试参数：`--port 8765`、`--tab-id 数字`、`--selector 选择器`、`--selector-type css|xpath`、`--full-page`、`--output 文件路径`。
`--selector` 和 `--full-page` 在截图测试中互斥。多个允许页面时，用 `uv run python -m automation.com.extension_bridge tabs` 查看 tab_id。
元素结果保存到 `data/extension-tests/element/时间戳/elements.json`；截图保存到 `data/extension-tests/screenshot/时间戳/screenshot.png`。

Cookie 测试通过 Playwright 直接连接已启动的 9222 调试浏览器，不经过 WS，不启动浏览器、不导航页面：

```powershell
uv run python tests/pdd/browser/test_import_cookies.py --cookies "一往无前宠物用品店cookie.json"
uv run python tests/pdd/browser/test_export_cookies.py --output "data/export/cookies.json"
```

两者支持 `--port 9222`。只处理拼多多域名，导入时清理原有拼多多 Cookie，其他站点 Cookie 保留；不在控制台输出 Cookie 内容。
导入后页面不会自动刷新。`tests/pdd/browser/test_launch.py` 直接运行时只启动 Chrome，也支持与 browser 相同参数。

飞书测试读取根目录 .env，不通过 WS，也不操作浏览器：

```powershell
uv run python tests/com/feishu/test_list_sheets.py
uv run python tests/com/feishu/test_read_sheet.py
uv run python tests/com/feishu/test_match_shop.py
uv run python tests/com/feishu/test_match_shop.py --cookies-dir .
```

.env 配置：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_WIKI_URL` 或 `FEISHU_SPREADSHEET_TOKEN`、`FEISHU_SHEET_ID`、三个列名字段。
`FEISHU_TEST_RANGE` 是读取测试范围，默认 A1:B2；`FEISHU_HEADER_ROW` 是正式读取的表头行，默认 1。
使用 Sheets API，直接使用链接末尾标识，不调用 Wiki 或 Bitable。账号密码只留在内存，不输出或落盘。

验证码诊断仍使用原有 Playwright/CDP 实现，尚未接入插件拖动：

新增按钮关联检测走插件和 WS，只读不拖动。重启 WS 服务并在 chrome://extensions 重新加载插件后运行：

```powershell
uv run python tests/pdd/captcha/test_detect.py --button-class "slide-button"
uv run python tests/pdd/captcha/test_detect.py --button-xpath "/html/body/div[4]/div/div/div[1]"
```

class 可带前导点；多个 class 使用空格分隔（例如 `"slide-button active"`），不是任意 CSS 表达式。
`--button-class` 与 `--button-xpath` 互斥且必选一个。另支持 `--port 8765`、`--tab-id 数字`、`--output-dir 路径`。
检测先确认唯一可见按钮，最多向上检查 8 层、每层最多 500 个节点，寻找背景、拼图、轨道的最近共同容器。
支持 img、canvas 与 CSS 背景的候选发现；canvas 内部拼图无法通过 DOM 独立定位。iframe/Shadow DOM 暂不支持。
状态为 detected、incomplete、ambiguous、button_not_found 或 ambiguous_button；只有 detected 且截图成功才通过测试。
结果写入 `data/extension-tests/captcha-detect/时间戳/` 的 `detection.json`、`screenshot.png` 和 `diagnostic.png`。
标注图用 button/background/piece/track 区分元素。结果是布局启发式检测，不表示验证码已通过；测试不会拖动、刷新或改动页面。

原有识别/拖动诊断仍可运行：

```powershell
uv run python tests/pdd/captcha/test_recognize.py
uv run python tests/pdd/captcha/test_drag.py
```

识别测试只分析、不拖动；拖动测试按综合分较低优先规则只尝试一次，不自动重试。两者支持 `--port 9222`，拖动支持 `--timeout 8`。
结果保存在 `data/captcha_tests/时间戳/`。候选分数不是通过概率。

## 本地回归及专门集成测试

```powershell
uv run python -m unittest discover -s tests -v
node --test tests/extension/test_protocol.mjs
uv run python tests/extension/test_integration.py
uv run python tests/extension/test_integration.py --headed
```

普通 discover 只运行本地规则和 mock 测试，不打开真实浏览器、不访问飞书、不执行验证码。
`test_integration.py` 是明确的集成测试例外，验证免 Token 连接、断线重连、CSS/XPath、表单值排除及三种截图，并调用两个单功能测试入口。
集成测试使用独立的 Chrome for Testing 配置及本地测试页，不操作日常浏览器。首次缺少测试浏览器时：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD/data/browsers"
uv run playwright install chromium
```

插件未提供通用点击、输入、拖动接口；拼多多账号登录、截图与金额采集使用限定的业务接口。

## 独立账号登录测试

重启 WS 服务并重新加载 0.4.0 版插件，手动打开拼多多登录页，再运行：

```powershell
# 只检查页面，不读取飞书凭据、不切换、不填写、不点击
uv run python tests/pdd/browser/test_login.py --dry-run

# 从飞书匹配默认店铺，切换账号登录、填写并提交一次
uv run python tests/pdd/browser/test_login.py

# 选择其他店铺，文件名只用于匹配，不导入 Cookie
uv run python tests/pdd/browser/test_login.py --cookies "其他店铺cookie.json"
```

默认店铺文件名为 `一往无前宠物用品店cookie.json`。
支持 `--port 8765`、`--tab-id 数字`、`--timeout 15`、`--cookies 路径`、`--dry-run`、`--output-dir 路径`。
默认只选择拼多多页面，因此同时打开易盾不受影响；多个拼多多页面时需指定 tab-id。
通过插件操作 #usernameId 和 #passwordId，确认唯一表单后点击“登录”一次；不使用 Playwright。
飞书凭据配置沿用 .env，按店铺精确匹配；账号密码不经命令行传入、不输出、不落盘。
结果保存在 `data/extension-tests/login/时间戳/result.json`，不额外截图。
出现验证码、身份验证或失败时停止；结果不确定也不会重复提交。正式测试仅在确认进入工作台后通过。
--timeout 仅指提交后的等待时间，不含飞书读取。测试不打开/关闭浏览器、不导入或清除 Cookie，浏览器原有 Cookie 保留。

## 通过插件独立测试 Cookie

重新加载 0.5.0 版扩展（新增 cookies 和拼多多所有子域权限），重启 WS 服务，保持浏览器和插件连接后运行：

```powershell
uv run python tests/extension/test_export_cookies.py --output "data/export/cookies.json"
uv run python tests/extension/test_clear_cookies.py
uv run python tests/extension/test_import_cookies.py --cookies "data/export/cookies.json"
```

三个入口均支持 `--port 8765`，无需 CDP 调试端口、页面或 tab-id，不启动浏览器、不导航、不刷新。
只处理扩展当前默认 Cookie store 中的 `pinduoduo.com` 和所有子域，不处理其他域名或其他浏览器配置。
导出是 JSON 数组，空结果也会写出 `[]`；导入支持数组或包含 cookies 数组的对象，支持原有 expires/expirationDate、SameSite 格式。
导入先完整校验所有条目，再清除现有拼多多 Cookie，最后逐条写入；空文件、非拼多多条目和无效字段会拒绝；过期 Cookie 保留原时间提交浏览器，校验失败不清除。
会保留 hostOnly、path、secure、httpOnly、sameSite、会话/有效期及 partitionKey 信息；storeId 不从文件恢复，始终写入当前默认 store。
浏览器 Cookie API 不提供事务；清除或写入失败可能已部分完成，不自动重试，可先导出检查。
控制台不打印 Cookie 内容。建议先导出备份，再执行清除或导入。

本地 Cookie 回归：`node --test tests/extension/test_cookies.mjs`。

Cookie 业务由 `automation.pdd.storage.cookies` 提供：
`await import_cookies(path, port=8765)`、`await export_cookies(path, port=8765)`、`await clear_cookies(port=8765)`。
browser 负责本地 JSON 读写和 WS 调用；插件负责浏览器 Cookie 的校验与清除/写入，保证校验在清除之前完成。
`tests/extension/test_*_cookies.py` 只调用对应业务函数并断言结果，原命令保持可用。
导出函数返回 count 和 output（绝对 Path）；导入/清除返回插件的操作统计，不自动刷新页面或重试。
默认 browser 命令仍只启动 Chrome。

清除测试会逐条显示删除成功及失败的 Cookie 域名、名称、路径，并汇总清除数、失败数、剩余数；不显示 value。更新后需重新加载扩展。删除失败或仍有残留时测试失败，明细仍会打印。

## 商家后台 local_storage 单点测试

重新加载 0.6.0 版插件并重启 extension_bridge。以下操作由 browser 业务模块读取文件、准备页面，再通过 WS 调用插件：

```powershell
uv run python tests/extension/test_import_local_storage.py --input "一往无前宠物用品店cookie.json"
uv run python tests/extension/test_export_local_storage.py --output "data/export/local_storage.json"
uv run python tests/extension/test_clear_local_storage.py
```

导入读取 JSON 的 `url` 与 `local_storage`，忽略 cookies；不会导入或清除 Cookie。
仅允许 `https://mms.pinduoduo.com`，先校验文件，再复用唯一同源标签页；没有则打开 `/home`（导入使用文件 url），操作后保留页面。
三个入口支持 `--port 8765`、`--tab-id 数字`；多个商家后台标签页时必须指定 tab-id，不自动选择。
local_storage 必须为字符串键值对象；允许空对象（导入后为空）。导入先清空再写入，不自动刷新，不自动重试。
导出文件包含 `url` 和 `local_storage`，可以直接再次导入。清除只打印删除的 key、数量，不输出 value。
页面若跳转到其他来源会拒绝操作。页面脚本可能在操作后再次写入存储；写入失败（如配额不足）可能已部分完成，可导出检查。
browser 业务函数位于 `automation.pdd.storage.local_storage`，分别为 import_local_storage、export_local_storage、clear_local_storage。

Cookie 清除测试在显示删除明细后，会通过 browser 重新读取当前拼多多 Cookie，并断言列表为空；复查不会写文件，也不输出 Cookie 值。

## 统一 cookies 与 local_storage 测试（0.7.0）

重新加载 0.7.0 扩展并重启 WS 服务后，三个 Cookie 测试入口会同时处理两类存储：

```powershell
uv run python tests/extension/test_import_cookies.py --cookies "一往无前宠物用品店cookie.json"
uv run python tests/extension/test_export_cookies.py --output "data/export/cookies.json"
uv run python tests/extension/test_clear_cookies.py
```

均支持 `--port 8765`、`--tab-id 数字`；多个商家后台页面时必须指定 tab-id。
业务位于 `automation.pdd.storage.state`，测试只调用 import_state/export_state/clear_state 并断言结果。
复用或打开商家后台页面后保留，不自动刷新。Cookie 范围为拼多多根域及子域；local_storage 仅限 https://mms.pinduoduo.com。
导入、导出统一使用 `{url, cookies, local_storage}` JSON 对象，允许两类数据为空；旧的纯 Cookie 数组请使用 browser.cookies 的单独接口。
导入在任何清除前校验两类数据，过期 Cookie 不拒绝、不跳过、不延长有效期，按原时间提交浏览器。
提交成功数与实际读取数单独显示；已过期 Cookie 可能被浏览器立即删除，不会因为读回数量减少而判定导入失败。
三个入口都展示处理前、处理后重新读取的数据：Cookie 只显示 domain/name/path，local_storage 只显示 key 和数量，不输出 value。
清除显示成功/失败明细后，再独立重新读取两类存储并断言均为空。网页脚本可能再次写入，复查非空会报失败。
导出不会改动存储，前后读取之间网页自身仍可能改变数据。Cookie 与 local_storage 的联合操作没有事务，失败后不自动重试，可导出检查。
原单独 local_storage 测试及 browser.cookies 单独函数仍可用。

## 网易易盾演示页滑块（插件 0.8.0）

参考文章：https://zhuanlan.zhihu.com/p/654584767 。采用灰度公式 0.2126R+0.7152G+0.0722B、阈值二值化、逐列亮到暗边界计数，以及 easeOutBounce 拖动曲线。
仅适配 `https://dun.163.com/trial/jigsaw` 主文档内的图片式拼图；iframe、Shadow DOM 及 canvas 拼图组件暂不支持。
先手动打开演示页并使拼图组件可见，在 chrome://extensions 重新加载 0.8.0 插件，重启 WS 服务。

```powershell
# 只检测和保存背景图，不识别或拖动
uv run python tests/pdd/captcha/test_yidun_detect.py
# 只识别缺口，保存二值图与标注，不拖动
uv run python tests/pdd/captcha/test_yidun_recognize.py --threshold 100
# 重新检测和识别，拖动一次，再检查结果
uv run python tests/pdd/captcha/test_yidun_drag.py --threshold 100 --timeout 5
```

均支持 `--port 8765`、`--tab-id 数字`、`--output-dir 路径`；只选择易盾演示页，不会因同时打开拼多多而产生歧义。多个演示页时必须指定 tab-id。
默认选择器：`.yidun_bg-img`、`.yidun_jigsaw`、`.yidun_slider`、`.yidun_control`。
可用 `--background-selector`、`--piece-selector`、`--button-selector`、`--track-selector` 覆盖，要求每种元素唯一且可见。
图像优先使用页面 Canvas 读取；图片跨域不可读时，扩展通过 debugger 截取背景区域，不更改浏览器安全设置。
识别排除起始拼图区域，按显示缩放、透明留白、拼图和按钮的轨道比例换算 CSS 距离；透明轮廓不可读时回退元素边界并记录 alpha_bounds_available=false。
边界过弱、候选接近、超出轨道范围时不拖动。缺口识别是启发式方法，文章的掘金经验不能保证易盾通过率。
按下鼠标前核对图片及几何指纹；变化则停止。拖动不自动重试，异常也会尝试松开鼠标并解除 debugger。
只在组件明确显示验证成功时返回 passed；失败返回 failed，超时返回 unconfirmed，不以组件消失判定成功。
结果位于 `data/extension-tests/yidun-{detect|recognize|drag}/时间戳/`：background.png、binary.png、gap.png、result.json（检测只生成背景与结果）。
browser 业务在 `browser/yidun.py`，图像分析在 `captcha/yidun.py`，插件页面读取和拖动在 `extension/yidun.mjs`，三个测试入口只调用对应业务。

```powershell
# 本地合成图像与浏览器验证，不访问真实验证码服务
uv run python -m unittest tests.pdd.captcha.test_yidun_analysis -v
node --test tests/extension/test_yidun.mjs
uv run python tests/pdd/captcha/test_yidun_integration.py
```

旧的 tests/pdd/captcha/test_drag.py 仍是 CDP 诊断，不是这次的 WS 易盾实现。

## 每日逐店自动化

先停止单独运行的 CS、WS，执行 `uv sync --locked`。先执行 `uv run python -m automation.pdd.screenshot --init`，再填写业务工作目录 `.env` 中的飞书及自动化配置。店铺名选择器固定在 `automation/runner.py`：`#mms-header-next .user-info-top .user-name-name > span`，无需环境变量配置；其唯一可见元素的文本必须与飞书店铺名完全一致。

```powershell
# 默认保持运行，等待北京时间次日自动开始
uv run python -m automation.pdd.screenshot
# 只处理一轮，结束后关闭任务标签页和本流程启动的 CS、WS
uv run python -m automation.pdd.screenshot
# 等价模块入口
uv run python -m automation.pdd.screenshot
```

自动化启动前会关闭所有现有 Chrome 窗口和后台进程，等待退出后重新打开；如果未正常退出，会结束残留进程，无法关闭时停止流程。不会关闭 Edge 等其他浏览器。启动仍不传 `--user-data-dir`，复用默认用户目录中的已安装插件；旧的 `AUTOMATION_PROFILE` 配置不再读取。请在 `chrome://extensions` 重新加载项目 `extension/` 插件（0.11.1），插件地址与 `AUTOMATION_WS_PORT` 对应。程序默认等待插件连接 120 秒，可配置 `AUTOMATION_READY_TIMEOUT`。Chrome 启动命令将请求交给已有实例后退出是正常行为，后续通过插件连接状态判断浏览器是否可用。

Chrome 重新打开后，插件只关闭自己登记的任务标签页；如果发现非任务的拼多多主域或子域标签页，会在清理前停止并提示，不关闭用户页面、不清除其状态。Cookie 和 localStorage 仍是当前浏览器配置共享的，运行期间不要手动打开其他拼多多页面。流程退出保留 Chrome，仅关闭任务页和本流程启动的 CS、WS。登记保存在插件的 session 存储中，可跨后台 worker 重启使用；如果整个浏览器重启或插件重新加载，无法确认归属的残留拼多多页面需要手动关闭。


启动顺序：CS 就绪 → WS 就绪 → 关闭现有 Chrome → 准备启动提示设置 → 重新打开一个 Chrome 实例 → 插件握手成功 → 读取飞书店铺 → 串行处理。采用 `subprocess + psutil` 管理本流程拥有的进程树。已占用的服务端口会报错，不接管或终止其他进程。进程退出、WS/插件失联、CS 不可用会停止当前任务并重启服务组，从 SQLite 恢复；默认每次主程序运行最多恢复 3 次，可用 `AUTOMATION_RESTART_LIMIT` 修改。

每店先从 CS 下载 `{店铺名}cookie.json`。无 Cookie、需要验证码或账密错误记录为 `login_error`，当天不再处理。Cookie 导入跳转后，最多等待 10 秒，出现“账号登录”按钮、店铺名或后台“待收货”数据区域即继续，超时按业务失败重试。若落到登录页，使用飞书账号密码尝试一次登录，不重复提交结果未知的登录命令。

店铺名或唯一可见的“待收货”数据区域出现即判定后台已就绪，随后独立核对店铺身份，最多等待 10 秒（配置的页面超时更短时取更短值）。店铺名一直未出现时记录 `shop_identity_unconfirmed` 并按业务失败重试，店铺名不一致时记录登录异常；两种情况均不采集截图或金额。日志会输出登录状态变化、当前等待元素及超时原因。身份核对通过后最大化窗口，从“待收货”元素相对定位数据条框，截图覆盖整个可视区宽度、高度从可视区顶部到条框底部。保存 JPG 后，从同一“待收货”区域相对定位金额；已显示金额时直接读取，否则点击一次“查看金额”并等待显示。截图路径、待到账金额逐店保存到 SQLite，金额未获取时保留空值，不填为零。

店铺目录使用飞书原始行号（至少三位，不足补零）加店铺名，如第 2 行为 `002某某店`，第 10 行为 `010某某店`。中间空行过滤后仍保留原始行号，跨读取批次不重新编号。下载的 `cookies.json`、导出的 `new.json` 和 `数据截图.jpg` 统一保存到 `data/{日期}/{行号加店铺名}/`。Cookie 源文件查找、后台店铺身份核对和 Excel 中的店铺名仍使用原店铺名；Excel 按飞书行顺序导出，重试或重启不改变排序。当天旧成功记录的截图路径不符合新目录时，会重新处理该店铺；历史文件保留。

当天一轮完成、Ctrl+C、跨日或异常结束当前轮次时，导出当天已保存的数据到 `data/{日期}/店铺应收截图.xlsx`，包含店铺名、待到账金额（数值，保留两位小数）、本地图片完整路径。已有同名文件时依次使用 `.1.xlsx`、`.2.xlsx`。当天重启导出会包含 SQLite 中当天已获取的数据。强制终止进程不能执行退出导出，已提交的数据留在 SQLite 中，下次运行可继续导出。

```text
data/
├─ sqlite.db                                  每日状态与步骤事件
├─ automation.lock                            防止同时启动两轮
├─ {北京时间日期}/
│  ├─ 店铺应收截图.xlsx                        每轮结束导出，重名加编号
│  └─ 002某某店/
│     ├─ cookies.json                         CS 原始下载文件
│     ├─ new.json                             本次导出的 Cookie，不上传 CS
│     └─ 数据截图.jpg                         可视区顶部到数据条框底部
└─ cookies/Cookie/                            现有配置下的 CS 源文件目录，保留
```

`daily_shops` 以 `(day, shop)` 为主键，字段为 day、shop、row_number、manager、status、reason、attempts、cleanup_ok、amount、image_path、updated_at；步骤及清理和进程恢复事件写日志。数据库不保存账号密码或 Cookie 值。飞书重复店铺名、不能直接作为跨平台目录名的店铺名会拒绝运行。

- `success`、`login_error` 算当天完成；其他状态在当天重启后继续处理。
- 业务失败额外重试 5 次（最多 6 次完整尝试），每次均从清理、下载与登录开始；耗尽后下一店。
- 每次尝试前后均清理。先检查其他拼多多页面，再关闭任务页面，再用 Chrome `browsingData.removeLocalStorage` 清除工作台存储，逐条清 Cookie 并重新读取确认无剩余。清理失败额外重试 3 次（最多 4 次），仍失败立即结束整轮，不再重启绕过。
- 北京时间午夜取消旧日期任务，等待清理结束，重新读取飞书并从新日第一家开始。已完成当前一轮时保持等待，不在当天无限循环失败店铺。`PDD_SCREENSHOT_ONCE=true`（在 `.env` 中配置） 可在一轮结束后退出。
- Ctrl+C 会清理当前店铺并关闭本流程启动的服务。硬退出后任务保留 `running`，下次恢复为 `interrupted`，先清理再重做。

### SSH 远程 CS（可选）

远端 Windows/Linux 需要安装本项目、Python 3.14 和依赖，并可使用 SSH 公钥或账号密码认证。先通过系统 SSH 验证主机指纹，使本地 `known_hosts` 已记录目标主机。保留 AsyncSSH 主机验证，不会自动信任未知主机。

本地设置 `CS_SSH_HOST`、`CS_SSH_USER` 和 `CS_SSH_COMMAND`；远程命令必须启动本项目的租约入口，不要直接启动普通 CS。例如：

```dotenv
# Linux 远端，使用绝对路径，不依赖交互式 shell 的激活环境
CS_SSH_HOST=server.example.com
CS_SSH_USER=operator
CS_SSH_PORT=22
# 使用密码认证时填写；也可留空使用私钥 / SSH agent
CS_SSH_PASSWORD="你的密码"
CS_SSH_COMMAND=/srv/pdd/.venv/bin/python -u -m automation.pdd.screenshot.remote --root /srv/pdd --port 8787
CS_SSH_REMOTE_PORT=8787

# Windows 远端示例，按 SSH 默认 shell 的规则引用有空格的路径
# CS_SSH_COMMAND=D:\repo\pdd-seller-desk\.venv\Scripts\python.exe -u -m automation.pdd.screenshot.remote --root D:\repo\pdd-seller-desk --port 8787
```

远端 `.env` 中 `COOKIES_DIR` 指向服务器 Cookie 文件目录；本地 `data/{日期}/{行号加店铺名}/` 是下载和结果目录。WS 和浏览器始终在本地运行。

AsyncSSH 将远端 CS 的日志实时转发到当前终端，并建立随机本地端口到远端 `127.0.0.1:8787` 的转发；HTTP 下载走该隧道。控制消息通过 SSH stdin 传输，每行 JSON：`{"type":"ping"}`（每 5 秒）、`{"type":"stop"}`。远端默认 30 秒未收到心跳或 stdin EOF 就终止自己的 CS 进程树；正常退出先发送 stop 并等待清理。断网时停止有心跳超时延迟。远程租约用 `--lease` 调整，必须大于 5 秒。

远端配置目录、解释器路径、SSH 用户和端口需按实际机器填写。远程模式下不启动本地 CS，也不上传 new.json。

### 新增的自动化插件动作

WS 请求/响应仍使用既有 `type/id/action/params`、`type/id/ok/result|error` 格式，新增以下动作：

| action | params | 结果 |
|---|---|---|
| `tab_create` | `{}` | 新建空白标签页并登记归属，返回 `tab_id` |
| `tab_navigate` | `tab_id`, `url` | 仅导航到商家后台 HTTPS 来源，等待加载完成 |
| `tab_close` | `tab_id` | 仅关闭已登记的任务标签页 |
| `shop_identity` | `tab_id`, `selector` | 唯一可见匹配元素的文本 `shop_name` |
| `automation_state_clear` | `{}` | 存在其他拼多多页面时拒绝；否则关闭登记的任务页并清理状态 |
| `automation_tabs_close` | `{}` | 只关闭登记的任务页，不清理 Cookie 或存储 |

普通 `cookies_clear`、`local_storage_clear` 的原有行为保持不变；统一流程使用 `automation_state_clear` 防止活跃页面在清理过程中重新写入存储。


### 终端日志与文件分块

同一个终端实时显示所有运行来源，统一使用北京时间和来源前缀：

```text
16:53:16 [管理] CS 已启动，PID=1234
16:53:16 [CS] * Running on http://127.0.0.1:8787
16:53:17 [WS] Chrome 插件已连接。
16:53:18 [业务] 店铺名：download_cookies
16:53:18 [WS] 任务开始：cookies_import
```

CS、WS 的 stdout/stderr 都实时汇入当前终端，同时按日期分别写入：

```text
logs/2026-10-29/
├─ cs.log
├─ ws.log
├─ business.log
├─ browser.log
└─ manager.log
```

每个来源文件按 **20 MiB（20 × 1024 × 1024 字节）** 分块。当前输出继续写 `ws.log`，已满分块依次归档为 `ws.1.log`、`ws.2.log`；其他来源同理。跨北京时间午夜自动进入新日期目录，不删除旧分块，重启后也不覆盖归档。文件保持 UTF-8。日志不打印 Cookie 值或登录参数。

浏览器日志记录启动、插件就绪与任务页关闭结果；页面动作日志由 WS 记录。默认 Chrome 的底层渲染器诊断日志和网页控制台日志不在此汇总范围内。

Ctrl+C 第一次请求停止并等待清理，期间重复 Ctrl+C 不打断清理。CS、WS 使用独立进程组，由管理器在清理完成后停止。同步飞书/HTTP 调用使用可取消的 daemon 工作线程，避免退出时卡在默认线程池的 join；已在进行的网络调用可能在后台运行至超时，但不会在取消后继续写入流程结果。


### Chrome 启动提示

统一自动化入口启动时，先关闭现有 Chrome，再准备默认用户目录中上次使用的配置（不存在时为 Default）。启动参数加入 `--no-first-run`、`--no-default-browser-check`、`--hide-crash-restore-bubble`，同时设置该配置为正常退出、新标签页启动，并关闭保存密码提示。不会禁用安全浏览、证书校验或密码泄露检测；这些安全提示不自动确认。

原 Preferences 文件先备份到 `data/browser-preferences/{时间}/{配置名}/Preferences.json`，备份路径写入 browser.log；随后以原子替换方式更新必要字段，保留插件及其他配置。配置无法读取时停止，不覆盖损坏内容。启动页恢复和密码保存提示的偏好会持续影响该 Chrome 配置，需要恢复时可在 Chrome 完全关闭后使用备份。浏览器版本、企业策略或其他扩展仍可能产生额外提示。

只运行 `automation.com.browser` 时仍保持原来的简单启动行为；自动关闭现有 Chrome 和准备配置仅用于统一自动化入口。开发测试使用模拟进程与临时配置，不会关闭真实 Chrome。

SSH 用户名、密码、端口分别通过 `CS_SSH_USER`、`CS_SSH_PASSWORD`、`CS_SSH_PORT` 配置。密码按原字符串传给 AsyncSSH，不去除首尾空格，不拼入远程命令，不写入日志，也不包含在 Config 的 repr 中。主机验证仍使用 known_hosts。只有 `CS_SSH_HOST` 非空才启用远程 CS；本地 Cookie 目录配置不能代替远端 `.env` 的 `COOKIES_DIR`。

业务步骤之间默认等待 0.5 秒，Cookie 导入后到打开工作台前等待 1 秒。自动化导航不等待整页加载完成，跳转后最多等待 10 秒，出现账号登录按钮、店铺名或后台待收货数据区域即继续；超时重试当前店铺。提交登录后等待 0.5 秒再轮询结果，使用统一截止时间，默认最多 30 秒。导航动作异常记为可重试的 business_failed，不直接计为当天登录完成。新版等待逻辑需要重新加载 0.11.1 插件并重启 automation。

截图业务专用 Cookie 服务入口 `automation.pdd.screenshot.cookies`从代码项目目录运行，读取 `../automation.pdd.screenshot/.env`，Cookie 相对路径以业务目录为基准。普通启动独立运行；加 `--remote` 接收 SSH stdin 心跳与 stop，在断线或心跳超时后清理子进程。旧 `automation.pdd.screenshot.remote --root ...` 仍兼容。远程模式端口取自服务器业务 `.env` 的 `COOKIES_SERVER_PORT`，需与本机 `CS_SSH_REMOTE_PORT` 一致。

Windows CMD 的远程启动示例（服务器代码需同步更新）：

```dotenv
CS_SSH_COMMAND='cd /d C:\Users\administrator\Desktop\RPA\automation && uv run python -u -m automation.pdd.screenshot.cookies --remote'
```

若服务器 SSH 默认 Shell 是 Windows PowerShell，则使用 `Set-Location`，并检查目录切换成功：

```dotenv
CS_SSH_COMMAND='Set-Location -LiteralPath "C:\Users\administrator\Desktop\RPA\automation" -ErrorAction Stop; uv run python -u -m automation.pdd.screenshot.cookies --remote'
```
