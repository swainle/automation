# 拼多多榜单与商品采集

在 automation 项目根目录运行：

```powershell
uv sync
uv run python -m automation.pdd.spider --check
uv run python -m automation.pdd.spider
```

当前统一入口使用 PostgreSQL 作为唯一数据源：数据库 `pdd`、命名空间 `spider`，所有表使用 `t_pdd_` 前缀。连接只配置在业务目录 `.env` 的 `SPIDER_POSTGRES_URL`，不在源码中保存凭据。SQLite 和原 PostgreSQL 库仅保留迁移备份，采集不会回退到 SQLite。旧独立 `ranking`/`product` 流程与当前每周结构不兼容，使用统一入口及 ID 筛选。

操作日志在执行前立即输出动作，完成后在同一行追加结果与一位小数的耗时，再换行，例如：

```text
[商品] 采集主图... 主图 5 张 [3.0s]
[ios] 点击 分享... 失败：连接超时 [10.0s]
```

嵌套操作或后台日志插入时先结束当前显示行，外层操作完成后另起一行标明动作与总耗时，避免输出混在一起。文件日志分别记录操作开始与结束，程序卡住时也能看到最后开始的动作。

## 业务目录

默认使用项目相邻的 `automation.pdd.spider`，也可在启动前设置 `PDD_SPIDER_ROOT` 为绝对目录。

```text
automation.pdd.spider/
├── .env
├── data/
│   ├── backups/
│   └── grab/pdd/
│       ├── 拼多多榜单.xlsx
│       └── 主榜/子榜/日期/
│           ├── 榜单数据.xlsx
│           ├── 商品数据.xlsx
│           └── 榜01/
│               ├── 排行截图.jpg
│               ├── 产品截图.jpg
│               ├── 分享链接.json
│               ├── 产品页截图.jpg
│               ├── 产品信息.json
│               ├── SKU.csv
│               ├── 评论.csv
│               ├── 差评.csv
│               └── 主图/、详情图片/
├── logs/
├── state/
└── tools/
    ├── vendor/go-ios/
    ├── vendor/zsign/
    ├── ios/
    ├── releases/wda/
    ├── devimages/
    └── selfIdentity.plist
```

来源表按 A 种类、B `source_name`、C `main_name`、D `tab_name`、E 子榜链接读取。每行仅采集 D 列指定的子榜，保留链接的 `activeTab`，同一 B 名称可对应不同子榜。目录统一为 `C列主榜/D列子榜/日期`，Windows 非法目录字符替换为下划线。D 列为空时创建 `主榜/日期` 空目录，并将对应记录设为 `failed`、原因“链接失效”，不打开页面；本轮返回失败状态，其余有效行继续采集。`降价榜` 为标准名称，兼容将旧写法“降价版”归一为“降价榜”。同一来源/子榜或主榜/子榜重复、E 列无效、链接 `activeTab` 与 D 列不对应时在启动采集前报错。

续采按 `capture_date + source_name + tab_name` 复用原记录，已完成的子榜不再打开页面，不会因另一个子榜未完成而重新采集整组。页面识别的主榜和子榜必须对应 C/D 列，避免 App 自动跳转后将商品写入错误榜单。已有主榜名称、目录或子榜链接与表格冲突时拒绝覆盖，须先迁移已有数据。首次运行生成默认 `.env`，已有配置不会覆盖。相对路径以业务目录为基准，源码不依赖原来的 `D:\repo\spider`。

未完成子榜续采时，先从当前页面完整快照重新解析排行、标题和指标，不用数据库里的旧标题直接定位。相同排行及榜单标题保留已验证链接和详情；标题、明确的商品 ID 或排名变化时，重置该位置的旧商品进度，再按当前页面采集，避免榜单更新后反复出现 `element not found`。

商品标题不可见时，用与长图采集相同的短距离手势逐步向下浏览，每次重新核对原榜单并查找同一标题及出现次数，最多滑动 12 次；不调用 WDA 的元素自动滚动接口，避免其阻塞后续命令。WDA 通信异常会停止本轮，保留未处理榜单的状态，不将剩余链接批量记为失败。恢复连接后重跑原命令续采。

可在业务 `.env` 调整 `SPIDER_SOGOU_EXECUTABLE`、`SPIDER_SOGOU_USER_DATA_DIR`、`SPIDER_SOURCE_XLSX`、`SPIDER_OUTPUT_DIR`、`SPIDER_TOOLS_DIR`、`SPIDER_RETRY_ATTEMPTS`、`SPIDER_RETRY_DELAY` 和 `SPIDER_PRODUCT_RETRY_DELAY`。WDA Bundle ID 自动检测，也可设置 `WDA_BUNDLE_ID`。

## 先准备并保留搜狗

```powershell
uv run python -m automation.pdd.spider.sogou
```

`sogou` 初始化命令先关闭所有搜狗窗口及其后台子进程，确认退出后重新打开；关闭失败时停止初始化。公共 WS 服务优先复用。窗口大小、页面缩放和视口由用户手动设置，插件不再修改这些设置。命令成功后退出，但保留浏览器、已配置的工作标签页和后台插件通信服务；后台服务日志为业务目录 `logs/sogou-bridge.log`（由搜狗启动时）。

后续 `ranking` 和 `product` 优先使用这个实例。已准备的工作页在采集结束后保留；商品使用同一浏览器中的独立临时标签页，逐商品结束时关闭，避免混淆请求响应。没有先运行 `sogou` 也可以直接采集，此时采集命令自行启动所需服务，退出后保留公共通信服务。

## 榜单流程

### 搜狗浏览器

```powershell
uv run python -m automation.pdd.spider.ranking
```

在搜狗的扩展管理中加载本仓库 `extension` 目录，已有插件需要重新加载至 0.13.0。在搜狗插件配置页把连接地址设为 `ws://127.0.0.1:8765/`，与商家截图共用公共 WS 服务。服务按浏览器分发，搜狗与 Chrome 可同时连接。

