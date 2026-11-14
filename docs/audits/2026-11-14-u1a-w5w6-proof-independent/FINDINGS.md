# 逐项发现：W5/W6 proof / e4eb6ce

行号均指被审 `e4eb6ce`，不是旧基线。优先级：P1 为真实数据操作的高风险入口；P2 为可复现功能、诊断或协议错误；P3 为文档、可维护性和可追溯性缺口。

## WR-01 / P1：恢复手册的文件组步骤不够安全

**位置**：[U1A_SWITCH_RUNBOOK.md](../../optimization/2026-11-14/U1A_SWITCH_RUNBOOK.md) 第 73–85 行，尤其是 82–85 行；类似文本由 [library_maintenance.py](../../../core/library_maintenance.py) 的 `_restore_steps:536–563` 生成。

### 实际承诺

手册让操作者：

1. 关闭主程序，确认没有下载/整理任务；
2. 把备份的 `library.sqlite3` 覆盖回正式路径；
3. 直接删除目标旁的 `-wal`、`-shm`；
4. 再 inspect 和 verify。

### 问题

- “任务没有运行”不等于**所有进程/连接/后台线程已经关闭**，也没有执行可验证的进程/实例锁检查。当前步骤可能让用户在仍有连接时覆盖文件组。
- 文件组替换应是一个协议，而不是三个独立动作：需要先验证备份，再隔离当前主库和关联文件，再放入匹配的备份组；最后才处理已隔离的旧 WAL/SHM。原文没有隔离当前组，也没有验证还原后的组。
- 最后的 `inspect` 只检查目标库自身，`verify <备份目录>` 只检查备份自身；两者都不证明“目标目录当前文件组等于已验证备份”。
- 模式虽在前面提过，但恢复步骤没有明确 online/file_group 的分支。恢复前只复制主文件再处理旧 WAL 的顺序，不能作为两种模式的共同安全操作。

这不是宣称用户数据已经损坏；而是现有说明**没有给出足以证明安全恢复的操作门槛**。一旦用户在外置卷未恢复、程序残留连接或目标有活动 WAL 时照做，就可能破坏未保存的当前记录。

### 必须怎样修

先暂停这段手工恢复指引。新协议必须满足：

1. 关闭并核实应用及所有辅助进程已退出；不通过“任务状态”推断进程状态。
2. `verify_backup` 四项均通过，记录备份模式与成员清单。
3. 在副本上准备恢复目录并验证；当前文件组另移到同卷独立保留目录，不覆盖、不删业务原件。
4. 按模式恢复完整匹配组；只操作明确命名的、已隔离的旧文件，不对正在使用的目标目录执行通配删除。
5. 重开恢复目录，`compare_library` 或等价的逻辑对照证明恢复结果与备份一致；保留失败回退。
6. 全程只在 `.runtime` 合成副本验证。完成前不得出现“现在可以照此恢复真实库”的措辞。

可以暂不实现自动 restore CLI；**正确的人工协议优于未经验证的自动化**。

## WR-02 / P2：扫描 CLI 不加载外部根映射，存在文件被误报为根不可用

**位置**：[library_maintenance.py](../../../core/library_maintenance.py) `main:1980–2006`；对照 [output_locations.py](../../../core/output_locations.py) `OutputLocations.load:83–103` 与 [collection_desktop.py](../../../core/collection_desktop.py) `DesktopSession._io_loop:187–204`。

CLI 使用 `PathResolver(AppPaths(root))`，随后直接 `scan_library_artifacts`。正常桌面启动则先 `self.locations.load()`，把 `config/output_roots_v1.json` 的外部根加入同一 resolver。扫描入口漏掉了这一步。

### 独立反例

- 合成合法 `output_roots_v1.json`，映射 `folder_test` 到一个存在的临时目录；
- artifact 为 `kind=external, root_id=folder_test, relative_path=p.jpg`，文件存在，记录摘要正确；
- `main(["--json","scan",db,"--root",project])` 返回 **exit 2**，行 verdict 为 **root_unavailable**，而不是 match。

### 影响

用户已选择外置图库后，运行手册中的扫描命令会把可读取的现存文件当成不可访问。虽然它是 fail-closed、不会删文件，但会让巡检失去可信性，并可能误导后续人工处理。

### 修法与判据

