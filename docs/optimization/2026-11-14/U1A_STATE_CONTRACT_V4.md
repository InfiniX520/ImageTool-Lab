# U1-A（RFB 轮）状态契约 v4

日期：2026-11-14。前序：[v3](U1A_STATE_CONTRACT_V3.md) 保持原文，不重写。本文件**修订** v3 的三处错误/矛盾，并冻结本轮四份协议。审计：[2026-11-14-u1a-rfa-recheck](../../audits/2026-11-14-u1a-rfa-recheck/FINDINGS.md)（RFB-01..06）。

## 0. 基线与范围

| 项 | 值 |
| --- | --- |
| 审计基线 | `de50f80`（被审产品 `48a6968`，被审文档 `f7c389b`） |
| 集成工作树 | `.runtime/jpgpng_u1a_rfb_fix_20261114_01/integration_worktree` |
| 分支 | `fix/a1-u1a-rfb-20261114` |
| 红基线 | `.runtime/jpgpng_u1a_rfb_fix_20261114_01/red01/`（12 项：10 红 / 2 绿） |

不新增表/列/索引，不改 `_SCHEMA_VERSION = "1"`，不加依赖，不接 UI，不碰真实图库/账号/网络。

**冻结的名字**（审计反例直接 import，不能改名/删除）：`PageBudget`（`max_files/max_bytes/max_seconds/max_slices`）、`MAX_PROGRESS_RECORDS`、`record_page_observation`、`_cleanup_cursor`、`_classify_after_race`、`recover_local`、`resolve_successor_ex`、`LineageResult`，以及 `tests/test_u1a_rfa_observation`、`test_u1a_rfa_delivery`、`test_u1a_rfa_lineage` 的 helper。

## 1. v3 的三处更正

1. **v3 §1.3 第 6 条把职责说错了**。stat（`device/file_index/size`）只能当**提示**：它无法发现同 inode 的等长原地改写，恢复 mtime 后更无法发现。trusted 复用不创建交付、不经过发布/确认门，所以"后面还会校验"不成立。v4 §2 用"提交前严格确认 pass"取代。
2. **v3 §2.6 自相矛盾**。伪代码写"except `_StorageFault` 后继续"，后文又写"账本不可读要上抛"。v4 §5 冻结为：**读不通 → 抛 typed 故障；读通但写被拒 → `recovery_needed` 且不再盲写**。
3. **v3 §1.1 的"单 page_spec 自身超限"没有兑现**。v4 §6 把 `max_bytes/max_seconds` 改成真正的每页硬限额，并把剩余额度传进观察器。

## 2. 协议一：观察与证据（RFB-01）

### 2.1 三层分离

| 层 | 来源 | 能做什么 |
| --- | --- | --- |
| `candidate_hint` | `page_observation_progress` 事件里的历史记录 | 只提示"这个候选以前看过什么"、避免重复打开；**不能**直接授权复用，**不能**当成功证据 |
| `attempt_evidence` | 本次 `accept_source_page` 调用内的观察 | 决定候选状态（reuse/changed/missing/hold） |
| `accepted_receipt` | `source_batch_pages`/`source_pages` 的已提交 receipt | 允许**零哈希**幂等重放（历史提交事实） |

### 2.2 提交前严格确认 pass

一次受理在"所有候选都有结论"之后、入队 writer 命令之前，对**所有来自 `candidate_hint` 的正面复用候选**重新做一次完整观察（`_verify_owned` 等价：`device/file_index/size` + 完整 sha256）：

* 新观察仍是 `HASH_MATCH/QUICK_MATCH` → 允许复用；
* 变成 `CHANGED/CHANGED_DURING_CHECK` → 该候选转 `needs_verification`（`artifact_changed`），**不重下载**；
* 变成 `MISSING/UNREADABLE/ROOT_UNAVAILABLE` → 按各自既有语义（`MISSING` 走正常下载、`UNREADABLE` hold、`ROOT_UNAVAILABLE` 整页保持可恢复）；
* 观察抛 `OSError`/`ContractError` → 该候选记 `unobserved`，由写者决定（不得静默复用）。

本次调用内已经观察过的候选不需要再确认（同一次 attempt）。确认 pass 在写事务之外，不做任何 DB 写。

**代价**：每个跨调用复用的文件多一次完整哈希（33 页作品约 66 次，仍一次受理完成）。必须实测并给出数字。