命令优先复用本机插件通信服务和已准备的搜狗，未就绪时才启动浏览器，保留其用户数据。浏览器不带 `--remote-debugging-port`，不使用 Playwright/CDP 连接。程序检查插件 WebSocket 连接对应的进程属于配置的搜狗可执行文件，避免使用其他浏览器中连接的插件。

浏览器窗口、页面缩放、视口均由用户手动设置。插件仅附加 debugger 以监听请求、发送主图拖拽输入，不再设置窗口尺寸、标签页缩放、设备模拟尺寸或触摸模拟开关。

公共服务端口在 automation 项目根目录 `.env` 设置 `AUTOMATION_WS_PORT=8765`，环境变量优先。旧的 `SPIDER_RANKING_WS_PORT` 不再使用。已有 WS 服务会优先复用；未启动时自动创建后台服务，采集命令退出后保留。旧版服务需先停止并重启。

以下业务参数在业务目录 `.env` 设置：

```dotenv
SPIDER_RANKING_READY_TIMEOUT=120
```

旧的 SPIDER_RANKING_WIDTH、SPIDER_RANKING_HEIGHT、SPIDER_RANKING_SCALE 不再用于设置浏览器。

### iOS 手机

首次准备或单独运行 iOS 服务：

```powershell
uv run python -m automation.pdd.spider.ios
# 多设备时指定 UDID
uv run python -m automation.pdd.spider.ios --udid 设备UDID
# 需要重新签名安装时
uv run python -m automation.pdd.spider.ios --reinstall
```

命令检测 USB 设备，复用已安装的 WDA；缺少 WDA 或指定 `--reinstall` 时打包、签名并安装，再准备隧道、开发者镜像、WDA 驱动和 8100 端口转发。保持该终端运行，其他终端的 `ranking --ios` 可复用服务；Ctrl+C 只停止本命令启动的辅助进程。准备期间持有业务锁，就绪后释放锁供采集使用。手机需已安装 Apple USB 驱动、解锁并信任电脑，开启开发者模式并完成设备端证书信任。

设备证书 ZIP 放在业务目录 `tools/ios/signing/`（默认 `D:\repo\automation.pdd.spider\tools\ios\signing\`）：

```text
tools/ios/signing/certificate_设备UDID.zip
  ├── certificate.p12
  └── profile.mobileprovision
