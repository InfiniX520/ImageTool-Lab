# U1-A 复审（RF）状态契约 v2

基线：审计 `b334a29`（产品 `3ee638f`）。本文件补在第一版契约之上，只增加三组协议；第一版未被撤销的条款继续有效。任何实现不得单方面改写；需要变更先回集成分支。

## 0. 本轮不变量

- 历史 artifact、完成交付、已接受来源收据、旧批次链接不改写。
- 不新增表/列、不改 schema 号。清理事实复用 `deliveries.prepared_json`，受理票据是进程内代次。
- 发布语义保持 gate-wins：`publish_authorized` 是线性化点；已落盘文件不因随后排除被删除。
- 所有文件观察在写事务外；写者只做 CAS、链接和事件。

## 1. P-A 发布 / 取消 / 清理

### 1.1 事实

```
UnpublishedEvidence{ delivery_id, batch_id, state ∈ {prepared, recovery_needed},
                    generation, prepared_digest, temp_ref, final_ref,
                    final_absent, temp_owned }
```

- `prepared`：已 prepare、未 link。
- `recovery_needed`：可能是“未 link 的本地故障”，也可能是“已 link 未 confirm”。**不能整体等同未发布。**

### 1.2 取消

`cancel_unpublished_delivery(delivery_id, batch_id, *, error_code, expected_state)`

- `expected_state ∈ {prepared, recovery_needed}`，CAS `WHERE state = expected_state`；
- executor 必须先确认 `final` 不存在且 temp 归属可验证；写者把 `expected_state` 与 `unpublished_asserted` 记入事件；
- `final` 已存在 → 不走取消；走 confirm 或保留 `recovery_needed`；
- 外来 final / 证据不全 / 不可读 → 保留字节并 hold，不 Cancel。

| 状态 | final | temp | 动作 |
| --- | --- | --- | --- |
| prepared | 不存在 | 归属正确 | 取消 + 清理待办 |
| prepared | 外来存在 | — | hold，保留 prepared，不动外来文件 |
| recovery_needed | 不存在 | 归属正确 | 取消 + 清理待办（RF-01） |
| recovery_needed | 自己已 link | — | confirm |
| recovery_needed | 外来/证据不全 | — | 保留，hold，诊断 |

### 1.3 清理

- `CleanupFact`（`core/delivery_cleanup.py`）只从 `state='cancelled' + prepared_json` 构造；
- `list_cleanup_candidates(limit)` 与 `cleanup_cancelled_delivery(delivery_id)`：重验 temp 归属（identity/size/sha）、拒绝链接/越界；成功追加 `cleanup_completed`，失败保留待办与诊断；
- `recover_local()` 结束前做一遍有界清理重试；`cleanup_delivery()` 对 cancelled 复用同一实现；
- 禁止目录扫描、禁止删非精确归属文件、禁止凭 `.part` 重新发布、清理成功不等于交付成功。

## 2. P-B 取代关系与证据

### 2.1 后继

```
Successor{ delivery_id, job_id, batch_id, state, generation, artifact_id }
resolve_successor(conn, delivery) -> Successor | None   # core/delivery_lineage.py
```

- 顺序：① `recovery_owner_transfer` 事件记录的精确 artifact（跨 job 的旧 completed 靠它）；② rowid 更晚的 completed/活动后继；③ 更晚的 `cancelled + superseded_by_recovery_*`（≤4 跳，环/过深 → 无后继）；
- 不在 lineage 里按 `generation` 过滤（跨任务不可比）；carry 侧先解析被取代批次自己的 page link，再回落到 lineage；
- carry（`_copy_batch_item_page_links`）遇到 superseded 引用：能解析且字节可核验 → 当前活动批次链 artifact；否则建本批 NV（`carried_superseded_unresolved`）；**不复制 cancelled 引用**；
- 历史 `batch_item_pages` 不改写。

### 2.2 证据

恢复 payload 必须同时携带：

```
observed_artifact_id   # negative 也有；来源行身份
matched_artifact_id    # 仅 reuse 时非空
health, selected_state, page_key, previous_delivery_id, previous_state, generation
```

写者对 reuse/repair/park 一律校验：行存在且有效、`expectation.matches_row`、页面/质量/输出路径、root revision、policy、freshness、expected-old。失败 → `refused/stale_observation`，不退休 hold、不占槽、不耗预算。