### 2.3 线性化点与残余竞态

页面提交的线性化点是 writer 事务。确认 pass 与事务之间仍有毫秒级窗口（外部改写可以落在中间）；v4 **不承诺**这段窗口被消除，也不能用 stat 冒充已消除。文档与测试必须如实表述。

## 3. 协议二：进度与容量（RFB-02）

### 3.1 可续状态

`progress_usable = writes_ok and candidate_count <= MAX_PROGRESS_RECORDS and not truncated`

* `writes_ok`：本次调用中 `record_page_observation` 没有失败（失败只记 `_progress_write_error`，随后 `writes_ok=False`）。
* `truncated`：读取时触到保留上限或 200 事件窗口，导致已持久化的记录可能不全。
* `candidate_count`：本页需要观察的候选 artifact 数。

### 3.2 切片上限与完成式尝试

* `progress_usable=True`：按 `max_files` 切片、`max_slices` 片；每片先持久化再判断。
* `progress_usable=False`：**关闭切片上限**，本次调用在**每页预算**内一直观察到覆盖全部候选并提交。理由：进度不可用时重试永远不会前进，完成式尝试是唯一能收敛的行为。
* **末项边界**：任何一次"切片收口"之前先判断是否还有未覆盖候选；没有就直接提交，不得仅因计数正好等于 `max_files` 再抛一次。
* `PreflightCancelled("page_slice_limit")` 必须携带 `details={"new": n, "persisted": m, "remaining": k}` 且 `n > 0`；若 `n == 0` 说明本次没有任何净进展，必须改走完成式尝试或抛 `page_progress_unavailable`（typed），不得返回可重试的预算结果。
* 暂停/取消仍然优先：任何两片之间检查取消，先持久化已完成的观察，再抛 `cancelled`。

### 3.3 容量

`MAX_PROGRESS_RECORDS` 是**读取/保留容量**，不是页面支持上限。页面支持上限由 §6 的每页字节/时间预算决定；超过即 typed 拒绝（`page_byte_budget`/`page_time_budget`），cursor 不动，控制面留可读原因。

## 4. 协议三：清理扫描（RFB-03）

```python
@dataclass(frozen=True, slots=True)
class TierScan:
    facts: tuple[dict, ...]
    next_cursor: int
    eof: bool
    budget_exhausted: bool
    examined: int
    offered: int
```

* 每个 tier（新行 tier0 / 失败重试 tier1）独立扫描、独立游标、独立 `eof`/`budget_exhausted`。
* **空结果不得兼任 EOF**：只有"确实读到队尾"（没有更多行）才 `eof=True`；检查到 `examine_cap` 而未凑满 `limit` 时是 `budget_exhausted=True`，游标必须前进到已检查的最后一行的 rowid。
* `_sweep_cleanup` 只有在该 tier `eof` 时才把它的游标回绕到 0；`budget_exhausted` 保留游标，下次继续。
* tier1 的"同调用内回绕"同样只在 `eof` 时发生。
* `list_cleanup_candidates(limit=200, *, after_rowid=0, after_retry_rowid=0, stats=None)`：`stats` 至少含 `next_rowid/next_retry_rowid/tier0_eof/tier1_eof/tier0_budget_exhausted/tier1_budget_exhausted/examined/skipped/reserve`。
* 保留位（`limit>=8` 时 `max(1, limit//8)`）与既有排序不变。

## 5. 协议四：恢复错误（RFB-05）

| 类别 | 判据 | 结果 |
| --- | --- | --- |
| `cas_loser` | 重读成功，行已被别人改成 completed/cancelled | 报告赢家状态，不盲写 |
| `compensation_write_failed` | 重读成功，写 issue 被 CAS 拒绝且重读仍非目标状态 | 报告 `recovery_needed`（行未改写），**不再写第二次** |
| `ledger_unreadable` | 重读本身抛 `CorruptDataError`/`_StorageFault`/不可读 `LibraryError` | **抛 `_StorageFault`**，保留 `prepared`/字节，调用方停止派发 |
| `file_unobservable` | 观察抛 `OSError`/`ContractError` | 保持 `prepared`/`recovery_needed` |

* `_classify_after_race` 与 `recover_local` 的**最外层**都按此表；`recover_local` 不得把 `_StorageFault` 吞成 `recovery_needed`（含 `publish_authorized` 抛出的 `CorruptDataError`）。
* 清理循环里 `list_cleanup_candidates` 抛 `_StorageFault` 也要上抛，不得转成空列表。
* 上抛后控制面行为：停止本轮派发、保留行与字节、不写任何完成/清理事实。