只读加载同一映射文件，**不能**新建 root 或猜测外部目录；未知 root 继续 `root_unavailable`。映射非法、路径冲突、链接、根不存在必须产生明确错误/不可用结论，不自动重建。

- 实际 CLI：合法外部根文件 → match；
- 未注册 id → root_unavailable；
- 合法映射但目录掉线 → root_unavailable，不创建目录；
- 非法 JSON、绝对/链接路径违规 → 结构化拒绝，不能退回 project root 检查另一位置；
- 混合 project/external 行分别核验。

## WR-03 / P2：全库巡检停在单页，末页又把本页说成“全部”

**位置**：[library_maintenance.py](../../../core/library_maintenance.py) `_parser:1613–1625`、`_scan_lines:1793–1809`、`_scan_outcome:1827–1835`、`main:1993–2042`；[library_audit.py](../../../core/library_audit.py) `scan_library_artifacts:320–334`。

引擎的 `coverage="complete"` 是**这一页读完**，CLI 把它提升成“覆盖全部 N 行”，且只看本次调用的 findings/unknown。它没有自动跟随 `next_cursor`，也没有跨页累计发现。

### 独立反例：201 行，全部文件匹配

| 调用 | 实测 |
| --- | --- |
| 第一次 `scan --limit 200` | `library_rows=201`、`matched=200`、`coverage=page`、`next_cursor=[...,a-0199]`、**exit 5** |
| 用游标再调用一次 | 只有最后一行 match，**exit 0**；文本是“本次核验覆盖全部 1 行，全部与记录一致。” |

这不是“所有 201 行都通过了”，而是只完成一页。若中间页有 missing/changed，后续末页仍可能以 0 结束，无法向只取最终退出码的自动化保留历史发现。runbook 只给了一条 `scan ... --root .` 命令，没有续跑循环或汇总步骤。

### 必须怎样修

在 S0 先冻结两个不同接口，不能混用：

- **单页接口**：显式命名为 page/slice，`complete` 文案必须限定“本页”；支持 `--after` 的显式续跑页应默认 exit 5，除非从同一全程报告恢复并明确证明到达 EOF。
- **全程接口**：默认按 `next_cursor` 推进到真正 EOF，累计所有页的 stats/findings；每页仍保持短事务与字节/时间预算。只有首行 cursor=None、无预算/取消/账本错误、到达 EOF 且累计没有未处理异常，才可输出全库 `coverage=complete`。

预算耗尽应返回可恢复进度，而不是说全库完成；未覆盖行、坏行、异常、unknown、excluded 必须分别计数。`--limit` 应解释成页大小/突发上限，不是全库数量上限。

## WR-04 / P2：备份晚期失败与坏清单未进入统一错误协议

**位置**：[library_maintenance.py](../../../core/library_maintenance.py) `backup_library:689–844`、`_load_manifest:1051–1070`、`_member_entries:1095–1116`、`verify_backup:1173–1198`、`main:2053–2100`。

### 已证实的两个边界

1. 将 `_write_manifest` 注入 `OSError(28)`：`backup_library` 的清理 try 只覆盖实际复制阶段（689–712），清单生成在 777–844。失败后 `backups/late/` 留下数据库但没有 manifest，也没有显式未完成标记，异常原样上抛。函数 docstring 却承诺“任何失败移除本操作目录”。
2. 将 `members[0].bytes` 改成 `"not-a-number"`：`verify_backup` 在 `int(item.get("bytes") or 0)` 抛 `ValueError`，`--json verify` 没有结构化输出，直接异常退出。

### 影响

前者让备份失败诊断、重试和现场清理依赖未约定的残留；后者让外部工具把不可读清单当成程序崩溃。它们目前都不会造成 `ok=True`，所以不能夸大为“假备份通过”，但确实是 P2 级协议缺口。

### 修法与判据

- 对 v2 清单做**完整、有限、类型严格**的结构验证：member name、bytes、sha256、sealed_members、fingerprint/version/digest/rows/tables、copy_name、mode、finalizer 全部校验，不把 `or 0` 当合法缺省。
- 清单不可信时不得靠其内容选择“尝试读取另一个数据库”；`ok=False` 或 typed error，绝不能默认为已验证。
- 复制之后、清单原子封存之前的所有异常统一处理。清理失败必须保留可诊断现场，并将原始故障与清理故障分别报告；不能因递归清理再删除非本操作创建的文件。
- 只验证并封存确实属于本备份的 SQLite 文件组；`copy_name` 必须属于 sealed members，`-shm` 不能被当成证明数据库逻辑内容的载体。
- 故障注入覆盖：目标创建失败、复制失败、fsync/rename 失败、写完临时 manifest 后失败、清理失败、不可读 JSON、错类型、缺字段、重复成员、链接成员、未知版本、未知指纹版本。