```

ZIP 文件名必须包含设备 UDID，每台设备只能匹配一个 ZIP；内部可有子目录，但必须恰有一个 `.p12` 和一个 `.mobileprovision`。程序检查描述文件有效期、设备授权和明确的 Bundle ID。P12 密码运行时隐藏输入，也可通过环境变量 `SPIDER_P12_PASSWORD` 提供；临时解压的证书在签名后清理。证书放业务目录，不放源码仓库。

Windows 使用预编译 WDA 进行打包签名，并非从源码编译。默认源目录为 `tools/ios/archive/WebDriverAgentRunner-Runner.app.v16.15.3`，签名工具为 `tools/vendor/zsign/zsign.exe`，go-ios 为 `tools/vendor/go-ios/ios.exe`。可通过 `--wda-version 7.0.0` 或业务 `.env` 的 `SPIDER_WDA_VERSION` 选择兼容设备 iOS 版本的预编译包。源码编译需在 macOS/Xcode 完成，再提供 `.app`。签名产物保存在 `tools/releases/wda/`。

业务 `.env` 可选设置 `SPIDER_IOS_UDID`（多设备时固定目标），`WDA_BUNDLE_ID`（设备上安装多个 WDA 时指定）。已有设备只需补齐缺失配置项，不必覆盖原 `.env`。

```powershell
uv run python -m automation.pdd.spider.ranking --ios
```

只准备 iOS 隧道、镜像、WDA 驱动与转发，通过 Safari 来源链接打开 App 并核对榜单，不启动搜狗或插件服务。旧版本 iOS 跳过不适用的隧道，已有服务复用。

按 Excel 每行采集指定子榜：

```powershell
uv run python -m automation.pdd.spider.ranking --ios
# 只采手机当前打开的榜单
uv run python -m automation.pdd.spider.ranking --ios --current
```

`ranking --ios` 按 `SPIDER_SOURCE_XLSX`（默认 `data/grab/pdd/拼多多榜单.xlsx`）顺序循环一轮。每条链接先结束拼多多应用，确认进程已停止后在 Safari 实际打开网址。自动处理“是否打开拼多多”确认框；没有弹窗时点击网页“打开拼多多APP”或右上角“在App打开”，再确认打开。核对 C 列目标主榜后，仅采集 D 列指定子榜的数据、长图、裁剪、商品截图及分享链接，再切换下一行。链接或榜单访问失败记入数据库并继续下一条；严重流程错误和 Ctrl+C 停止。`--refresh` 重新采集全部榜单数据、长图、裁剪、商品截图和分享链接，`--source-name` 仅用于 `--current` 模式。

### 数据与续采

两种模式共用 SQLite。按来源表、子榜、商品顺序执行采集、截图、裁剪和链接获取，原 Excel 的所有列和行先入库，再导出原格式 Excel。

当前设备访问/读取失败的任务记录原因后跳过，不自动使用另一设备；可稍后执行另一个命令补采。已完成子榜不会重复采集；切换设备采集尚未完成的子榜时重新读取该子榜，避免混用两种设备的截图和元素快照。截图、裁剪、链接或导出失败留在所选设备重试，直到完成或 Ctrl+C。

同日重启按数据库续采，次日运行建立新任务，不自动等待第二天。商品命令独立处理已有有效链接的记录，ID 缺失时在采集中补全，不受整轮榜单完成状态限制。两种模式只关闭插件登记的榜单任务页或自己启动的手机辅助进程，复用的浏览器与服务保留。

移动模拟与设备预览缩放的底层能力参见 [Chrome 扩展 debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger) 和 [Emulation.setDeviceMetricsOverride](https://chromedevtools.github.io/devtools-protocol/tot/Emulation/#method-setDeviceMetricsOverride)。

## 商品流程

### 安卓拼多多原生 App（USB 真机）

准备工具：

1. 安装 [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)，将 adb.exe 及配套 DLL 放入业务目录 `tools/vendor/android/`，或设置 `SPIDER_ADB_EXE`。已有 ADB 可直接复用；Windows 识别异常时安装厂商 USB 驱动。
2. 手机开启开发者选项、USB 调试，连接数据线，解锁并允许此电脑调试；安装并登录拼多多 App。
3. 执行 `uv sync` 安装项目依赖，包含 `uiautomator2`。首次连接自动部署需要的设备端组件，之后复用健康服务。无需 Appium、Root、Chrome、搜狗插件、WS 或代理。

```powershell
uv sync
# 一键准备原生连接并打开/复用拼多多；命令退出后连接保留
uv run python -m automation.pdd.spider.android
# 采集手机当前已打开商品，默认最多 100 条普通评论
uv run python -m automation.pdd.spider.product --android --current
# 三项可以独立运行，也可以组合
uv run python -m automation.pdd.spider.product --android --image-sku --current
uv run python -m automation.pdd.spider.product --android --image-main --current
uv run python -m automation.pdd.spider.product --android --image-detail --current
uv run python -m automation.pdd.spider.product --android --comment --current
# 自定义当前商品评论数量
uv run python -m automation.pdd.spider.product --android --current --comment-limit 30
# 按当天榜单商品链接逐条打开 App 采集
uv run python -m automation.pdd.spider.product --android
```

`--current` 不读取榜单队列、不打开其他商品，不新建虚构榜单记录。产物保存到 `data/grab/pdd/当前商品/日期/商品名称-摘要/`；包含 `产品信息.json`、商品 XML/截图、`主图/`、`SKU/`、`SKU.csv`、`评论.csv`、`评论原始.json`、评价标签及 `status.json`。SKU 和评论以原生控件可见信息为准，未知商品 ID、评论 ID、评论日期等字段不会伪造。普通评论去重来自 UI 内容摘要，达到数量上限或明确结束标记后完成；连续翻页无新增但无法确认结束时保留失败及已采进度。

主图和 SKU 图按“点击放大 → 截图 → 裁掉黑边和工具栏”保存；`图片清单.json` 明确标记 `android_enlarged_screenshot_crop`，没有伪造原图下载 URL。滑动起点、持续时间与等待间隔随机，主图逐张核对页码，不保存未推进的重复主图；SKU 选择后确认选中及已选规格摘要，再读取对应价格，始终不点击确认购买。重试后仍不可选或 App 自动改选的组合写入 `SKU不可选组合.json`，不为其捏造价格和图片；`SKU采集摘要.json` 记录候选数、成功数及不可选数。重复运行会复用通过图片校验、规格和价格对应的已保存记录。当前版本不将截图称为原始图片，不提供接口 JSON；详情图也通过大图截图裁剪获取，并按顺序合成长图。差评优先采用 App 明确的差评筛选入口，否则按灰色差评标签采集，不根据普通评论内容自行猜测情感。

`product --android` 只处理北京时间当天有有效链接的榜单商品，每件商品先使用安卓 Chrome 打开链接，经确认弹窗、网页“打开拼多多APP”或“在App打开”跳转拼多多，等待商品页加载并核对目标标题后再采集；完成后沿用 SQLite、图片清单和 CSV 导出。Chrome 跳转记录保存在商品目录的 `网页跳转/`；采集跳转完成后关闭本次新建的 Chrome 标签页，保留其他标签页和 App。链接无法跳转或页面结构不符时记录失败并重试。原生商品信息标记 `source=android_native_ui`。

已有业务 `.env` 不覆盖，需要时追加：

```dotenv
SPIDER_ADB_EXE=D:/repo/automation.pdd.spider/tools/vendor/android/adb.exe
SPIDER_ANDROID_SERIAL=
SPIDER_ANDROID_PORT=7912
```

一键启动优先复用 ADB 和手机 UiAutomator；服务缺失时启动隐藏的后台连接进程持有设备服务，日志在 `logs/android-service.log`，设备断开持续约 15 秒后后台退出；为设备端 TCP 9008 建立默认本机 TCP 7912 转发，占用其他设备或程序的端口不会覆盖。不对局域网开放服务。退出不会停止手机 App、ADB、UiAutomator 或已建立转发。同一台手机的多个脚本可复用连接，应依次操作页面，以免互相切换页面；正式采集按设备互斥，不同设备可并行。

其他本机脚本可以：

```python
import os
os.environ['ADBUTILS_ADB_PATH'] = r'D:\repo\automation.pdd.spider\tools\vendor\android\adb.exe'
import uiautomator2 as u2
device = u2.connect('adb devices 中的序列号', port=9008)
print(device.app_current())
```

原生控制使用 [uiautomator2 官方接口](https://github.com/openatx/uiautomator2)；需要时可以使用准备命令打印的本机 JSON-RPC 地址。`android --serial 序列号` 用于多设备准备，采集时需在环境变量或业务 `.env` 固定同一 `SPIDER_ANDROID_SERIAL`。

### 搜狗插件（默认）

读取数据库中已有有效 HTTP/HTTPS 产品链接的记录，无需预先具备商品 ID，仅处理启动时北京时间当天（Asia/Shanghai）的榜单商品，按来源、子榜、排行顺序循环处理，不再处理历史未完成商品。榜单采集运行期间每 5 秒重新查询队列，自动接收新增链接；正在采集的子榜暂时跳过，避免榜单刷新或共享产物被同时覆盖。榜单进程退出且当天商品队列清空后，商品进程退出；没有运行中的榜单且没有待采商品时直接退出。榜单整体失败、某个子榜未完成或最新一轮尚未结束，都不阻挡已有链接的商品。产品详情采集状态为 completed 或 skipped 时跳过；榜单商品 state=completed 仅表示榜单截图和链接完成，不代表产品详情完成。

插件在商品页面导航前启用 Network 监听，保存属于当前商品的产品信息；SKU、主图、详情图、截图及日期规则沿用原有逻辑，元素通过插件读取和操作。评论只接收本轮点击后发出的请求，仅采集差评，多个灰色标签合并去重后最多 100 条。原有图片、CSV、状态 JSON 继续保存；SQLite 额外保存产品信息、SKU/评论数据、产物路径和完成状态，导出每个子榜的 `商品数据.xlsx`。

缺失的商品 ID 依次从分享链接、跳转后的商品页 URL、当前商品信息接口的主体字段或请求参数补全，不遍历推荐商品列表猜测 ID。ID 冲突或无法确认唯一 ID 时保留重试；确认后立即写回数据库、行数据和 `分享链接.json`，后续图片或评论步骤失败也不丢失已补全 ID。

运行前手动打开公共 WS 和搜狗，确认搜狗插件已连接服务。`product` 仅复用现有连接，不启动或重启 WS、搜狗；服务未启动、插件未连接或连接来自其他浏览器时直接报错。全部完成或 Ctrl+C 后保留外部服务和浏览器，仅关闭采集时创建的商品标签页。没有待采商品时不连接浏览器。

未完成商品间隔重试，完成项跳过。售罄沿用原规则，超过 10 次检测后跳过。全部处理结束或 Ctrl+C 后退出，不轮询等待榜单新增数据。

## 状态和资源

- `rankings`：某天的榜单 Tab，区分来源分类名、页面主榜名和 Tab 名。
- `ranking_products`：上榜商品，排行、商品 ID/链接、标题和统一指标 `metrics_json`。
- `product_info`：每条上榜记录对应的商品详情、上架日期和图片文件信息。
- `product_skus`：SKU 规格、价格等。
- `product_comments`：普通评论和差评，按评论 ID 或内容指纹去重。
- `collection_tasks`：整轮/来源/步骤状态、重试次数和错误记录。

`metrics_json` 对两种设备使用同一组英文键，保存完整文案；未读到时为 JSON null。字段包括 `display_price`、`recent_sales`、`total_sales`、`hot_index`、`dominance_days`、`positive_rate`、`positive_count`、`coupon_price`、`deal_price_30d`、`price_drop`。没有独立的排行价格列。旧数据中仅剩数值的指标保留原数值的字符串，无法还原丢失的展示文案。原始数据另存 `raw_json`，避免迁移时丢失未知字段。日期按北京时间分批，数据库默认时间戳为 UTC。

旧数据库必须手动执行项目中的 `src/automation/pdd/spider/migrate/20261102115136_rankings_products.sql`，参见 `src/automation/pdd/spider/migrate/README.md`。程序不会自动转换旧数据库；空数据库自动创建六张新表。


榜单与商品使用独立流程锁、设备锁和子榜目录锁，允许不同设备在同一业务目录并行；同一设备仍串行。SQLite 使用 WAL 和 30 秒写锁等待，事务只覆盖数据库操作，Excel 保留文件级锁。`sogou` 显式准备的浏览器与插件服务长期保留。采集命令退出只清理自己临时启动的辅助进程，保留复用的浏览器、工作页和外部服务。工具、镜像、签名资料和抓取产物位于业务目录，不进入代码仓库。

旧工作区的历史采集数据保留原处，不会在首次运行时当作新数据库中的已完成任务。来源 Excel 已复制到新业务目录；运行前核对本机搜狗路径、设备连接、开发者模式以及 WDA 安装情况。

## 手动打开榜单后采集一轮

先在手机手动打开拼多多 App 的目标榜单，并确保 App 在前台，再运行：

```powershell
uv run python -m automation.pdd.spider.ranking --ios --current
```

命令不读来源 Excel、不打开 Safari、不跳转榜单来源链接。榜单文字解析阶段不拖动；后续长图和商品采集会按需回顶或滚动。识别当前页面主榜名称和 Tab，直接解析首次返回的完整元素树入库；初次识别的快照直接复用，不重复调用 source。当前已选中的 Tab 不重复切换，其他子榜切换后读取快照。推荐分界可以在屏幕之外，只要存在于元素树中即可。未包含榜单推荐分界时，无法确认全榜已加载，会记录失败而不保存部分商品；不会自动拖动补采。iOS 26.6.1 的实测首次快照包含 20 个榜单商品及分界，无需拖动即可解析。

所有子榜预先建任务；单个子榜失败会记录错误，继续其他子榜；本轮结束后退出，不无限重试。同一天重复运行会更新原榜单及排行记录，保留已有记录 ID，不新增重复数据；缺少的新排行才插入。排名和榜单名称相同时保留已有 ID、分享链接及详情；任一变化时清理该位置旧进度并重新采集。一天多轮历史快照尚未支持。

默认 `source_name` 使用当前页面主榜名，可通过 `--source-name 分类名` 自定义来源分类。本命令没有来源链接，`source_url` 为空。每条榜单唯一键为 `capture_date + source_name + tab_name`，每条上榜记录唯一键为 `ranking_id + rank`。

本命令默认完整采集当前打开的主榜及全部子榜：榜单指标、榜单长图、卡片裁剪、商品首屏、分享链接和 Excel 导出，一轮后退出。SKU、评论和详情图片仍由独立商品流程处理。运行前建议先在另一个终端准备 `automation.pdd.spider.ios`，保持手机停留在榜单页。

`recent_sales` 保存近期拼单文案，支持 24 小时、一周、30 天等周期，替代旧键 `sales_24h`。iOS 卡片分组优先遵循 XML 文档顺序，避免离屏元素坐标失真导致价格串位。

## 完整采集：长图、商品首屏与分享链接

已有六表数据库需要先手动执行 `src/automation/pdd/spider/migrate/20261102142611_capture_state.sql`。`rankings` 沿用已有 `state`，`ranking_products` 增加 `state`，值为 `pending`（待采集）、`running`（采集中）、`completed`（完整完成）、`failed`（失败待续采）。只读过文字的旧记录不会被当作整个流程完成。原五表数据库直接执行新版 `20261102115136_rankings_products.sql` 即可。

手动在拼多多 App 打开榜单，保持前台并运行：

```powershell
uv run python -m automation.pdd.spider.ranking --ios
```

每个子榜依次执行：

1. 复用完整元素树，将榜单卡片与指标更新到原数据库记录。
2. 确认榜单顶部，以排行徽标和推荐分界建立长图范围。
3. 滑动并截图；截图之间用重叠区域匹配位移，不逐屏请求 XML。保留一次头部，去掉重复区域与底部系统条，生成 `榜单长图.jpg`。
4. 按已验证的排行边界裁剪，每张 `产品截图.jpg` 包含榜单头部和对应商品卡片。无法确认几何关系或拼接位移时停止，不输出标记为完成的错位长图。
5. 逐商品通过 WDA 元素 ID `.tap()` 打开商品，校验对应商品页后保存首屏 `产品页截图.jpg`。
6. 清除并验证旧剪贴板，点击“分享”和“复制链接”，读取本次链接，写入数据库及 `分享链接.json`。只复制链接，不向联系人发送内容；`ps` 等不透明分享参数不当作真实商品 ID。
7. 返回并校验原榜单与 Tab，再处理下一个商品；当前子榜结束后切换下一个子榜。

产物示例：

```text
主榜/子榜/日期/
├── 榜单长图.jpg
├── 榜单长图.meta.json
├── 榜单元素.xml
├── 榜单分屏/0001.png、0002.png、...
├── 榜单数据.xlsx
├── 榜01/
│   ├── 产品截图.jpg
│   ├── 产品页截图.jpg
│   └── 分享链接.json
└── 榜02/...
```

当前 `ranking --ios` 每次都读取 APP 最新榜单，不因数据库显示子榜完成而跳过。标题、价格、指标和商品数量按当前完整快照更新；不再上榜的旧位置从当前榜单记录移除。

商品以同一天、同一子榜内的“排名＋榜单卡片名称”判断是否延续旧记录。两者均不变时保留旧链接、ID、详情和已完成步骤，并更新当前价格、销量等指标；无需为了确认 ID 再次打开商品页。指标变化时重新采集商品截图、更新榜单长图及裁剪，已有有效分享链接可继续使用。排名或名称变化时清理该位置的旧进度及详情关联，重新采集；新增商品加入，下榜位置移除。当前快照若明确提供了与旧记录冲突的商品 ID，同样重采。

榜单卡片名称保存在 `title` 和 `raw_json.榜单标题`，商品页名称单独保存在 `raw_json.商品页标题`，详情页“【送赠品】”等前缀不影响下次按榜单名称续采。旧版本已分开记录 `榜单标题` 的数据仍可正确识别。复用前验证截图文件和分享链接文件有效，即使链接仅有不透明分享参数、没有真实 ID，也可以按排名＋名称复用。

进入商品后关闭 SKU 面板并等待页面就绪；以返回、分享及购买/店铺控件确认商品页，不再要求标题与榜单完全一致。可识别的商品页标题（包括 Image/Other 节点上的“【送赠品】”等前缀）单独保存，不覆盖榜单名称。重新复制分享链接时，以本次链接更新真实 `goods_id`，不透明分享参数不推断 ID。无法取得链接的商品保留失败状态；校验失败时在业务目录 `data/diagnostics` 保存 XML、JSON 和诊断截图。

长图按当前排行、标题和指标指纹验证复用；价格或指标改变会使旧长图失效。点击前若目标商品不可见，先按元素滚动到可见位置，再重新查找同一标题及出现次数；仍不可见则不点击。商品页校验失败但原主榜和 Tab 的导航栏仍可见时，跳过返回按钮，将该商品记为失败并继续后续商品。已进入商品页时正常返回并校验原榜单；无法确认返回正确榜单或拼多多离开前台时仍停止后续操作。商品只有在截图、分享链接保存并确认返回原榜单后才写为 `completed`。本流程不包括 SKU、评论或详情图采集。

使用 `--refresh` 可强制刷新全部榜单数据、长图、裁剪图、商品页截图和分享链接：

```powershell
uv run python -m automation.pdd.spider.ranking --ios --refresh
```

`ranking --ios` 默认执行上述完整流程；`--refresh` 可直接使用，无需搭配 `--full`。


### 产品图片及评价采集

网页产品采集不再截图，既有手机 `产品页截图.jpg` 不被覆盖，也不影响网页产品信息完成判定。主图从 `//*[@id="main"]/div/div[1]/div[1]/div/div[1]` 所在轮播容器递归读取，支持嵌套 img、懒加载源和滑动后加载，不再遇到空图片就中止，也不再限制为 29 张；读取数量不足轮播声明总数时保留失败重试。

