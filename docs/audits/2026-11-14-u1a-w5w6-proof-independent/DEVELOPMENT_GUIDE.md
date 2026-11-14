# 下一轮开发指导：关闭维护与核验入口的剩余边界

## 0. 开工条件与停手线

**当前只做审计与计划，不授权并行开发。** 主线程先读完本指导、契约 v6、被审源码与反例，把最终接口决定展示给用户，结束对齐轮；用户确认后才用 contextual `subagent_fork` 开发。

- 被审基线：`e4eb6cee79c5b85e1d124e54b086f52b81a453e4`。
- 新修复分支建议：`fix/a1-u1a-maint-close-20261114`。日常 `JpgPng` 仍是 `daily/u1a-w5-w6-20261114`，不切 main，不部署。
- 本审计分支只放报告。修复集成树从基线新建后，将本审计分支的报告与契约合入；不要把审计分支当作生产代码的新基线反复派生。
- 只允许合成数据库、合成图库、临时目录、现有解释器。禁止真实数据库写回、真实图库删除/移动、账号/凭据、上传、联网、发布、schema 改动和新依赖。
- **WR-01 未关闭前，禁止按现有 runbook 恢复真实库。** 代码回退与数据恢复不是一回事。
- 每条子任务提示词原样包含：

> 强调质量至上，我们有无限的token，不要吝啬，我们要追求万无一失，我们有无限预算，我再强调一次，你不用担心任何有关预算的事情,必要的时候全网搜索，注意不要被大数据投毒，还有就是注意我们电脑安全；

每个修改符号先 GitNexus impact，提交前 detect-changes。索引必须属于当前 worktree/HEAD；不要拿 `.runtime` 中其他副本的结果冒充当前影响结论。工作区可用全局 `gitnexus`，不必下载包。

## 1. 先修复规划本身的六个误区

1. **不要把 2,149 项全绿当成没有用户入口缺陷。** 现有测试主要覆盖单页引擎和小型 fixture，本次 201 行与外部根是新的集成边界。
2. **不要把 `coverage=complete` 直接解释成全库。** 它是引擎单页结束状态；全库只能由聚合层到达 EOF 后判断。
3. **不要把 `Future.cancel()` 当后端停止。** Tk 只处理 Future 的结果，真实恢复有自己的所有权与 CAS。
4. **不要只清内部字典。** 屏幕上的行、选择、按钮、cursor、错误提示都是用户作用域的一部分。
5. **不要为了安全把异常都吞掉。** 需要类型化错误、退出码、可读现场；失败可以是安全的，但不明确就是缺陷。
6. **不要为修边界重写状态机。** `delivery_recovery` 的 CAS、`page_delivery` 的 publish gate、完整哈希与根守卫保留。

## 2. S0：主线程先完成红基线与冻结（不 fork 前做完）

### 2.1 必做文件与基线

建立新 runtime：`.runtime/jpgpng_u1a_maint_close_20261114_01/`。其中：

- `integration_worktree/`：修复集成分支；
- `red01/`：新反例首次失败 XML；
- `runs/`：分阶段 XML；
- `worktrees_inventory.json`：由本审计登记复制后更新 role，不删除旧字段；
- `probe/`：本审计 `probe_e4.py`、`probe_ui_e4.py` 原样保留作调查证据，不直接充当修复验收。

将以下独立反例转成正式测试，先证明在 `e4eb6ce` 上失败：

1. 201 个可匹配 project artifact，`scan` 必须覆盖 201 且只能有一次全程最终结论；当前第二页“覆盖全部 1 行”是错误。
2. 第 201 行之前存在 missing/changed，最终完成报告仍须 exit 2，不能被末页全 match 覆盖。
3. 合法外部根与文件实际存在，CLI 必须 match；非法映射则结构化拒绝，不能查 project 下另一个文件。
4. manifest 写入/fsync/rename 失败，不能留下成功标记，必须 typed error；清理失败另报。
5. `members[0].bytes="not-a-number"` 等坏清单必须返回 `manifest_unreadable`/exit 3，而非 `ValueError`。
6. 51 行核验记录，第一页与第二页无重复/遗漏，按钮明确只操作当前页。
7. A→B 后旧 A 行不能继续作为当前可行动行；查询失败必须出现面板错误。
8. 页面 delivery id 改变时，后端在**任何文件观察之前**返回 `state_changed`；另一个场景必须是 `recovery_needed`（不是只有 completed），才能证明确实越过了 completed 快路。
9. 旧 WC 12、RFB 12、RFA 12、RF 15、UA 14 原样运行，不删断言、不加 xfail。

