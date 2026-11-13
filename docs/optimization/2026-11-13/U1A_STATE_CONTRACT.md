# U1-A 状态契约（独立审计后修复基线）

基线：`892a140`（产品 `8b1c751` + 审计文档与 14 项反例）。本文件是并行修复前冻结的接口契约，任何一路实现不得单方面改写；需要改动先回到集成分支。

## 1. 预检快照（C1 / UA-01、UA-09）

`core/observation_snapshot.py`：

| 类型 | 作用 |
| --- | --- |
| `PageKey(illust_id, page_index, output_key, quality_key)` | 一次内容判断的唯一页面坐标 |
| `PageSnapshot(key, generation, delivery_id, observed, unobserved, degraded, reason)` | 预检对**这一页**看到的完成代次与冻结核验记录 |
| `PreflightSnapshot(run_id, pages, policy, degraded, reason)` | 一次预检的完整结果 |
| `RetryPreflight(reason)` | 写者发现快照未覆盖的证据；**零写入**，事务外重新预检 |
| `PreflightUnavailable(reason)` | 有界重试耗尽；不写任何行 |

写者判定（`_accept_source_page` 内，遇到 `existing.state == 'completed'`）：

1. `snapshot.page(key)` 为空、`page.degraded`、`page.stale_for(generation, delivery_id)` → 抛 `RetryPreflight`。
2. `page.health_for(artifact_id)` 为空 → 抛 `RetryPreflight`（`unobserved` 同样如此）。
3. 有记录 → 只用 `_health_allows_reuse` 决定复用；失败时用**该记录的状态**做 NV 错误码，不再凭空写 `artifact_unknown`。
4. 写者看不到 completed → 正常新建 pending；预检失败不阻断全新下载。

调用侧（`accept_source_page`）：`RetryPreflight` 在事务外重跑预检，最多 3 次且总耗时 ≤ 2 s；耗尽抛 `PreflightUnavailable`。重试不得推进 cursor、不得重复预算、不得第二次下载。

同批 receipt 重放（UA-09）：同 `batch/source/pass/entity/page_identity` 且 `content_digest` 相同 → 直接返回已存 `delivery_ids`，不哈希；写者仍权威复核 digest，不同则 `CursorConflict`。重放是幂等回执，**不表示当前文件健康**：收纳据里没有 mtime/文件身份，accept 不做身份猜测；文件在重放之间被改动，由 `recover_verification_page`（或后续核验入口）实际观察，该入口必须给出 `held`/`reused`，不得静默维持 completed。

## 2. 全局意图作用域（C2 / UA-02、UA-10、UA-11）

`core/removal_scope.py`：

- 键：`IntentKey(illust_id, page_index, output_key, root_revision)`，**跨任务全局**；最新 `event_seq` 生效，旧事件保留。
- `read_projection(conn)` 一次有序读取；坏事件进 `corrupt_events`，`require_clean()` 抛 `CorruptIntentError(event_seq)`；**禁止** `except/continue`、禁止删行。
- `CANCELLABLE_STATES = (pending, suspended, retry_wait, needs_verification)`；`PUBLISHED_STATES = (prepared, completed)`。
- `publish_allowed(key)`：无排除且投影干净 → 真；当前意图为 EXCLUDED 或存在坏事件 → 假。发布线性化点是 `os.link` 之前这一刻；已落盘的交付按“本次已完成，仅约束后续”处理，不删除已发布文件。

落点：