主图、SKU 图和详情图下载信息立即写入 `product_images`，同时保存 `图片清单.json` 供中断恢复。字段为类型、位置、源 URL、相对商品目录的本地路径、pending/completed/failed 状态及错误；图片文件验证通过后才记完成。SKU CSV 增加“图片路径”，对应数据库 `product_skus.image`；详情合成长图以 detail_merged 单独记录，源 URL 为空。旧图片表升级见 migrate/README.md。

普通评论最多前 100 条；每个灰色负面标签分别最多 100 条，不足 100 条需确认已到分页末尾或达到标签声明数量，不将暂时没有新响应判成获取完毕。所有负面标签均处理，合计可以超过 100 条；跨标签重复评价按 review_id 合并，所属标签保存在 product_comments.tags_json。原始评论和差评分别保存“评论原始.json”和“差评原始.json”，CSV 保留可读导出。

修改插件后需在搜狗扩展管理中重新加载插件，继续复用手动打开的 WS 和搜狗。


主图采集使用 `//*[@id="main"]/div/div[1]/div[2]/span` 的当前页/总页数，通过 debugger 发送 mousePressed、分段 mouseMoved、mouseReleased，从主图区域右侧拖到左侧；移动过程中持续按住鼠标左键，异常时也会释放鼠标；每次需确认页码递增。全部页码遍历且对应图片加载后才完成，图片按页码顺序保存并去重。旧的无页码校验主图完成状态，在未完成产品下次续采时会自动重新采集；已完全完成的产品仍遵守完成项跳过规则。插件修改后必须重新加载。