测试只用 `tmp_path`/`--basetemp` 下合成数据；不要让临时 fixture 打开工作区 `download_logs`。

### 2.2 冻结的接口选择（建议按此执行）

- `scan_library_artifacts` 不改签名，保持单页引擎。
- 新增 `core/library_scan.py`：只负责**只读映射 + 游标聚合 + 有界汇总**，不复制哈希算法。
- `maintain scan` 默认走向前推进到 EOF 的全程报告；单页用显式 `--page`。已有调用若预期单页，按契约迁移表调整，不静默保留误导行为。
- `--after` 可保留为续跑入口，但输出只能称“从指定游标继续”，不能在未知前段历史时宣称全库完成。
- `recover_verification_page` 新增可选 `expected_delivery_id` 以保持其他内部调用兼容；**UI 和桌面端口必须每次显式传入**。它不是新授权，只是过期证据的快速拒绝。
- 所有新增 API 的字段使用明确名词，不把 `ok` 同时用于“备份健康”“字节未改”“与当前源相同”。

## 3. S1：备份失败协议与恢复说明（独占维护模块）

### 3.1 文件归属

- `core/library_maintenance.py`
- `tests/test_u1a_library_maintenance.py`
- 新增 `tests/test_u1a_restore_protocol.py`（若确有恢复协议函数；纯文档脚本则放调查 probe）

主线程负责 `maintain.py`、runbook 和最终 CLI 集成，避免 S1/S2 同时改 dispatch。

### 3.2 先补 manifest 验证，不碰指纹算法

保留 `logical_fingerprint`、`_encode_value`、`_table_digest` 的冻结算法。新增一个受限的 `_validate_manifest_v2(manifest)` 或等价函数，在任何 `int(...)`、`.get(...)` 链和数据库读取之前完成：

- `manifest_version` 必须为 int 2；bool 不是 int。
- `members` 是非空有限 list，每项只有约定的 name/bytes/sha256（以及已定义的 sealed 标志）；bytes 是 `type(x) is int` 且非负，sha256 是 64 位小写 hex。
- `sealed_members` 唯一、大小写无冲突，全部存在于 members；`copy_name` 必须指向 sealed 数据库主文件。
- `mode` 只允许已登记值；online 备份文件组按模式验证，file_group 的 WAL/SHM 不能和 online 主库混用。
- `logical_fingerprint.version=1`，digest/objects_digest 是合法 SHA；`rows`、`tables` 必须是 dict，表名/行数/digest/order 类型与数量有界，order 在已支持集合。
- `finalizer.format=manifest.json`、`sealed_by=backup_library`；校验格式，不将文本时间戳当安全凭据。
- 字段数量、表名长度、总清单大小仍受限；未知关键字段、未知版本、未知成员类型拒绝，不“宽松跳过”。

失败统一 `BackupError("manifest_unreadable", safe_detail)`。detail 不包含完整路径、数据库内容、授权或任意长 payload。`--json` 必须返回同样的错误结构与 exit 3。

**特别注意**：不要只捕获 `ValueError` 就算修好。要把非法结构挡在业务读取之前，且 OS I/O、SQLite 错误、编程错误分类不同。不能用 `except Exception: return ok=True`。

### 3.3 统一备份收尾，不把清理变成第二次破坏

把 `target.mkdir` 之后到 `_write_manifest` 返回的完整过程置于同一异常边界。任何阶段失败：

1. 记录 primary error；
2. 只对本次新建、已验证在目标内的路径做收尾；
3. 不跟随符号链接/junction，不删除额外出现的陌生文件；
4. 清理成功才没有失败目录；清理失败则保留目录，报告 `backup_cleanup_failed`，附安全诊断；
5. 保留 KeyboardInterrupt/SystemExit 原类型，不把中断说成成功；
6. 成功返回的 `verified` 不得暗示 damaged source 已健康；verify 仍分别检查 sealed/logical/healthy/compatible。

