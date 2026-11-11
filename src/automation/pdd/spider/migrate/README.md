# Spider SQLite 手动迁移

## 图片独立表升级（不执行正式库）

新增 `product_images`，一张图片一行，唯一键为 `product_info_id + image_type + position`。类型包括 `main`、`sku`、`detail` 和生成的 `detail_merged`。生成长图没有单一源 URL，因此 source_url 为 NULL；本地路径相对 `ranking_products.directory`。下载开始、失败、完成都会立即记录，重跑更新原位置。新库不再包含 `product_info.images_json`。

已有数据库停止采集并备份后，使用能遇错停止的 SQLite 工具，依次执行：

1. 结构准备：`20261102183000_product_images.sql`，创建图片表。
2. 数据迁移：业务目录 `data/upgrade/20261102183001_product_images_data.sql`，从旧 `images_json`、原始文件清单及已有图片元数据迁移。仓库保存同名可测试源文件。
3. 结构收尾：`20261102183002_product_images_cleanup.sql`，核对旧路径均已迁移后删除 `images_json` 字段。

每个文件独立事务，执行失败立即停止并 `ROLLBACK;`，不要直接跳到第三步。尚未保存过的历史源 URL 无法从本地路径还原，不伪造 URL；旧路径保留、状态设 pending，后续采集补全。已有已完成产品不会自动重采，需要补全其历史 URL 时应显式重新安排采集。本组迁移独立于旧 collection_tasks 状态归属迁移，不会删除该表或改变它的版本标记。

## v3 → v4：状态归属榜单和商品（待配套业务代码上线）

结构变更与数据回填分开，按以下顺序执行：

1. 结构准备：仓库 `migrate/20261102173000_owner_states.sql`，只新增字段，保留旧表，临时版本为 400。
2. 数据回填：业务目录 `data/upgrade/20261102173001_owner_states_data.sql`，根据执行时的 `collection_tasks` 数据回填状态，临时版本为 401。仓库同名文件为可测试的源文件。它不新增或删除任何业务表、字段；仅创建事务内临时校验对象。
3. 结构收尾：仓库 `migrate/20261102173002_owner_states_cleanup.sql`，校验关键检查点，删除 `collection_tasks`，最终版本为 4。

三份文件不是替代关系，必须按序执行。每步独立事务，失败后执行 `ROLLBACK;` 并重试失败步骤；不要重跑已经成功的步骤。禁止在三步之间恢复采集。

**只生成，尚未执行正式库升级**。当前 `database.py` 仍依赖 `collection_tasks`，必须先完成配套代码调整，再停采、备份并执行脚本。旧版应用不能打开升级后的库。

- `rankings` 新增 `collect_state`、`screenshot_state`（榜单长图）、`export_state`、`device`、`last_stage`；保留总体 `state`、`last_error` 和时间。
- `ranking_products` 新增 `crop_state`、`screenshot_state`（商品页截图）、`link_state`、`detail_state`、`detail_attempts`、`last_stage`、`last_error`、`completed_at`；保留总体 `state`。详情状态对应旧 `product` 步骤，不与完整采集混用。
- 从旧步骤记录迁移检查点；`running` 重置为可重试的 `pending`，旧详情 `skipped` 转为 `completed`，原始原因可从备份查看。既有总体失败不因截图和链接完成而被改成成功，防止遗漏返回榜单失败。
- 开页和返回操作不再独立存状态，仅保留最近步骤的失败信息；原榜单已有总体错误优先保留。
- 路径仍由已有 `directory` 和固定文件名确定；升级不移动文件、不验证图片有效性，续采仍需验证文件。
- 结构收尾脚本核对数据后删除 `collection_tasks`，版本升至 4。整轮、来源发现、历史事件及逐步骤产物列表不再入库，历史记录留在升级前备份。未发现子榜的来源不会生成占位榜单。

在能遇错停止的 SQLite 工具中依次完整执行三份脚本。脚本使用事务并校验版本及核心检查点；任何错误都要停止并执行 `ROLLBACK;`，不要跳过错误继续执行。成功后 `PRAGMA foreign_key_check;` 应为零行，剩余五张业务表。再次执行会被版本检查拦截。

基于 2026-11-02 现有库副本验证：3 个榜单、54 个商品；商品总体 completed=39 / failed=11 / pending=4；商品截图 completed=43；链接 completed=39 / failed=4；榜单长图 completed=2 / failed=1。迁移前后原业务字段逐行一致。正式执行时按届时数据动态转换，不写死这些数量或 ID。


## 已有六张表：增加完整采集状态

如果已经执行过早期六表迁移，先备份，再完整执行 **`20261102142611_capture_state.sql` 一次**。它只给 `ranking_products` 增加 `state`，`rankings` 使用已有的 `state`。已有商品、指标、链接和 ID 不变；只采过文字的榜单保持待补全状态，防止误跳过截图和链接步骤。

完成条件：长图、所有卡片裁剪、商品首屏截图、分享链接及返回原榜单均完成。当天已完整完成的榜单和商品会跳过；失败或中断项下次续采。新建数据库不需要执行这个增量 SQL。

## 原五张表：迁移到六张表

迁移文件：`20261102115136_rankings_products.sql`。

1. 停止所有正在使用 spider 数据库的采集命令。
2. 用 SQLite 工具打开 **spider** 的数据库，默认是 `D:\repo\automation.pdd.spider\data\sqlite.db`。不要选择 screenshot 的数据库。
3. 使用 SQLite 工具备份数据库，例如执行 `VACUUM INTO 'D:/repo/automation.pdd.spider/data/sqlite-before-20261102115136.db';`，目标备份文件必须尚不存在。
4. 在没有其他未提交事务的情况下，完整执行本迁移 SQL。
5. 最后的 `PRAGMA foreign_key_check;` 应返回零行。确认六张新表出现，再运行新命令。

脚本使用事务。任何报错都应停止执行并运行 `ROLLBACK;`，不要跳过错误继续执行后面的语句。

数据转换：

| 旧数据 | 新位置 |
|---|---|
| `spider_runs` | `collection_tasks`，kind=run |
| `spider_boards` | `collection_tasks`，kind=source，保留未发现子榜的失败来源 |
| `spider_lists` | `rankings`，保留原 ID |
| `spider_items` | `ranking_products`，保留原 ID |
| `row_json` | 规范字段及 `metrics_json`；完整原 JSON 保存在 `ranking_products.raw_json` |
| `product_json` | `product_info`、`product_skus`、`product_comments`；完整原 JSON 保存在 `product_info.raw_json` |
| 原步骤状态、商品重试次数 | `collection_tasks` |
| `spider_events` | `collection_tasks`，kind=event |

复制成功后事务内删除五张旧表，最终只有六张新业务表。评论在同一产品信息内去重，差评标记合并；未经标准化的原内容仍可从 `raw_json` 查找。历史上缺失的完整文案无法恢复，不会根据数值编造文案。文件和图片不移动。

该 SQL 可用于旧版数据库，也可在空库初始化；迁移成功后再次执行不会重复导入。已有旧库在迁移前会被程序拒绝打开，不会自动修改。

采集当前手动打开的 iOS 榜单：

```powershell
uv run python -m automation.pdd.spider.ranking --ios
```

此命令只读榜单卡片并入库，一轮后退出；不读取 Excel、不导航 Safari，也不逐商品打开详情获取分享链接。