主图拖拽坐标以当前可见图片的交集边界计算，而非整个轮播父容器；通过 tabs.getZoom 只读获取实际缩放，将 DOM 坐标换算到浏览器输入坐标，不更改缩放设置。已在搜狗 50% 缩放的 9 张主图商品实测逐页遍历并读取 9 个 URL。

主图轮播默认模拟自然拖动：起点在当前图片宽度的 72%–86%，终点在 14%–28%，高度在 55%–80%，带小幅纵向偏移；每次移动 12–18 步，速度逐渐减慢。拖动前随机等待 350–1000ms，释放后等待 450–1100ms，按下和移动步骤也有随机间隔。仍逐页校验页码，异常时释放鼠标。修改扩展后需在搜狗扩展管理重新加载。

`product_info` 表保存每条上榜商品的详情汇总：商品标题、店铺 ID/名称、上架日期、在售状态、采集时间，`info_json` 保存匹配当前商品的接口响应，`raw_json` 保存含 SKU、评论、文件清单及阶段状态的采集结果。SKU、评论和图片索引同时分别写入 `product_skus`、`product_comments`、`product_images`；图片本身保存在商品目录。

原生评价只采集差评，保存 `差评.csv`；`差评原始.json` 用于数据库入库和保留筛选依据，不再生成普通评论、好评文件。默认多个差评筛选标签合并去重后最多 100 条，`--current --comment-limit N` 可调整差评上限。切换筛选后确认标签已选中才读取，保留差评标签与分类依据；没有差评入口或灰色标签时记为 skipped 并说明“入口未提供”，不声称没有差评。`商品数据.xlsx` 只展示差评数量。历史普通评论、好评文件和数据库记录不会自动删除。

