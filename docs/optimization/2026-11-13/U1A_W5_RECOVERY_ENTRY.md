# U1-A / W5：待核验页面的显式离线恢复入口

更新日期：2026-11-13。基线：`d8de8dc`（分支 `fix/a1-u1a-j01-20261113`）。本批次只改仓库层与测试，不接 UI、不联网、不覆盖磁盘、不改写历史 artifact 与已完成 delivery。

## 1. 问题

J-01 修复后，changed / unreadable / root_unavailable / changed_during_check 的页面不再被当成本次完成，而是落到 `needs_verification`。但这条路径当时只进不出：

- 该 hold 占用 `ux_deliveries_active_output`（`output_key` 唯一活动槽），同一路径上无法再排修复代次；
- 批次只能结算成 `partial`，没有任何“用户已把文件放回／已把盘接回”的显式入口；
- 新任务也修不了：accept 路径同样会因为同一 hold 占用而只能再建一个 hold 或直接失败。

用户把文件恢复原位、把移动盘接回来、或意识到“这页其实没丢”之后，产品没有任何动作可用。DESIGN 2.4 早已列出这些动作（“重试本地检查”“补齐缺失”“保留当前文件”），W5 把它们落成唯一的仓库层入口。

## 2. 一个公共命令

```python
repo.recover_verification_page(batch_id, illust_id, page_index, *, command_id=None) -> dict
```

- 只在 `running` 批次上生效（结算中/已结束的批次拒绝，避免改到结算口径）。
- 页面必须已被该批次选中（有 `batch_item_pages` 链接），否则 `NotFoundError`。
- `command_id` 可选。给出时按既有命令事件表做“恰好一次”：首个已接受结果原样重放，且重放不再读盘。

### 2.1 返回契约

固定字段（JSON 安全，可直接进事件/诊断）：

| 字段 | 含义 |
| --- | --- |
| `outcome` | `reused` / `repair_queued` / `held` / `completed` / `refused` |
| `reason` | 稳定原因码（见下表） |
| `batch_id`,`job_id`,`illust_id`,`page_index`,`output_key` | 定位信息；`output_key` 是路径摘要，不是明文路径 |
| `previous_delivery_id`,`previous_state`,`previous_error_code` | 调用前该页链接的 delivery 及其状态 |
| `delivery_id`,`state` | 调用后该页的 delivery 与状态（复用后是被复用 artifact 的 delivery） |
| `artifact_id` | `reused`/`completed` 时被复用（或已在该页）的 artifact；`refused` 时是本次想链接但未生效的目标 |
| `repair_delivery_id` | `repair_queued` 时新（或已在队）的修复代次 |
| `health` | 主候选的 `ArtifactHealth.to_payload()`，含 `state`/`reason`/`checked_at`/`observed_*` |
| `observed_size`,`observed_sha256`,`checked_at` | 主候选的本次观察摘要，便于界面说明“核验于何时” |

原因码：`hash_match`、`artifact_missing`、`repair_already_pending`、`artifact_changed`、`artifact_unreadable`、`artifact_root_unavailable`、`artifact_changed_during_check`、`observation_unknown`、`observation_unavailable`、`no_recorded_artifact`、`cross_task_reuse_disabled`、`already_completed`、`batch_not_running`、`page_not_in_batch`、`page_owner_mismatch`、`work_budget_exhausted`、`output_blocked_by_other_task`、`state_changed`、`stale_preflight`、`unsupported_previous_state`。

候选选择与 accept 路径一致：先看本任务自己的 completed 收据，再看 `dedup_policy=trusted` 的跨任务收据。因此 `redownload` 任务不会因为恢复入口而静默复用别的任务成果——观测不到本任务收据时会返回 `cross_task_reuse_disabled`（该判断只查库、不读盘）。

## 3. 状态机

`previous_state` 是页面当前链接的 delivery 状态；`observed` 是本次对页面已有 artifact 的完整摘要观察。

| previous_state | observed | action | 结果状态 | 说明 |
| --- | --- | --- | --- | --- |
| completed / 已链接 artifact | 不观察 | none | completed | 幂等返回，不做 IO |
| needs_verification | hash_match | reuse | completed | 取消 hold，链接既有 artifact |
| needs_verification | missing | repair | pending（新代次） | 取消 hold，插入一个 pending |
| needs_verification | changed | hold | needs_verification | 不写字节，保留原因 |
| needs_verification | unreadable / root_unavailable / changed_during_check / unknown | hold | needs_verification | 不猜丢失 |
| pending | hash_match | reuse | completed | 修复已不需要 |
| pending | missing | none | pending | 幂等，不再造代次 |
| pending | changed | park | needs_verification | 避免后续白跑一次必败传输 |
| pending | 其它 | none | pending | 保持不变 |
| 其它（failed/prepared/transferring/…） | – | none | refused | 明确拒绝 |

纯函数 `decide_page_recovery()` 就是上表，没有 I/O；19 组参数化用例逐行覆盖。

### 3.1 旧行的处置（不删、不改历史）

