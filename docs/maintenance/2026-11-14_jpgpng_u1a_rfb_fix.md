# U1-A RFB 轮修复记录（JPGPNG-U1A-RFB-FIX-20261114-01）

日期：2026-11-14。起点是 [RFB 复审](../audits/2026-11-14-u1a-rfa-recheck/README.md)：在 `48a6968`（审计 `de50f80`）上确认 6 项问题（1 P1、5 P2，RFB-01..06）。本轮把它们全部按行为关闭。

## 1. 基线与范围

| 项 | 值 |
| --- | --- |
| 审计基线 | `de50f80`（被审产品 `48a6968`，被审文档 `f7c389b`） |
| 修复分支 | `fix/a1-u1a-rfb-20261114` |
| 集成工作树 | `.runtime/jpgpng_u1a_rfb_fix_20261114_01/integration_worktree` |
| 契约 | [U1A_STATE_CONTRACT_V4.md](../optimization/2026-11-14/U1A_STATE_CONTRACT_V4.md)（含 v4.1 修订） |
| 解释器 | `D:\Miniconda3\envs\JuiLiu\python.exe`（CPython 3.12.14），`PYTHONNOUSERSITE=1` |

全部验收使用合成 SQLite 与合成 PNG：没有真实账号、图库、网络、迁移、部署或删除。库 schema 仍是 `"1"`，没有新增表/列/索引/依赖。

## 2. 修了什么

| 编号 | 原问题 | 修法 | 证据 |
| --- | --- | --- | --- |
| RFB-01 P1 | 跨续跑的正面健康缓存直接把"原地等长异图"当成完整已有文件；trusted 复用不经过发布/确认门 | 三层证据（提示 / 本次尝试 / 已接受 receipt）；**提交前严格确认 pass**：最后一片持久化之后、writer 命令之前，对每个来自提示的正面复用候选重新完整观察（identity+size+sha256），CHANGED → `needs_verification`（不重下载） | `test_cached_hash_cannot_approve_an_in_place_equal_size_edit[False/True]`（基线 1 次观察、两页都复用；现在 2 次观察、1 completed + 1 needs_verification） |
| RFB-02 P2 | 进度截断或持久化失败后，反复"继续"仍停在同一切片上限 | `progress_usable`（写入成功 + 候选数 ≤ `MAX_PROGRESS_RECORDS` + 未截断）；为假时关闭切片上限，一次受理内观察到覆盖并提交；末项边界不再多抛一轮；`page_slice_limit` 带 `new/persisted/remaining` 且 `new>0` | `test_missing_progress_cannot_turn_a_large_page_into_an_endless_retry[cache_cap/progress_write]` 均为一次受理 committed；6 页/2 片从"第 4 次提交"变成"第 3 次提交" |
| RFB-03 P2 | 清理扫描把"检查预算耗尽"当成队尾并回绕，后面的合法记录饿死 | 每 tier 的 `TierScan(facts,next_cursor,eof,budget_exhausted,examined,offered)`；空结果不再兼任 EOF；只有 `eof` 才回绕，预算耗尽保留游标；公开 `stats` 也能区分"空"与"队尾" | `[1]`：游标 `[6,11,13,0,6]`、尾项被访问；`[200]`：`[1001,2001,0,1001,2001]`、尾项被访问（基线 visits 空、游标固定） |
| RFB-04 P2 | 窗口内的中间取代行携带 handover 指向更早 completed 时，解析漏边、公开恢复拒绝 | 节点集合 = {anchor} ∪ 窗口行 ∪ 事件目标；对集合内需要事件的节点批量读取并按权威顺序展开；坏目标拒绝；节点/事件/查询预算 | 探针 `test_superseded_intermediate_event...` 现在 `reason=supersede_chain`、`queries=4`、`nodes=2`、`events=1`，公开恢复 `reused` 并链接 C 的 artifact |
| RFB-05 P2 | 账本不可读被吞成普通 `recovery_needed`，控制面无法区分 | 四分类：CAS 输家报赢家；可读但补偿写被拒 → `recovery_needed` 且不盲写；**读不通 → 抛 `_StorageFault`**；文件不可观察 → 保持可恢复。私有分类器与公开 `recover_local` 最外层都接好 | `test_unreadable_ledger_is_not_reported_as_a_recovery_classification`、`test_recover_local_surfaces_an_unreadable_publication_ledger` 均抛 `_StorageFault` 且行仍 `prepared`、字节保留 |
| RFB-06 P2 | 单文件可静默突破声明的字节预算，末文件后直接提交 | `max_bytes/max_seconds` 改为每页硬限额（默认 8 GiB / 300 秒）；开文件前用行内 size 预判；剩余额度裁进 `ObservationBudget`；末文件后再检查；单文件超限 typed 拒绝且 cursor 不动 | `test_a_single_file_cannot_silently_exceed_the_declared_slice_byte_budget`：`PageBudget(max_bytes=1)` + 70 字节文件 → `page_byte_budget`、checkpoint None |