### 2.3 held 的调度意义

| 前态 | 观察 | 结果 |
| --- | --- | --- |
| pending | 有历史 artifact，但无 observation（无 resolver/观察失败/None） | **PARK** → NV，reason 取 `no_resolver/observation_failed/observation_unavailable` |
| pending | 无任何历史 artifact | `repair_queued`（真正新下载） |
| needs_verification | 无 observation | `held`（本来就不可派发） |

`held` 且属于危险/未知集合 ⇒ 不在 `list_dispatchable`；`repair_queued` ⇒ 至多一个 pending。

## 3. P-C 控制协议

### 3.1 受理票据

```
AdmissionTicket(batch_id, epoch)
```

- coordinator 在 `_guard` 内签发；`pause/cancel/close` 在同一 guard 关闭 admission 并递增 epoch；
- `accept_page` 只有拿到有效票据才入队；写者只对带有效票据的页允许 `pausing` 提交；
- 未拿到票据 → Future 立即 `InvalidStateError`，不排队、不哈希；
- 禁止用锁外 DB 读出的 phase bool 作为受理依据。

### 3.2 信任故障

- `TrustFault`：坏意图投影、可疑证据、损坏快照等全库级问题；
- accept/claim/recovery/publish 任一路遇到 → `coordinator._dispatch=False` + `trust_fault` 事件 + 具体错误；后续任务不激活，直到用户修复并显式 `run_queued`；
- 普通单图 HTTP/磁盘/校验失败仍按单图策略，不升级为全库停机。

### 3.3 生命周期

- `_closing` 与 `_shutdown` 等价于同一个退出信号；observer/control/owner/worker 四个循环都检查；
- owner finally 负责 drain 四类队列并对未完成 Future 给出唯一终态；
- `close()` 幂等；超时后可再次调用；writer 最后关闭；禁止 daemon 化掩盖泄漏。

### 3.4 取消与预算

```
accept_source_page(..., cancel: Event | None, page_budget: PageBudget | None)
PageBudget(max_files, max_bytes, max_seconds)
```

- 预检在候选之间检查 cancel；累计整页文件数/字节/耗时；超限或取消 → `PreflightCancelled`；
- `PreflightCancelled` 不写行、不推 cursor、不生成 MISSING/CHANGED、不占预算；
- 文件级预算保留；取消/预算耗尽不是文件损坏；
- `FollowCollection._accept` 的 `result(timeout)` 超时不得重投同一页：保留命令身份，区分“仍在处理/已取消/已提交/被拒绝”。

## 4. 文件所有权（RF 轮）

| 阶段 | 唯一写入范围 |
| --- | --- |
| S0（主） | 新模块、契约、`library_repository.py` 冻结原语、15 项测试接入 |
| P1 | `core/page_delivery.py`、`core/delivery_repository.py` |
| P2 | `core/delivery_recovery.py` |
| P3 | `core/task_coordinator.py`、`core/collection_runtime.py`、`core/follow_collection.py` |
| T | 仅 `tests/test_u1a_rf_cross.py` |
| M | 仅 `.runtime/.../probe/` |

合并顺序 P1 → P2 → P3；T/M 并行。

## 5. 验收映射

| 反例 | 归属 |
| --- | --- |
| foreign_handover[reuse/repair] | P1（carry 后继）+ P2（恢复） |
| pending unobservable / no resolver | P2 |
| negative binding[artifact_id/sha256/size_bytes] | P2 |
| fatal owner threads | P3 |
| pause admission stale read | P3 |
| accept corruption activation | P3 |
| pause cancels remaining hashes | P3（S0 已提供 cancel/budget 钩子） |
| excluded recovery_needed local recovery | P1（+ S0 原语） |
| cancelled owned temp cleanup | P1（+ `core/delivery_cleanup.py`） |
| gate-wins boundary / intact history | 保持通过，不改语义 |

## 6. 禁止捷径

不许 xfail/skip/放宽超时；不许 sleep 造竞态；不许删坏事件；不许把 prepared/recovery_needed 一律当未发布；不许扫描目录删 `.part`；不许把 superseded 当用户取消或批量重写历史链接；不许用 daemon 线程掩盖泄漏；不许在写事务内做文件 I/O；不许改 schema 号。