| 旧行 | 处置 |
| --- | --- |
| `needs_verification` hold | reuse → `cancelled` + `recovered_hash_match`；repair → `cancelled` + `superseded_by_recovery_repair`。行保留，UID/generation/created_at 不变 |
| `pending` 修复 | reuse → `cancelled` + `recovered_hash_match`；park → `needs_verification` + `artifact_changed` |
| 历史 `completed` delivery | 只读。UPDATE 的 WHERE 恒带 `state IN ('needs_verification','pending')`，completed 永不匹配 |
| `artifacts` | 从不 UPDATE/DELETE；reuse 只是把既有 artifact 链接回该批次的页面 |
| 已完成批次计数 | 只受本批次自己的 `batch_item_pages` 行影响，旧批次行不动 |

`cancelled` 不在唯一活动槽索引里，因此“取消 hold + 插入 pending 代次”在同一条短事务内即可完成，不会出现“旧 hold 永久占槽”。

## 4. 事务与线程边界（要求 3）

1. 调用线程：`_read_recovery_page()` 用一个只读快照读出批次、页面链接、候选 artifact（同任务 completed 优先，其次 `dedup_policy=trusted` 的跨任务候选，最多 8 个）。
2. 同一调用线程：`_observe_recovery_page()` 对候选逐个走 `core.local_observation`（完整 SHA-256、身份/大小/mtime 前后一致）。路径级结论（missing / unreadable / root_unavailable / changed_during_check）立即停止；内容级 changed 继续试下一个候选。
3. 关闭读事务之后才 `_enqueue`；写者事务内**不做任何哈希、不打开文件、不联网**。
4. 写者短事务只做 CAS 与链接：
   - 批次 phase 必须仍为 running；
   - 页面链接的 delivery id 与状态必须等于预检冻结值，否则 `state_changed`；
   - reuse 时对 artifact 行重放 `_health_allows_reuse()`（策略版本、新鲜度、根 revision、行绑定全部重验），任何一项不符即 `stale_preflight`；
   - repair 时先查工作额度与活动槽再写，额度不足 `work_budget_exhausted`，别的任务占槽 `output_blocked_by_other_task`。
   
   测试 `test_the_preflight_never_hashes_inside_a_write_transaction` 用线程本地标记同时断言“没有在写事务内观察”和“没有在写线程里观察”。

## 5. 复用与修复的具体 SQL

- reuse：`UPDATE deliveries SET state='cancelled' … WHERE delivery_id=? AND state=?` → `batch_item_pages` 改指既有 artifact（同任务则带 delivery id，跨任务置 NULL，与 accept 路径一致）→ `_refresh_batch_item_result`。
- repair：查额度 → 查活动槽 → 取消 hold → `generation = MAX(generation)+1` 插入 `pending`（`checkpoint_json`/`prepared_json` 为 NULL，避免把别的 delivery 的 `.part` 续传前缀带进来）→ 页面改指新代次 → 需要时把 `counts_work` 置 1 → 刷新作品结果。
- 每次调用都追加一条 `verification_recovery` 事件（含完整结果），带 `command_id` 时作为 `accepted_command_id` 的唯一键。

## 6. 测试

`tests/test_u1a_recovery.py`（39 项）：全部合成临时库、临时输出根、假 transport、真实 `PageDeliveryExecutor`，无网络/账号/真实图库。

- 纯状态机 19 组 + 4 组非法输入；
- hash_match：复用、hold 取消、无下载、批次可 completed（同任务与跨任务两种链接）；
- missing：新 pending 代次（generation+1、无 checkpoint）、跑通发布、两个 artifact 并存；
- changed：保持 hold、字节不变、批次只到 partial、重复调用不产生新代次；
- root_unavailable：先改名整个根，再改名回来，第一步绝不产生 repair；
- pending 修复：幂等 / 文件回归后转复用 / 外来字节转 park 且不再排队；
- 重复调用与 command_id 重放、跨页面复用 command id 报 `CommandConflict`；
- 运行中批次限制、页面不在批次内、未知批次；
- 工作额度不足时保持 hold；
- 无根绑定（重开库）时 fail closed；
- 过期预检、无冻结证据、以及“摘要自洽但不匹配记录”的伪造证据都不能授权复用；
- 每种恢复后历史 artifact / completed delivery / 已完成批次计数逐字段不变。

对新增代码做过 6 组变异验证（把 changed 改成强制修复、repair 不取消 hold、reuse 跳过健康门、repair 不占额度、park 空操作、写者内部哈希），全部被测试捕获。

## 7. 残余（本轮不做）

1. **UI 未接**：结果里的 5 种 outcome 与原因码足够驱动按钮，但按钮、文案与“重试本地检查/补齐缺失/保留当前文件”的呈现留给后续 UI 批次。
2. **没有列表入口**：一次只处理一页；批量“重试本批全部待核验页”需要新的分页查询与预算，不在本批次。
3. **旧 `.part` 不清理**：hold 被取消后，它名下的暂存文件（若存在）仍留在磁盘；本模块从不删除文件，清理需要单独的“受控清理”协议。
4. **activity 槽预检在当前 schema 下恒为真**：`ux_deliveries_active_output` 保证同一 output_key 只有一条活动行，所以 `output_blocked_by_other_task` 目前不可达，保留作为索引语义的守护。
5. **快速核验策略**：仍只支持完整 SHA-256；`quick_match` 一律按未知处理（fail closed）。
6. **跨会话缓存**：每次恢复都重新观察，没有跨会话缓存，也不把旧观察当新鲜证据。