## WR-05 / P2：核验列表只显示前 50 行，接口支持游标但界面不翻页

**位置**：[delivery_repository.py](../../../core/delivery_repository.py) `list_verification_pages:661–771`；[collection_controller.py](../../../gui/collection_controller.py) `_refresh_verification:745–765`、`_render_verification:817–829`；[verification_panel.py](../../../gui/verification_panel.py) `_build:307–365`。

仓储返回 `next_cursor` 和 `stats.total`，控制器却固定请求 `limit=50`，没有保存/传递 after，只显示“还有更多未列出”。面板没有“下一页/下一批”动作。

这不是只有设计稿未完成的小瑕疵：交付记录中的真实库有 **897 suspended** 行。若后续出现核验停留，前 50 条以外的页无法从用户面板查看；用户也无法判断“未列出”的数量。维护记录虽登记“不分页”，但 `maintain scan` 又不具备 WR-02/03 的全程能力，所以“走 CLI 即可”不能补足它。

### 修法与判据

- 按 cursor 显示**一页**（例如每批 50 行）并提供明确“下一批”按钮；上一批可选，若实现则维护 cursor 栈。
- 保留读取 epoch/batch；换批/失败时游标与可见行一起失效。
- 批量恢复只对**当前已展示、用户明确选择的有限行**执行，不在点击“批量核验”时隐式走遍全库。
- 展示 `本页 N / 范围内 total`，而不是仅“还有更多”。
- 至少 51 行合成记录验证第一页→第二页无重复/遗漏，前后响应倒序、第二页切换任务时不可污染新批次。

## WR-06 / P2：作废只清缓存，旧行和恢复身份没有同步失效

**位置**：[collection_controller.py](../../../gui/collection_controller.py) `_invalidate_verification:689–714`、`_row_in_scope:722–730`、`_verify_step:889–899`；[verification_panel.py](../../../gui/verification_panel.py) `row_key:184–192`；[delivery_recovery.py](../../../core/delivery_recovery.py) `recover_verification_page:361–506`、writer gate `851–936`。

### 已复现

1. 加载 A 批次一行，切换到 B：`_verify_rows` 已清空，但 view 最后一次 `set_verification_rows` 仍含 A 的旧行，直到 B 查询成功。
2. 查询失败后旧行继续可见，且没有“上次结果/读取失败”标记。
3. `row_key` 含 `delivery_id`，但 `_verify_step` 只传 `batch_id,illust_id,page_index`，后端自行重读当前 delivery。UI 展示的“旧 delivery 已完成/被取代”不会直接传给后端提前拒绝，可能先做一次无效文件哈希。

后端 CAS 仍比较 `previous_delivery_id` 和 `previous_state`，所以**没有证明旧行能误写另一页**；问题是展示和 I/O 的实效性及用户对当前操作范围的理解。

### 修法与判据

作废时同步：

- 清内部 rows/cursor；
- 清/替换可见列表为“正在读取”或“读取失败，以下为过期内容且不可操作”；
- 禁止旧行动作并清选择；
- 保持已在飞的后端命令完成，其结果只进入原批次的摘要，不渲染到新批次。

恢复命令增加**不可变 delivery identity**（至少 expected_delivery_id，必要时 generation），向后兼容只适用于已有内部调用，不接受 UI 缺省。writer 在观察前先快速拒绝不匹配的身份，再执行既有观察与 CAS；不得用 UI 校验替代后端检查。

## WR-07 / P2：核验查询错误没有形成明确的页面状态

**位置**：[collection_controller.py](../../../gui/collection_controller.py) `_poll:202–247`、`_refresh_verification:745–765`、`_verification_failed:952–969`。

`verification_pages` 的 Future 抛错时，`key.startswith('verify:')` 为 false，走通用 `_show_error`；只有真实恢复命令才调用 `_verification_failed`。面板没有对应的 `loading/ready/stale/error` 状态。