## 6. 协议五：预算（RFB-06）

* `max_files`/`max_slices`：**每次调用**的切片与片数上限。
* `max_bytes`/`max_seconds`：**每页/每次尝试**的硬限额，覆盖观察 + 提交前确认 pass。默认 `max_bytes=512 MiB`、`max_seconds=300`。
* 开文件前用 artifact 行记录的 `size` 预判：`remaining_bytes < size` → 直接 `page_byte_budget`（不打开该文件，不静默超读）。
* 观察时把 `ObservationBudget(max_bytes=min(remaining, per-file cap), max_seconds=min(remaining, per-file cap))` 传给 `LocalObserver`；观察器自己的上限仍然生效。

> v4.1 修订（2026-11-14，S1 实施反馈）：**不把取消标志传入 `LocalObserver`**。v3 §3.1 的 admission 契约要求"已经受理的观察可以完成"（`test_a_ticket_taken_before_pause_may_finish` 依赖它）；传入取消标志会让在飞的哈希变成 CANCELLED 判决，把控制结果伪装成文件判决。暂停/取消仍在**文件之间**与**切片之间**生效。
* 末文件之后再检查一次：超限 → `page_byte_budget`/`page_time_budget`（cursor 不动），不得提交。
* 单个文件本身大于整页上限：`page_byte_budget`，明确不可推进；不得无限空切片。

## 7. 协议六：后继解析（RFB-04）

* 节点集合 = {anchor} ∪ 窗口行（`rowid > anchor`，≤64）∪ 事件目标（按 delivery/artifact id 取，≤64）。
* 对**节点集合内的每一行**批量读取 `recovery_owner_transfer` 事件（一次查询），逐个校验目标（页面/路径、quality 作用域、字节质量、owner completed、self-reference）。
* 迭代解析：有事件的节点按事件，无事件的节点按窗口顺序（completed → active → superseded），`visited` 防环。
* 预算：`MAX_SUPERSEDE_NODES=64`、`MAX_EVENTS=64`、`MAX_LINEAGE_QUERIES=4`；超限 → `budget_exhausted`（不是 `none`）。
* 显式坏目标 → `event_target_unusable` 拒绝，不得静默跳到别的 completed。
* 读预检、writer CAS、carry 共用同一结果；carry 只有在 `reason == "none"` 时才允许自有回退（当前已删）。

## 8. 验收映射

| 探针 | 依赖协议 | 期望 |
| --- | --- | --- |
| `test_cached_hash_cannot_approve_an_in_place_equal_size_edit[False/True]` | §2 | 编辑后 1 个 `needs_verification` + 1 个 `completed` |
| `test_missing_progress_cannot_turn_a_large_page_into_an_endless_retry[cache_cap/progress_write]` | §3 | 8 次内提交 |
| `test_cleanup_sweep_does_not_treat_examine_budget_as_end_of_queue[1/200]` | §4 | 末尾合法记录被访问 |
| `test_superseded_intermediate_event_can_lead_to_an_older_completed_artifact` | §7 | anchor 解析到 C，公开恢复 `reused` |
| `test_unreadable_ledger_is_not_reported_as_a_recovery_classification` | §5 | 抛 `_StorageFault` |
| `test_recover_local_surfaces_an_unreadable_publication_ledger` | §5 | 抛 `_StorageFault` |
| `test_a_single_file_cannot_silently_exceed_the_declared_slice_byte_budget` | §6 | `page_byte_budget`，checkpoint 为 None |
| `test_restored_missing_cache_does_not_overwrite_a_real_file`（绿） | §3 | 恢复的文件不被覆盖，0 请求 |
| `test_original_twelve_rfa_regressions_are_not_weakened`（绿） | — | AST 与审计原件一致 |

全量：非 Tk + Tk 分文件；旧 12 + 15 + 14 反例保持；不给旧断言加 skip/xfail。

## 9. 明确不做

不合并 main、不部署、不碰真实图库/账号/网络；不做 events 索引/投影迁移、carry 长事务、claim 语句优化、真实外置卷/迁移演练、W5 接线、completed 重核验、旧 `record_manager` 健康门。不承诺消除确认 pass 与 writer 事务之间的毫秒级窗口。