`--image-sku`、`--image-main`、`--image-detail`、`--comment` 仅用于 `--android --current`，指定哪项就只执行哪项，允许组合；不指定则四项全采（含详情图）。分项采集各自记录阶段状态，不会将单项完成当作整个商品完成；主图与 SKU 共用图片清单但不会清除对方记录。评论每次读取一屏控件树中的多条可见评价，再滚动一段、批量解析、去重并保存；不先滑到底，因为原生列表可能回收离屏控件，底部一次快照不能代表全部评论。

安卓详情图：滚动至商品详情标题，点击该区域的商品图片进入大图查看器，按页码逐张截图并裁剪；保存 `详情图片/详情_01.jpg` 等文件，全部完成后按顺序合成 `详情图片/长图.jpg`。每次滑动校验页码，失败重试三次；图片清单保留主图、SKU 记录。截图清晰度受手机显示分辨率限制。

安卓浏览器跳转及网页数据调试：

```powershell
uv run python -m automation.pdd.spider.product --android --jump 'https://mobile.yangkeduo.com/goods1.html?ps=I11LLBVgc9'
```

手机需安装 Google Chrome 并允许 USB 调试。命令复用现有 ADB / UiAutomator，临时转发 Chrome 调试端口，在新标签页打开链接；识别浏览器打开 App 的确认框、网页“打开拼多多APP”和“在App打开”入口，确认拼多多进入前台后结束，不启动榜单队列或完整商品采集。网页文档、XHR / fetch 响应与页面状态保存在 `data/grab/pdd/网页调试/时间/`，包含 `网页.html`、`页面数据.json`、`网络记录.json`、`responses/`、`跳转操作.json` 和 `结果.json`。结果区分网页原始数据与 App 控件读取的数据；不能用分享时间、接口服务器时间冒充上架时间。Chrome 未登录时可能只取得登录页和登录相关响应。结束后仅移除本次临时 Chrome 转发，保留浏览器、App 和共享 UiAutomator 服务。

统一采集直接写入 PostgreSQL；手机、网页状态、图片路径、SKU 和评论都在 `spider.t_pdd_*` 中实时保存。设备与任务锁仍使用业务目录的本地锁文件。

一条命令运行完整手机流程（仅需要 iOS 设备）：

```powershell
uv run python -m automation.pdd.spider
```

将某天的采集数据改归另一日期（直接修改当前配置的数据库，不启动采集）：

```powershell
uv run python -m automation.pdd.spider --switch-data 2026-11-04 --switch-to 2026-11-05
```

命令先在业务目录 `data/backups/` 生成 PostgreSQL JSON 备份，再修改榜单的 `capture_date`，将各榜单日期目录整体改名，并同步更新数据库和目录内 JSON 的文件路径引用。数据库及 JSON 内的创建、更新、采集、开始、结束、完成和重试时间统一改为目标日期，保留时分秒与时区；`status.json` 的采集日期、重试 epoch 和文件修改/访问时间同步更新。ID、采集状态、商品上架日期、用户评论发表日期保持原值；Windows 文件创建时间不修改。改写前的 JSON 内容保存在备份旁 `.files.zip` 中。

源日期无数据、目标日期已有任意榜单、目标目录已存在或日期相同时会拒绝修改；发生普通异常时回滚数据库、已改名目录、JSON 内容和文件修改/访问时间。请在采集进程停止后执行，避免运行中的任务继续写入旧日期。数据库事务与文件改名无法跨系统原子提交，执行中勿强制终止或断电。