摘要与列表是两个独立 Future，各自只检查 UI epoch/batch，并不来自同一个仓储读取 epoch。两者都成功也可能代表不同观察时点。当前控制器把“summary 到达”当成可以立即渲染的事实，列表失败时仍可保留/显示不一致画面。

### 修法与判据

- 列表与摘要请求以同一 UI read epoch 绑定，理想上提供一个同快照的查询端口；若不合并，至少标明各自读取时点，不能把拼接当原子快照。
- 页面状态至少区分 loading/ready/error；失败必须展示在面板，不只依赖全局状态栏。
- 读取失败后禁止旧行的恢复按钮，保留有限重试，不自动形成高频循环。
- summary 先失败、pages 先失败、两者错序、同 job 新 batch、close 后回调全部纳入反例。

## WG-01 / P3：当前导航与机器指针仍指向旧阶段

- [项目 README.md](../../../README.md) 第 3 行仍写 RFA 审计、`f7c389b`、日常 `5465e0c`；实际日常已是 `e4eb6ce`。
- [IMPLEMENTATION.md](../../optimization/IMPLEMENTATION.md) 第 3 行仍说 RFB 待修，后文 4M 却宣布全部关闭。
- [契约 v5](../../optimization/2026-11-14/U1A_W5_W6_CONTRACT_V5.md) 第 3 行使用 `../audits/2026-11-14-u1a-w5-w6/README.md`，少了 `../../`，在日常树中确实打不开。
- [TASK_PLAN.json](../../../TASK_PLAN.json) 第 349–365 行已标记 daily 快进完成，却仍以“快进 daily”作下一步；同一块 `deployed:false` 与 `runtime_checkout_note` 的日常切换描述不一致。

不要修改历史审计原文来掩盖；只修当前导航和机器指针，并在当前条目中标注本轮新发现尚未修。

## WG-02 / P3：审计原件链接缺失，验收矩阵不易从仓库复现

- [旧审计 README](../2026-11-14-u1a-w5-w6/README.md) 第 64 行链接 `evidence/test_w5w6_recheck.py`，该文件不在修复分支证据目录。
- 原件实际存在于原审计 worktree `.runtime/jpgpng_u1a_w5w6_audit_20261114_01/test_w5w6_recheck.py`，并未丢失；AST 比对证实引入后的 11 个函数/类定义与原件一致，只有 ROOT 推导按契约调整。
- 最终 `verify_matrix.ps1` 只在 `.runtime/jpgpng_u1a_w5w6_fix_20261114_01/`，且带本机绝对路径；从公开仓库/新 worktree 难以原样复现。

建议将原审计原件与 SHA-256 登记补齐，并提交无硬编码工作区路径的测试矩阵说明/脚本。不要把 XML 的大量临时路径、数据库或图库复制进 Git。

## WG-03 / P3：worktree/分支缺少活跃与历史边界

盘点时有 **75 个 worktree、58 个本地分支**；**8 个 worktree 有未提交项**，其中 `verify_r1` 等有真实源码/测试修改。不能只按 `branch --merged` 删除。

### 整理要求

1. 将本次 `worktrees_inventory.json` 转为带 role 的登记：daily / active-fix / current-audit / frozen-checkpoint / disposable-measure / unpreserved-draft。
2. 所有 dirty tree 先保存 diff、未跟踪文件清单与内容摘要；不得用 `clean`、`reset --hard` 或目录删除“整理”。
3. 已合并的分支可留引用而清理多余 checkout，但先核对 worktree 是否承载唯一证据、未跟踪报告或本机映射；有则先登记归档。
4. 不移动检查点标签，不删除历史提交，不删业务库/备份/原图；一次性大批清理不是本批前置条件。
5. 后续每批结束更新登记，不再默认永久保留每个 lane worktree。

## WG-04 / P3：用户文档没有同步新维护能力及适用边界

工作区 [workspace.json](../../../../../../workspace.json) 已登记 `maintain.cmd`，但项目 README 的入口表只有启动/旧版/整理 CLI，[使用说明.md](../../../使用说明.md) 也没有维护命令入口。用户很难从常规文档发现扫描/备份，或正确理解它与“核验”按钮的差别。

补充时应说明：scan/inspect 只读但 SQLite 可能刷新 SHM；backup 只写新目标；verify 的密封与源对照不同；恢复不是自动动作；扫描不是文件恢复授权，也不自动补下载。修复 WR-02/03 前不得宣传为随意外置图库的一键全库巡检。
