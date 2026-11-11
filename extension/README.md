# automation.com.extension

Manifest V3 原生 Chrome 插件，无需 Node 构建。提供标签页列表、CSS/XPath 元素查询和 PNG 截图。
不需要 Token。Python 的 `automation.com.extension` 提供调用工具，源码插件位于本目录。

## 安装使用

1. 启动服务：`uv run python -m automation.com.extension_bridge`。
2. 启动本机 Chrome：`uv run python -m automation.com.browser`。
3. 打开 `chrome://extensions`，开启开发者模式，加载已解压扩展，选择 `D:/repo/pdd-seller-desk/extension`。
4. 插件默认自动连接 `ws://127.0.0.1:8765/`，不需要打开配置页。点击图标可查看状态或修改本机端口。
5. 已安装旧版插件时，点击“重新加载”，以启用免 Token 协议。旧服务也需重启。

断线后每 3 秒重连，后台心跳保持连接。只允许连接 127.0.0.1；服务拒绝普通网页来源和无效角色握手。
角色握手仅用于区分插件与测试客户端，不包含认证凭据。

## 单功能测试

```powershell
uv run python tests/com/extension_bridge/test_connection.py
uv run python tests/extension/test_get_element.py --selector ".captcha-wrapper"
uv run python tests/extension/test_get_element.py --selector-type xpath --selector "/html/body/div[4]/div/div/div[1]"
uv run python tests/extension/test_screenshot.py
uv run python tests/extension/test_screenshot.py --selector ".captcha-wrapper"
uv run python tests/extension/test_screenshot.py --full-page --output "data/extension/full.png"
```

这些测试只通过 WS 调用插件，不使用 Playwright，不创建或关闭浏览器。
多页面时，用 `uv run python -m automation.com.extension_bridge tabs` 查看 tab_id，再给测试增加 `--tab-id 数字`。
`--port 8765` 指 WS 端口，不是 Chrome 的 9222 调试端口。

元素结果默认不包含文本或表单值；需要文本时给元素测试增加 `--text`。表单值和编辑区域仍不返回。
结果包括视口坐标 x/y、文档坐标 page_x/page_y，单位均为 CSS 像素。
首版只支持主文档，不查询 iframe 或 shadow DOM。允许 `https://mms.pinduoduo.com/*` 和 `https://dun.163.com/*`。
本地验证只允许 `http://127.0.0.1/extension-test/` 路径。

截图使用扩展的 debugger API，支持视口、整页和唯一可见元素，面积上限为 2500 万 CSS 像素。
浏览器可能显示调试提示；若 DevTools 或其他调试器占用目标标签页，截图可能失败。
插件没有通用点击、输入、拖动接口；提供限定拼多多登录页的账号登录流程，验证码不自动处理。

## 通过按钮检测关联元素

新增只读 `captcha_detect` 任务（不是验证码求解或拖动）。重新加载插件、重启 WS 服务后：

```powershell
uv run python tests/pdd/captcha/test_detect.py --button-class "slide-button"
uv run python tests/pdd/captcha/test_detect.py --button-xpath "//*[@id='slider-button']"
```

两个定位参数互斥。支持前导点、多 class 空格组合；支持 `--port`、`--tab-id` 和 `--output-dir`。
检测会跳过隐藏按钮副本；可见按钮不唯一时不选择。最多检查 8 层祖先和每层 500 个节点，
寻找共同容器及背景、拼图、轨道，并排除同一轨道的进度填充层。多个候选或缺失组件时保留诊断信息并使测试失败。
截图和元素检测均由插件执行，Python 仅保存 JSON、原始截图并生成本地标注图，不读取表单值。
完整参数与输出位置见仓库 README。主文档之外的 iframe/Shadow DOM 尚未支持。

## headless

先用可见 Chrome 在指定目录安装插件，再关闭该实例，用相同目录启动：

```powershell
uv run python -m automation.com.browser --headless --user-data-dir "data/User Data" --url "https://mms.pinduoduo.com/home/"
```

插件通信和截图不需要调试端口。需要 Playwright Cookie 导入导出时，再显式增加 `--debug-port 9222`。
不要把 chrome-headless-shell 当作支持本插件的完整 Chrome。普通 Chrome 不依赖 --load-extension 启动参数安装插件。

## 集成测试

```powershell
uv run python tests/extension/test_integration.py
uv run python tests/extension/test_integration.py --headed
node --test tests/extension/test_protocol.mjs
```

使用单独 Chrome for Testing 和测试目录，验证免 Token 连接、重连、查询与截图，不操作用户现有浏览器。
具体测试入口、依赖安装及 .env 说明见仓库 README。

## 拼多多账号登录

重新加载 0.4.0 插件并重启 WS，手动打开拼多多登录页：

```powershell
uv run python tests/pdd/browser/test_login.py --dry-run
uv run python tests/pdd/browser/test_login.py
```

正式模式从飞书按 Cookie 文件名匹配账号，切换“账号登录”、填写并点击一次登录。
`--cookies` 仅选择店铺，不导入 Cookie。账号密码不输出、不写入结果文件。
插件收到凭据前及提交前均检查目标地址；网易易盾等其他网站不能接收此登录命令。
遇到验证码或身份验证时只报告并停止，不过验证，不自动重试提交。完整参数见根目录 README。

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


## 搜狗榜单采集

在搜狗扩展管理中加载/重新加载本目录，插件版本 0.13.0；配置页连接地址使用 `ws://127.0.0.1:8765/`。
运行 `uv run python -m automation.pdd.spider.ranking` 后，由 Python 管理本机通信服务，插件负责读取榜单元素、滚动和截图。
浏览器窗口、视口和缩放由用户手动设置，插件不再修改。没有浏览器远程调试端口，设备模拟使用扩展自身的 debugger 权限。
`ranking_open`、`ranking_navigate`、`ranking_read`、`ranking_screenshot`、`ranking_close` 仅操作插件创建并登记的榜单标签页；商家工作台动作的页面范围保持独立。
手机模式使用 `uv run python -m automation.pdd.spider.ranking --ios`，不需要此插件。


`uv run python -m automation.pdd.spider.sogou`（或 `.chrome`、`.edge`）独立补采网页产品 ID、链接和上架日期；自动打开对应浏览器的任务窗口并等待页面加载，成功后关闭任务窗口。三个浏览器均须安装本插件。主命令 `automation.pdd.spider` 仅采集手机，不再自动补采网页。
商品通过 `product_open` 在导航前启用扩展 Network 监听，`product_dom` 仅允许固定元素操作；`product_network` 按商品标签页和评价请求阶段隔离响应，`product_close` 只关闭此商品临时页。
无需任何浏览器远程调试端口，浏览器扩展需要重新加载至 0.13.0。

## 公共 WS 配置

可先在 automation 项目根目录的独立终端运行公共服务：

```powershell
uv run python -m automation.com.extension_bridge
```

保持该终端运行，再在其他终端执行 `automation.pdd.spider.sogou`、`automation.pdd.spider.ranking`、`automation.pdd.spider.product` 或 `automation.pdd.screenshot`。这些功能优先复用已有公共服务，不会重复启动，也不会在退出时关闭该服务；没有服务时自动启动后台服务。手动运行的服务使用 Ctrl+C 停止。

在 automation 项目根目录 `.env` 设置 `AUTOMATION_WS_PORT=8765`。搜狗与截图共用该服务，插件连接地址保持一致。服务按 Chrome／搜狗分发请求，已有服务优先复用，业务退出后保留公共 WS。更新代码后重启旧版 bridge，并重新加载两个浏览器的插件。