安卓商品页识别兼容原生控件及 App 内 WebView：WebView 必须同时具备“商品大图”、可点击购买/拼单按钮以及主图下方、评价区上方的标题文本，不再强制要求分享图标暴露 content-desc。继续核对目标商品标题，避免把推荐或评论文本当成商品；支持“共9张商品图 当前为第1张”页码和 WebView 图片控件。此项不代表所有 WebView SKU、评论及详情图布局都已实测兼容。

独立安卓商品队列同样经过 Chrome 跳转，不再直接给拼多多 App 发送 HTTP 商品链接。`--android --current` 仍只采集当前已打开页面，不执行跳转。

安卓商品采集确认“商品已售罄，推荐以下相似商品”时立即标记 `skipped`，原因保留该提示，记录 `outcome=sold_out`、`retryable=false` 并保存售罄页面截图和 XML；本条不再重试，继续下一商品。控件树无文字时使用当前安卓浅色页面提示模板识别，未匹配仍按原规则重试，不采集推荐商品。

指定单个榜单：`uv run python -m automation.pdd.spider.product --android --ranking-id 24`。ID 对应数据库 `rankings.id`，不限日期，只处理该榜单的有效商品链接，沿用完成/跳过状态；完成后退出，不等待其他榜单。不能与 `--current`、`--jump` 同时使用。省略该参数仍采集当天所有榜单商品。

安卓网页跳转按浏览器原始导航加载，不拦截或改写文档响应；网络记录仅旁观监听。跳转时点击业务页面精确文案“打开拼多多APP”，控件树无该文字时点击同名网页元素；不泛点“继续”“在App打开”等入口。只有点击业务按钮后才接受打开拼多多的系统确认及 App 前台结果，操作记录保存到 `网页跳转/跳转操作.json`。App 到达前台仅代表唤起成功，不代表目标商品信息已核对成功。

指定 iOS 单个榜单：`uv run python -m automation.pdd.spider.ranking --ios --ranking-id 12`。读取数据库 `rankings.id=12` 保存的来源链接，仅采集该子榜，不遍历 Excel；沿用记录原日期、目录及断点，已完成时默认跳过，配合 `--refresh` 可重新采集。不能与 `--current`、`--source-name` 同时使用。

`--refresh` 仅覆盖本次选中榜单的榜单采集数据，即使记录已完成也重新读取最新排名、标题、价格及指标，重新生成图片并实际复制分享链接；不会复用原已完成链接。SKU、详情图和评论由 `product` 命令独立采集。旧参数 `--refresh-media` 已替换为 `--refresh`。

### 手机与搜狗统一采集

```powershell
uv run python -m automation.pdd.spider
```

此命令的手机部分使用一台 iOS：按 Excel 采集榜单、生成长图并裁剪卡片，再逐个进入商品，依次采集主图、SKU 规格价格及放大图片、放大详情图片、差评（跨负面标签去重，最多 100 条）。完成商品后返回原榜单，不复制分享链接。图片来源是 iOS 屏幕裁剪，不伪造原图 URL、商品 ID 或评论日期；SKU 图片点击放大后截图，再返回规格面板；详情图片进入大图浏览器，按页码逐张右滑截图，保存放大图片在屏幕中的可见区域。同一页面复用 XML 和坐标；主图翻页只查询页码控件，不重复拉取整页 XML。手机未完成时整套重采；未确认结束、商品身份或原榜单时保留失败，不将部分结果标记完成。

SKU 图库没有暴露完整规格或价格时，返回规格面板逐项选择，校验当前选中摘要及确定价格后再打开对应缩略图采集大图。只有单一规格时，允许大图页缺少规格和页码标签；多规格且无法确认图片对应关系时保留失败。退出大图后必须确认已返回 SKU 面板。

大图翻页后若仍读到上一页，最多重读三次等待目标页码，不重复滑动；读到无关页码或总数变化时仍停止。SKU 图库点击图片未退出时，只在确认仍处于图库后尝试图片上方空白区域或向下滑动，再核对规格面板。清理异常不会覆盖先前的采集错误。

页码已出现但大图尚未就绪时，在同一页最多等待 60 秒、检查 12 次有效图片区域；等待中页码变化则停止，不保存黑屏或其他页图片。正常已就绪图片不增加额外页面读取。

规格候选不等于全部可售组合。选项只在所属滚动区域内安全可见时点击；App 自动清除或改选、重试后仍不匹配的组合记录到日志，不为它保存价格、图片或 CSV 数据行。仅在完整规格摘要与目标一致、价格确定时保存有效 SKU；没有任何有效 SKU 时不标记完成。

详情页有前置视频或说明模块时，跳过带播放控件的视频封面，短距离滚动查找真正的详情图片；找到后才进入大图浏览器核对页码。最多检查 12 个位置，无法确认图片入口时保留失败，不以页面分段截图代替。

统一入口按北京时间周一至周日保留每周榜单；本周续采沿用首次日期和目录。可用以下命令限定数据库记录，或只读查看状态：

```powershell
uv run python -m automation.pdd.spider --ranking-id 12
uv run python -m automation.pdd.spider --ranking-product-id 34
uv run python -m automation.pdd.spider --check
uv run python -m automation.pdd.spider --check --ranking-id 12
```

`--check` 只读检查业务路径、来源 Excel、iOS USB、WDA、搜狗进程、公共 WebSocket、搜狗插件和 PostgreSQL（只读 `SELECT 1`），显示 ASCII 环境面板和榜单手机/网页完成数、下次网页可尝试时间。指定榜单或产品 ID 时展开产品状态。`OK` 表示通过，`WARN` 表示未启动或未配置等提示，`FAIL` 表示检查失败；存在失败时退出码为 1，否则为 0。检查不会创建或迁移数据库、启动服务、打开网页、采集或同步，也不会显示连接密码。每项失败不会阻止后续检查。