故障矩阵至少包含：复制中途、逻辑指纹、inspect、manifest write、fsync、os.replace、清理 unlink、目标额外文件。每条断言“成功标志不存在”和“错误类型正确”，不是只断言目录不存在。

### 3.4 WR-01 先修文档安全边界，不急着写自动 restore

runbook 的新恢复段必须先声明“仅在副本演练，真实恢复另批”。如需给人工步骤，按契约 v6 的“先验证备份→副本准备→隔离当前组→匹配模式→重开逻辑对照→保留回退”写。

- 不将“没有任务在跑”当成进程已退出；明确检查所有应用与辅助进程，不能用不确定的进程查找命令杀死程序。
- 不在当前工作目录直接 `Remove-Item *-wal,*-shm`。
- 不把“备份模式”藏在操作者猜测里；从已验证 manifest 读取，非法/缺失即停。
- 不声称已做真实外置卷或真实恢复演练。本批仍不操作真实库。

## 4. S2：扫描全程、外部根和预算（独占扫描与新聚合）

### 4.1 外部根接线

主线程 CLI 集成应使用与桌面相同的只读映射：

```python
resolver = PathResolver(AppPaths(project_root))
OutputLocations(AppPaths(project_root), resolver).load()
```

然后才把 `(database, resolver)` 传给扫描引擎。这个过程不能启动 DesktopSession、账号、网络或 writer。

- 配置文件不存在：空外部映射，project 路径仍可扫描。
- 配置存在但非法：**终止并报告 mapping 错误**，不是退回空映射继续。
- root id 未登记：行级 `root_unavailable`，不扫描另一个根。
- 外部根目录不存在：保持不可用，不 mkdir。
- `--root` 继续表示项目根，用于查找 `config/output_roots_v1.json`；文档不要称它“覆盖所有 artifact 的根”。

### 4.2 新聚合器的最小算法

每轮只做：

1. 从 `cursor` 调 `scan_library_artifacts(..., limit=page_limit)`；
2. 将本页计数累加到 bounded summary，保留有限 findings 示例和 unknown 原因直方图；
3. 记录 `next_cursor`、实际读取字节/耗时、页数；
4. 检查 cancel/总预算；
5. `coverage == "empty"` 且 rows 为空才判断 EOF；`page` 且有合法前进的 cursor 才继续；`budget/cancelled/error` 一律停止；
6. 同一 cursor 重复、倒退、无行却 `page`、`next_cursor` 非法都 typed `cursor_unavailable`，绝不无限循环。

**禁止**：

- 用 `has_more=False` 把 budget 停止当 EOF；
- 只看最后一页的 findings；
- 把全库行/完整路径无界存进 list；
- 让“文件大于单页预算”的行每轮都阻挡同一个 cursor；
- 通过增加 sleep、调大 timeout 来掩盖游标/竞态错误。

### 4.3 预算和恢复口径

- 单文件超过总/页字节预算可以标 `unknown/file_exceeds_budget`，该行已经有结论，游标可前进；这与“没读到该文件”不同。
- 真正 I/O/时间中断且行没有 verdict 时，游标停在前一条已判结论；恢复后重试该行。
- 默认 `limit` 是每页数量上限；新增总预算需要清晰命名（例如 `total_budget`），不要悄悄改变 `ScanBudget` 的每页语义。
- 启动时必须提供取消入口，CLI 至少键盘中断要有可控收尾；GUI 若复用必须走 query owner 的 cancellation，不在 Tk 线程扫描。
- 跨页连续遍历不是原子全库快照。报告中写 `snapshot_mode="incremental_walk"`；遍历中新增/删除的变化不能冒充同一时刻视图，必要时标 `revision_changed` 或“变化未隔离”。

### 4.4 汇总与退出码

`_scan_outcome` 消费的是**全程累计报告**，不是单页结果。至少验证：