## 3. 契约演进

v4 冻结五份协议（观察与证据、进度与容量、清理扫描、恢复错误、预算、后继），并更正 v3 的三处错误陈述（stat 不能当复用证据、`recovery_local` 的异常矛盾、单文件预算没有兑现）。v4.1 追加一条：**不把取消标志传进观察器或观察 helper**——已受理的观察必须跑到判决，暂停只落在文件之间与切片之间。

## 4. 代码与测试改动

产品 5 个文件：`core/library_repository.py`（两段式计划器、进度可用性、每页预算、确认 pass）、`core/observation_snapshot.py`（`PageBudget` 每页语义、`PreflightCancelled.details`）、`core/page_delivery.py`（`_classify_after_race`/`recover_local`/`_sweep_cleanup`）、`core/delivery_repository.py`（`TierScan`、双游标扫描、公开 stats）、`core/delivery_lineage.py`（节点集合 + 批量事件 + 预算）。

测试：`tests/test_u1a_rfb_boundaries.py`（审计 12 条原样）、`test_u1a_rfb_observation.py`（11）、`test_u1a_rfb_delivery.py`（9）、`test_u1a_rfb_lineage.py`（13）、`test_u1a_rfb_scenarios.py`（17），另有 `test_u1a_rfa_observation.py`（计数更新）与 `test_u1a_rfa_scenarios.py`（9 条期望改到两阶段口径）。

## 5. 验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| RFB 12 条探针（原样导入） | **12/12 通过**（修复前 10 红 / 2 绿） | `red01/red.xml`、`full01/all.xml` |
| 非 Tk 全量（最终树） | **1,897 项：1,893 通过、4 跳过、0 失败、0 错误** | `full01/full2.xml` |
| Tk 两文件 | 逐条隔离 **9/9** | `full01/tmp-tk/*` |
| 旧反例 | RFA 12 + RF 15 + UA 14 全绿 | 含于全量 |
| 只读审计 | 见 §7 | — |

4 个跳过仍是三处真实符号链接权限限制 + 一个 W 阶段登记。

### 5.1 关键数字

* 确认 pass 只作用于**跨调用缓存命中**的候选：单次受理内完成的页面，所有候选都在本次尝试观察过，不需要确认（3/33 页仍是 3/33 次观察）。
* 33 页、第一次 `max_files=32/max_slices=1` 停在切片上限、resume 后提交：第 2 次 = **1 次新观察 + 32 次确认 = 33 次**（基线是 1 次）；复用链接 33、checkpoint 前进。
* 256 页（正好 8×32）：**一次受理 256 次观察**即提交；257 页：第 1 次 256 次 + `page_slice_limit`（`remaining=1`），第 2 次 1 新 + 256 确认。
* 6 页 / 1 文件/片 / 2 片每次：基线第 4 次提交（第 4 次 0 观察）；现在**第 3 次提交**，总观察 10（前两次各 2 次，第 3 次 4 确认 + 2 新）；两次 `page_slice_limit` 分别带 `{new:1,persisted:1,remaining:4}` 与 `{new:1,persisted:1,remaining:2}`。
* cache_cap 与 progress_write 两种降级：基线 8 次全 `page_slice_limit`、16 次观察、不提交；现在**第 1 次受理即提交、6 次观察**。
* 清理：limit=1 游标 `[6,11,13,0,6]`；limit=200 游标 `[1001,2001,0,1001,2001]`；两者都访问到尾部合法项。
* 后继：事件路径 2 条语句；无事件路径 3 条语句（新增一条锚点事件批量查询）、1 次窗口扫描、节点 = min(n,64)、64 行 `budget_exhausted`；A→B→C 链 4 条语句，A 的 `reason=supersede_chain`（事件属于中间节点 B，B 自己 `reason=event`），`queries=4` 正好等于上限。
* 不退化：accept 3×10 = 311、2,000 页 = 18,025；claim 30 = 61、2,000 页 = 8,021。