| 入口 | 规则 |
| --- | --- |
| `record_removal_intent(EXCLUDED)` | 追加事件后，对**所有 job** 的该键 `CANCELLABLE_STATES` 置 `cancelled/intent_excluded`；已发布态一律不动 |
| `claim_delivery` | 转移前查投影；EXCLUDED → 取消该行并返回 `None`；投影损坏 → 事务内把批次置 `pausing/blocked(intent_unreadable)` 并写诊断事件，再向调用方抛 `CorruptDataError`（不得用 `None` 静默空转） |
| `recover_verification_page` | `blocks_repair` → `refused/intent_excluded`；坏事件 → `refused/intent_unreadable` |
| `_publish`（链路前，`final` 不存在时） | 调 `publish_authorized(delivery_id)`；`intent_excluded` → 不落盘，`cancel_unpublished_delivery` 置 `cancelled/intent_excluded`，只删自己校验过的 `.part`；`intent_unreadable` → 不落盘，置 `recovery_needed` 并保留 `.part`（不知道意图就不丢弃字节） |
| `_confirm_delivery` | **不复查意图**：发布线性化点是 `os.link` 之前；confirmation 只完成已经落盘的文件（崩溃后 `final` 已存在时按“本次已完成，仅约束后续”处理，不删已发布文件） |

## 3. 恢复证据（C3 / UA-04、UA-06）

所有动作（reuse / repair / park）提交同一份冻结观察：`{health, artifact_id, selected_state, page_key, previous_delivery_id, previous_state, generation}`。

写者闸门（三条同时满足才允许变更）：策略版本一致、`is_fresh`、`root_revision` 与当前绑定一致；页面/路径/大小/摘要与 artifact 行绑定一致；`previous_delivery_id/state` 未变。失败 → `refused/stale_observation`，旧 hold 不退休，event 同时记 `requested_action` 与 `applied_action`。

决定表增补：

| 前态 | 观察 | 结果 |
| --- | --- | --- |
| pending | 无观察 / `no_recorded_artifact` | `repair_queued`（尚未下载，`NONE`） |
| pending | `root_unavailable`/`unreadable`/`unknown`/`changed_during_check`/`changed` | `held` + **PARK** → `needs_verification`，错误码取观察原因 |
| needs_verification | 同上 | `held`（本来就不可派发） |

不变式：`held` 且原因为上述集合 ⇒ 该页不在 `list_dispatchable`；`repair_queued` ⇒ 至多一个 pending 且占唯一输出槽。

选中观察：决定、`artifact_id`、展示摘要、事件证据必须来自**同一条**记录（选了旧版本就用旧版本）；无匹配时另给稳定失败原因。

## 4. 所有权与根（C4、C5 / UA-05、UA-03）

- carry 仍链外任务 NV；恢复获得受控取代权：`hash_match` → 外任务 NV 置 `cancelled/superseded_by_recovery_reuse`，本批页链 artifact（跨 job 时 `delivery_id=NULL`）；`missing` → 先取代外任务 NV，再建本批 pending。`transferring/validating/prepared` 不抢，仍返回 `output_blocked_by_other_task`。事件 `recovery_owner_transfer`。
- `PathResolver.root_state(ref) -> available | missing_bound | unbound`；`_new_stage` 只在 `available` 时允许创建 `.part`，`missing_bound` 抛 `_Fault("root_unavailable", block=True)`，`unbound` 仍由 `resolve()` 报 `ContractError`（不把未映射路径当成可创建目录）。

## 5. 并行分工（Phase 2）

| 路 | 唯一写入范围 | 新测试 |
| --- | --- | --- |
| A accept | `core/library_repository.py` | `tests/test_u1a_accept_race.py` |
| B recovery | `core/delivery_recovery.py` | `tests/test_u1a_recovery_ownership.py` |
| C root guard | `core/page_delivery.py`、`core/library_paths.py` | `tests/test_u1a_root_guard.py` |
| D 跨路反例 | 仅测试（不改产品） | `tests/test_u1a_cross_lane_boundaries.py` |
| E carry 效率 | `core/follow_repository.py`、`core/query_repository.py` | `tests/test_u1a_carry_parse_once.py` |

合并顺序 A → B → D → C → E；UA-07（观察执行器 + 控制通道）在 Phase 3 串行。

## 6. 禁止捷径

不许 xfail/skip/放宽超时；不许 sleep 造竞态；不许删坏意图行；不许把 `prepared` 直接改 `cancelled`；不许去掉所有权检查或抢占别的任务槽位；不许对已绑定根 `mkdir`；不许把 DB 收据当健康证据；不许改 schema 号；不许改写历史 artifact / 完成交付 / 已接受收据。