产品的 `mobile_state`、`sogou_state` 独立保存 pending/running/completed/failed 状态。手机完成后跳过；未完成则按当前 rank 重新采集，替换该排名目录、标题、指标、图片、SKU 和评论，不复用上一轮的产品阶段。完成产品的排名、链接和文件保持不变。WebView 无元素也不算完成，会继续重试。

每轮手机整榜处理后，通过搜狗插件读取一次整榜。只给手机已完成、搜狗未完成且标题在整榜中唯一匹配的记录填写产品 ID、链接和上架日期。网页名次变化时仍保留数据库原 rank；重复标题不写入。网页失败不重采手机；`rankings.web_collected_at` 记录网页尝试时间，满 30 分钟才允许再次访问本榜单。手机未完成项不限重试轮数。手机整榜完成后，即使网页失败也继续下一个榜单；每轮手机采完时，一并重试本次采集范围内此前待补网页、且已满半小时的榜单。哪个榜单两端全部完成，就在 PostgreSQL 标记该榜单完成，无需再次同步。最后一榜手机采完后，等待最早到期的网页待补项继续重试，直到全部完成。Ctrl+C 可停止，下一次继续。

PostgreSQL 结构定义在 `mobile_schema.sql`。采集使用原生 PostgreSQL 参数和事务，状态更新立即落库；`--check` 使用只读连接，不初始化或迁移表。

指标按 `metrics.py` 的十个名称拆成文本字段，保留“万+”“起”等页面原文。`collection_month` 为 `YYYY-MM`；`collection_month_week` 按每月 1—7 日为第 1 周；`collection_year_week` 为 ISO 年周数，`collection_week_year` 保存对应周所属年份。这些字段表示该产品所属榜单的首次采集周期。

主图保存于 `product_main_images`，详情图保存于 `products_detail_image`，SKU 图片保存于 `product_skus.local_path`。所有图片路径相对业务目录的 `data/`，使用 `/` 分隔，例如 `grab/pdd/主榜/子榜/日期/榜01/SKU/0001.jpg`。评论仅采集差评，`product_comments.tag` 保存单个标签名称；SKU、评论及指标不再保留重复的原始 JSON 数据列。

统一采集的每个榜单日期目录只保留以下输出，排名目录至少两位、主图至少三位、SKU 与详情图片至少四位编号：

```text
榜单数据.xlsx
榜单长图.jpg
榜01/
├── 产品榜单截图.jpg
├── 产品截图.jpg
├── SKU.csv
├── 差评.csv
├── 主图/001.jpg
├── SKU/0001.jpg
└── 详情/
    ├── 0001.jpg
    └── 长图.jpg
```

`产品榜单截图.jpg` 是从榜单长图裁剪的卡片，`产品截图.jpg` 是进入商品后的页面截图。详情长图按详情图片编号升序竖向拼接，宽度统一到最宽图片并等比缩放。采集中不再写出 JSON、XML、分屏 PNG 或长图元数据；每次启动和榜单处理结束时清理输出目录，完成状态直接由 PostgreSQL 保存。旧的 `产品截图.jpg`（卡片）和 `产品页截图.jpg`（商品页）会对应改名，旧 SKU 图片改名后同步 CSV 路径。清理仅限配置输出目录内的榜单日期目录，遇到符号链接或目录联接时停止。

详情汇总长图任一边超过 JPEG 编码器的 65500 像素限制时，先按比例缩小再拼接，避免编码失败并减少内存占用；编号单张详情图片保留原尺寸。每周商品定位使用当前页面快照里的标题、出现次序和可见范围，再点击对应原生标题元素，避免滚动后的坐标偏移；保存前必须核对目标标题出现在商品页，身份不符时保留失败断点。首次商品页允许等待 120 秒，重建 WDA 会话时重新应用关闭静止等待的设置。

SKU 图库兼容明确的“券后 ¥金额”；未完整展示规格或确定价格时，保留已确认记录并返回面板逐项点击。统一入口对手机未完成产品重新采集整套数据。不可选或被 App 自动改选的组合不写入 `SKU.csv` 和 SKU 数据表。

iOS 差评逐条点击该评论的原生“菜单”元素，避免评论图片加载后坐标偏移；菜单数量变化时重新定位，未打开时最多尝试三次。读取“发表时间”后点击“取消”，核对仍在原评论列表。发表日期保存到 `product_comments.commented_at` 和 `差评.csv` 的“评论日期”列；菜单只显示日期时保留日期精度。统一入口以手机完成状态为准；已完成产品不因旧评论缺少日期自动重采，未完成产品下轮整套重采。

### PostgreSQL 唯一数据源

业务 `.env` 必须设置 `SPIDER_POSTGRES_URL`，连接数据库 `pdd`。表固定为：

```text
spider.t_pdd_rankings
spider.t_pdd_ranking_products
spider.t_pdd_product_skus
spider.t_pdd_product_main_images
spider.t_pdd_products_detail_image
spider.t_pdd_product_comments
spider.t_pdd_selling_point
```

图片仍保存在原业务目录，数据库保留相对 `data/` 的路径。原 SQLite 的榜单和产品 ID 保持不变，ID 筛选指令可继续使用。采集不再执行二次同步，`automation.pdd.spider.postgres` 指令已移除。

一次性迁移逻辑在 `migrate_to_postgres.py`：读取已备份的 SQLite 和旧 PostgreSQL JSON，按榜单与排名关联；已有位置使用最新 SQLite 整套产品数据，旧 PostgreSQL 独有记录补入并重映射外键，不拼接已换产品的旧图片、SKU 或评论。迁移在单个事务中完成，提交前逐字段核对，失败回滚；目标已有不同数据时拒绝覆盖。历史数据库和备份不删除。

PostgreSQL 集成测试使用独立的 `pdd_spider_test_*` 数据库，通过 `PDD_TEST_POSTGRES_URL` 指定；测试会清空该测试库的采集表，禁止指向正式库。