## 6. 已知边界（记录，不在本轮修）

1. 确认 pass 与 writer 事务之间仍有毫秒级窗口；不承诺消除。
2. 每页内容（含确认）超过 8 GiB / 300 秒会明确拒绝（`page_byte_budget`/`page_time_budget`）；该上限是部署级代码常量，配置入口留给 W5。
3. 跨调用复用的文件哈希次数由 1 变 2（设计代价）。
4. 无事件路径语句数由 2 变 3；`MAX_LINEAGE_QUERIES=4` 正好被三层链用满，再增加查询会落到预算拒绝。
5. `progress_usable` 为假时一次受理可能较长（受每页预算约束）。
6. 已登记遗留不变：`finish_source` 结果查询协议、`AdmissionTicket` 未实例化、意图投影/索引、carry 长事务、claim 语句数、真实迁移/备份/外置卷、W5 UI、completed 重核验、旧 `record_manager` 健康门。

## 7. 只读审计

独立只读审计（对照 v4/v4.1 逐条核 diff + 定向 149 项）结论：**RFB-01..06 全部按行为关闭，无 P1**；测试诚实性通过（探针逐字一致、T 的两轮改动是加强而不是削弱、无 xfail/skip/静默吞异常、无新表/列/依赖）。

审计提出 2 项 P2 与 4 项 P3：

| 编号 | 问题 | 处理 |
| --- | --- | --- |
| P2-1 | 每页 512 MiB 是硬编码上限：33×20 MB 的作品永远受理不了 | **已修**（`aede9e5`）：默认上限提高到 8 GiB（单页内容上限，不是突发上限），用"正好 N / N+1 字节"的测试钉住；"每页上限仍是部署级代码常量、配置入口留给 W5"记录为边界 |
| P2-2 | 批量 handover 事件被全局 LIMIT 64 截断时返回 `none` 而不是 `budget_exhausted` | **已修**（`d8cdcfd`）：事件查询取 `MAX_EVENTS+1` 行，读到第 65 条即返回 `budget_exhausted`（`event_cap`），不返回基于不完整集合的候选；补了 >64 与 ≤64 两条测试 |
| P3-1 | 200 条事件窗口造成的进度丢失没有计入 `truncated` | **已修**（`aede9e5`）：事件窗口打满同样置 `truncated`，触发完成式尝试 |
| P3-2 | `tests/test_u1a_rfa_scenarios.py` 一处断言从精确集合改为 `len(seen) <= max_slices` | 记录；同一性质由 S1 的 `test_expired_progress_is_observed_again`（`calls == recorded`）精确覆盖 |
| P3-3 | `_known_artifact_size` 对坏行返回 0，预判退化为观察器上限兜底 | 记录（fail-safe，诊断不清） |
| P3-4 | `MAX_LINEAGE_QUERIES=4` 被三层链正好用满，零余量 | 记录；再加查询会落到预算拒绝 |

审计另确认（不是问题）：确认 pass 不会把同一页读两遍（每页每次调用每个 artifact 最多读一次，P2-1 是单页内容上限而不是双倍计费）；256 页在第 8 片收口即提交；清理在持续负载下每轮都能取到失败行；取消标志不传进观察器/观察 helper。

## 8. 复现

```powershell
$env:PYTHONNOUSERSITE='1'
& 'D:/Miniconda3/envs/JuiLiu/python.exe' -s -B -X utf8 -m pytest `
  tests/test_u1a_rfb_boundaries.py tests/test_u1a_rfb_observation.py `
  tests/test_u1a_rfb_delivery.py tests/test_u1a_rfb_lineage.py `
  tests/test_u1a_rfb_scenarios.py -q -p no:cacheprovider --basetemp=<空目录>
```

测量：`PIXIVKEEP_MEASURE_REPO=<集成树>` + `.runtime/jpgpng_u1a_rfb_fix_20261114_01/probe/measure_rfb.py --label postfix`（约 35 s）。