- 201 全 match → exit 0、`library_rows=201`、`pages_examined=2`；
- 200 match + 1 changed 在末页 → exit 2；
- changed 在中间页 + 末页 match → 仍 exit 2；
- 预算到第一页末尾 → exit 5，可恢复 cursor 明确；
- 继续 cursor 后只完成尾页 → 文案必须说“本次续跑页”，exit 5，除非有同一全程报告能证明从首行到 EOF 且汇总完整；
- 空库 → exit 0，不称 0 行异常；
- 外部根 match + 未注册 root → 分别 match/root_unavailable；
- JSON 的 `exit_code` 与进程返回值一致。

## 5. S3：核验列表、错误状态与恢复身份（独占 UI/恢复端口）

### 5.1 数据与动作的最小模型

保留 `list_verification_pages` 的稳定游标，不为 UI 新建无界查询。controller 至少维护：

```
_verification_cursor_stack = [None]   # 当前页的 after
_verification_next = None
_verification_total = 0
_verification_view_state = 'loading' | 'ready' | 'error'
_verification_read_error = None
```

- 下一批：有 `next_cursor` 才 append 并发起**一页**查询；上一批：cursor stack 长度 >1 才 pop。
- 新页请求仍带 `(UI epoch,batch,cursor)`，响应除 epoch/batch 外还要对应当前 cursor。
- `navigate` 离开核验页也作废；回到核验页重新读取，而不是把旧缓存当最新。
- 按钮名应是“下一批 50 行”，不要暗示还有自动批量恢复。批量恢复仍只对当前可见、明确选中的最多 20 行串行执行。

### 5.2 失效必须立刻到屏幕

在 `_invalidate_verification` 内完成内部清理后，若尚未 dispose，必须立刻：

1. 清 view rows/选择；
2. 将 panel 置为 loading 或 error；
3. 禁用恢复和下一批按钮；
4. 清 cursor stack、total 和 summary；
5. 保留当前在飞后端命令的身份，但不允许它把结果渲染到新批次。

不要等下一次 `set_verification_rows` 才覆盖旧画面。dispose 后不能再碰 Tk widget。

新增 panel 方法可选 `set_view_state(state,message)`，内部只管理展示；不要把 DB/Future/线程放进 panel。

### 5.3 错误 Future 要按查询处理

`_poll` 对 `verification_summary` 与 `verification_pages` 的异常必须路由到专用 `_verification_read_failed`，不是只给全局 `_show_error`。记录请求时的 epoch/batch/cursor，避免旧请求错误擦掉新页面的状态。

- pages 失败：清 rows 或将它们标为不可操作，panel 显示“读取失败，可重试”；
- summary 失败：不将旧 summary 当新批次事实；
- 两个 Future 都要处理，错序完成不能复活旧数据；
- 不自动连续重试，不让错误循环耗尽 query capacity；
- 若保留双查询，明确它不是同一 SQLite 快照。更好的方案是 repository 新增一个短事务读取端点，但这属于同文件独占改动，不能多个 lane 顺手实现。

### 5.4 在哈希前校验 delivery identity

API 建议：

```python
recover_verification_page(batch_id, illust_id, page_index, *,
                          command_id=None, expected_delivery_id=None)
```

实现要求：

1. 若提供 `expected_delivery_id`，严格校验字符串；
2. 重放 command_id 时也检查它与预期 delivery 一致，避免旧命令 id 被误用于另一行；
3. `_read_recovery_page` 后、任何 `_observe_recovery_page` 前，比较 `page.delivery_id`。不一致直接返回 `outcome=refused, reason=state_changed`，不调用观察器；
4. 这个快速拒绝不能用 writer 的 `previous_delivery_id` CAS 代替，也不取代 CAS；
5. `DesktopSession` 的公开端口与私有方法、`_verify_step`、fake session 全部显式传递 `row['delivery_id']`；
6. 返回结果保留 `delivery_id`、`previous_delivery_id`、`generation`、`health=None`，不伪造文件观察。

**常见错误**：只在 UI 加检查、后端没加；把 expected id 放到 payload 却没有 preflight 检查；让旧 command replay 绕过新身份；修改 writer 让它在 previous id 不符时继续 CAS。这些都不得出现。

## 6. S4：集成、文档与整理（主线程串行）

### 6.1 集成顺序

S0 → S1 + S2 + S3（文件不重叠才并行）→ 主线程 CLI 接线 → 全量测试 → 只读 contextual fork 审查 → 原 lane 修复 → 文档/登记。

