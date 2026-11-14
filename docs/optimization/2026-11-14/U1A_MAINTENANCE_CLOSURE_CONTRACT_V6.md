# U1-A 维护与核验闭环契约 v6（开发前冻结稿）

基线：`e4eb6ce`。输入：[独立审计](../../audits/2026-11-14-u1a-w5w6-proof-independent/README.md)。本契约只处理 WR/WG，不重开已关闭的 UA/RF/RFA/RFB/WC 编号，不授权生产迁移、真实库恢复、归档释放或联网。

## 1. 验收的含义

“单页通过”“全套测试通过”“可以替换日常入口”是三个不同事实。最终候选必须分别记录：

1. 引擎/状态机行为；
2. CLI 与 GUI 用户路径；
3. 异常、分页、外部根、进程关闭的边界；
4. 真实库只允许只读预检与已核验备份，任何写回/恢复都另批。

不得把“没有 P1”写成“P2 为零”，也不得把历史 `deployed:false` 与当前 daily 实际 HEAD 混写。

## 2. 备份与恢复文档（S1）

### 2.1 清单与完成协议

- v2 清单结构必须先验证再使用，字段类型严格；manifest 不存在或不可信时 `ok=False`，不能将未检查解释为成功。
- `copy_name` 必须属于 sealed 数据库成员；`-shm` 可以复制/封存供参考，但不得被当成逻辑数据库来源。online backup 应是自包含主库。
- 本操作创建的目标目录用唯一路径持有。复制后、清单封存前所有异常都必须进入统一收尾；原始失败与清理失败分别报告。
- 成功标记是最后的原子 `manifest.json`。临时 manifest 不得被误当完成；清理不成功时保留现场，并返回 `backup_cleanup_failed`（或等价明确类型），不能宣称已清理。
- 禁止递归跟随链接；只清理已验证属于本操作的文件。任何额外/不可识别文件都不擅自删除。

### 2.2 错误协议

| 情形 | 结果 |
| --- | --- |
| 参数/路径不合法 | typed refusal / exit 4 |
| 清单缺失、损坏、未知版本、成员缺失 | typed unreadable / exit 3 |
| 实际字节、逻辑内容、健康、兼容性不一致 | 分项 `mismatch/not_checked`、`ok=False` / exit 2 |
| 备份生成失败 | 非零、明确失败目录处理结果；不留“完成”标志 |
| JSON 输出 | 与文本同一事实、同一退出码，不吐 Python traceback |

### 2.3 人工恢复协议

在安全协议写好并合成演练前，现有 runbook 的“覆盖+删除 WAL/SHM”标为**不可照做**。协议只描述人工步骤，不自动恢复：

1. 关闭应用及辅助进程，并验证没有剩余进程/连接；
2. 先验证备份，确认模式与成员；
3. 在副本准备恢复文件组并验证；
4. 将当前文件组移入同卷独立保留目录，不删除、不覆盖；
5. 按备份模式放入匹配的组，不混用新主库和旧 WAL；
6. 重开恢复副本，检查兼容性、健康与逻辑对照；
7. 失败可退回原文件组，报告保留。

只有在合成副本完成 WAL 残留、文件组模式、外部卷不可用、连接未关、进程中断与重开验证后，才能给出真实库的操作卡；本轮不执行真实库恢复。

## 3. 扫描与输出根（S2）

### 3.1 根映射

CLI 必须从项目根的 `config/output_roots_v1.json` 只读加载与桌面一致的映射。禁止扫描目录猜 root、自动 mkdir、接受未知 id 时回退 project root。映射文件存在但不可解析、版本未知或包含非法根时，整个命令以 `mapping_unreadable` / exit 3 停止；不能在部分映射下给出全库结论。

`--root` 只指定项目根，不是一个任意的“替换所有记录位置”的根。若需要显式替代映射，另定义有边界、可审计的参数，不能改变现有参数含义。

### 3.2 单页与全程

引擎 `coverage` 保留“本页”语义。CLI 增加独立的**全程聚合层**，不改变 `scan_library_artifacts` 的逐页 API：

```
scan_library_report(source, *, after=None, limit=200, budget, cancel=None) -> {
  coverage: complete|partial|cancelled|empty|error,
  scope: full_walk|continuation,
  pages_examined, library_rows, stats, findings, unknown_reasons,
  next_cursor, resume: {state, complete_prefix, exit_code}|null,
  exit_code
}
```

- 默认 `maintain scan` 走全程接口，显式 page 子模式才走单页接口；`--limit` 是页大小/单轮突发上限，不是全库总量。
- 每页只读短事务；每页观察后释放数据库连接，不将整个扫描包进长事务。
- 首行 `cursor=None`，只有到 EOF、未被预算/取消/错误停止，才是全程 `complete`。`--after` 的续跑页不等于全库完成，即使已经到 EOF 也只能标 `scope=continuation`、`coverage=partial`。若未来允许断点续跑后返回 0，必须先在报告中验证同一 run/schema/根映射/策略的完整前缀链，不能从裸 cursor 推断。
- 聚合保留全部 verdict、unknown、排除、发现、页数、读取字节、时间和游标。中间页确定异常必须进入最终退出码，不能末页覆盖。
- 总文件/总字节/总时间预算独立于单页预算；单文件超预算被标 unknown 后游标可以推进，不能永远卡住同一行。整个扫描不能因缺少 `cancel` 而无法停止。
- 报告落盘若实现，必须临时文件+原子替换、限制单页行数/总输出，不存完整路径或凭据；不强制本批引入持久数据库。
- 全库报告只允许从**一次已证明完整的连续遍历**声称覆盖完成；跨多次启动的累计可给“已观察集合”，不能冒充当时全库快照。

### 3.3 退出码

保留 0/2/3/4/5 的现有大类：

- 0：全程覆盖完成，且确定异常为 0；legacy unknown 只有显式 `--allow-unknown` 才放行；
- 2：任一被访问页有 missing/changed/unreadable/root_unavailable/changed_during_check；
- 3：账本/映射/清单不可读或无法安全继续；
- 4：参数拒绝；
- 5：覆盖未完成、取消、预算停止、存在不可核验行或只完成一页。

## 4. 核验页与恢复身份（S3）

### 4.1 读取作用域

每个列表请求携带：

```
(job/batch identity, UI epoch, states, after, limit)
```

返回 `rows,next_cursor,truncated,stats.total,observed_at/read_revision`。summary 与 pages 尽量由同一短读事务产生；若保留两个 Future，必须标明非原子读取，不声称同一时刻快照。

页面至少区分 `loading / ready / error`，并有明确“本页 N / 范围内 total”。下一批仅推进一个 cursor，不自动遍历/处理全库。

### 4.2 失效协议

切换 job、batch、刷新、离开页面或关闭时，作废同时作用于：

- rows/cache/cursor；
- 可见列表、选中项和按钮；
- 尚未派发的批量队列；
- 新查询的 epoch 与错误/加载提示。

已在飞的后端恢复命令允许完成原命令，但结果不进入新批次；不要靠 `Future.cancel()` 推断已经停止。

查询失败后，旧行若保留只能以“过期、不可操作”显示；优先清成错误页。不能继续显示为当前可核验行。

### 4.3 恢复身份与后端安全门

UI 的 `row_key` 已含 `delivery_id`，但当前命令未携带它。新端口必须增加 `expected_delivery_id`（和可选 generation）；UI 必填，旧内部调用显式适配。这个值来自列表的“用户以为要操作的行”，与后端读到的 `previous_delivery_id` 是两个角色；不得把它写入 payload 后当作另一个 previous id。

后端在文件观察之前读取 `batch_item_pages` 并比对 delivery identity，不匹配返回 `state_changed`，不做哈希。匹配后继续使用既有观察、policy/root revision、generation、previous_delivery_id/state、intent/CAS；这些一个也不能删。`state_changed` 走 `_refuse_recovery`，`health=None`，不得为了生成 draft 去观察文件。

## 5. 并行归属与集成

| 包 | 独占产品文件 | 测试 |
| --- | --- | --- |
| S1 备份/恢复文档 | `core/library_maintenance.py`、`maintain.py` 必要入口 | `tests/test_u1a_library_maintenance.py`、新的恢复协议测试 |
| S2 扫描 | `core/library_audit.py`、新的 `core/library_scan.py`（推荐） | `tests/test_u1a_library_audit.py`、新聚合测试 |
| S3 UI/身份 | `gui/collection_controller.py`、`gui/verification_panel.py`、`core/delivery_recovery.py`、必要桌面端口 | `tests/test_collection_verification_panel.py`、新恢复身份测试 |
| 主线程 | CLI 接线、runbook、导航、矩阵、现场登记、集成 | `tests/test_maintain_scan_cli.py`、文档/链接验证 |

共享文件不并行：`library_maintenance.py` 的 CLI dispatch 归主线程，S2 不直接修改它；`delivery_repository.py` 若需快照/恢复身份扩展，先由主线程冻结并指定唯一负责人。

### 5.1 旧证据与异常输出的额外约束

- 备份损坏清单、源路径不存在、文件打开失败等预期外部输入错误必须进入公开错误协议；不能用“任何 OSError 都是程序员 bug”当作放行 traceback 的理由，也不能用 broad catch 隐藏编程错误。
- `test_u1a_w5w6_boundaries.py` 的 AST 与原审计原件一致（ROOT 除外）只能证明原反例保留，不能替代原件随审计包留存。
- 新增测试命名必须表达期望行为；“test 当前错误行为仍然存在”的调查 probe 不得进入正式绿色回归。

## 6. 验收矩阵

1. 现有所有反例保持通过，不加 xfail/skip，不通过放宽时限掩盖竞态。
2. 新反例先红后绿：外部根、201 行跨页、跨页异常保留、清单故障、51 行 UI 翻页、读取失败、旧行、delivery 变化先拒绝。
3. `core/gui/utils` 编译；受影响调用方 GitNexus impact 与全量 detect-changes。
4. 非 Tk 正确排除 `test_collection_presentation.py`、`test_source_presentation.py`，两个文件逐进程隔离；核验面板属于主体，不重复当 Tk 隔离计数。
5. 合成文件组/外部根/预算/取消/进程残留演练；不读取或改写真实业务数据。
6. 只读 fork 对照本契约和 `git diff` 逐项审查；失败回原 lane 修复。
7. 当前文档只保留一个接续入口，机器计划状态与 daily HEAD 一致，历史记录不改写。