每个 fork 交付：commit id、影响分析、测试 XML、实际 diff、未解决边界；不能只发“已完成”。合流后主线程独立跑测试与 diff 审核。

### 6.2 正确的测试矩阵

```powershell
$env:PYTHONNOUSERSITE='1'
$py='D:\Miniconda3\envs\JuiLiu\python.exe'
& $py -s -B -X utf8 -m compileall -q -f core gui utils
& $py -s -B -X utf8 -m pytest -q -s -p no:cacheprovider `
  --basetemp <全新目录> --junitxml <报告目录/non_tk.xml> `
  tests --ignore=tests/test_collection_presentation.py `
        --ignore=tests/test_source_presentation.py
```

- 排除名不要写成 `test_collection_view_presentation.py` 或 `test_collection_view_source_presentation.py`，它们不是本仓库文件。
- 两个 Tk 文件共 9 项逐进程隔离，使用已有 TCL_LIBRARY/TK_LIBRARY；不要在同进程反复创建/销毁 Tk root。
- `test_collection_verification_panel.py` 的 60 项属于主体，不要从主体排除后又宣称已含在全量。
- 结果从 XML 汇总 `tests/errors/failures/skipped`，不要只看收集数或“进程 exit 0”。

### 6.3 当前文档只更新这些入口

- `README.md`：当前候选、实际日常 HEAD、当前未修问题与维护入口；
- `使用说明.md`：scan/inspect/backup/verify 的用户含义及恢复边界；
- `docs/README.md`、`docs/PROJECT_LAYOUT.md`：当前导航与 worktree/证据规则；
- `docs/optimization/IMPLEMENTATION.md`：顶部当前接续指向本审计与修复批次，历史 4M 保留；
- `TASK_PLAN.json` 的 `current_plan.active_workstream`：`audited_commit=e4eb6ce`、`fixed_commit=null`（修复前）、新 WR 编号与下一步；不要继续把“快进 daily”写成未做；
- `LOG.json`：只追加当前审计/修复事实；
- `docs/optimization/2026-11-14/U1A_W5_W6_CONTRACT_V5.md` 第 3 行改为 `../../audits/2026-11-14-u1a-w5-w6/README.md`。

原始 `test_w5w6_recheck.py` 可从 `.runtime/jpgpng_u1a_w5w6_audit_20261114_01/test_w5w6_recheck.py` 恢复到旧审计的 evidence 链接位置；保留原件，不改写测试来让它变绿。登记 SHA-256，保留 `tests/test_u1a_w5w6_boundaries.py` 只改 ROOT 的说明。

### 6.4 不要粗暴清理 worktree

当前 `worktrees_inventory.json` 的 75 项中，8 项 dirty；其中有源码和测试草稿。先分类：

- **保留**：daily、当前修复、当前审计、带未提交证据的树；
- **冻结引用**：历史 checkpoint、审计基线；可保留分支/tag，但不等于可删除其目录中的唯一证据；
- **候选移除**：已合并且没有未跟踪唯一材料的测量副本，逐个核对后才能移除；
- **草稿**：先保存 diff/未跟踪文件，建 WIP 提交或明确归档，不能清理。

只登记本批新增/变更的 worktree 和少量当前引用；历史目录逐步整理，别为了“干净”一次扫掉所有证据。每批只新增一个 runtime 根，结束更新 role，不新建无实际用途的顶层文档包。

## 7. 完成判据

只有全部满足才可建议用户继续：

- WR-01..07 均有独立反例及关闭证据，WG-01..04 已登记或完成；
- 新旧测试没有 skip/xfail/删断言，201 行与 51 行跨页真实覆盖；
- 外部根合法/非法/掉线、manifest 错误/清理失败、恢复文档合成演练通过；
- 全部编译、主体测试、Tk 隔离与只读 fork 审查通过；
- 日常 main/数据/图库/账号未动；文档不再把当前未修问题说成“无 P2”；
- 下一步仍是另批的 `finish_source`、intent projection/索引、carry 长事务、page ceiling 配置、legacy record_manager 健康门与真实外置卷演练。不要借本批偷偷打开归档封包、云端回读或释放。
