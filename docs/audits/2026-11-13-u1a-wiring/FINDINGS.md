# J-01 生产复用问题明细

日期：2026-11-13；产品 `4023fca`（代码 `dda6fc1`）。本页只描述当前行为与评审结论，不代表已修复。

## 1. 当前行为

| 场景 | 新任务页面状态 | 可派发 | 磁盘 |
| --- | --- | --- | --- |
| 删除已交付文件 | completed=1, existing=1, new_completed=0, pending=0, needs_verification=0 | 空 | 文件不存在 |
| 等长非图字节替换（70 B） | 同上 | 空 | 仍是替换字节 |
| 同尺寸真实 PNG、摘要不同 | 同上 | 空 | 仍是替换 PNG |
| 批次结算 `coverage_complete=True` | phase=completed, works.completed=1 | — | 与实际磁盘不符 |

独立测试节点（全部红）：

- `test_deleted_file_is_not_reused_by_a_new_task`
- `test_missing_page_is_queued_for_repair_and_the_repair_restores_bytes`
- `test_equal_length_non_image_bytes_are_not_reused`
- `test_same_size_png_with_another_digest_is_not_reused_and_is_preserved`
- `test_a_missing_or_changed_page_cannot_settle_as_completed[delete|non_image|same_size_png]`

## 2. 根因

1. **`DeliveryRepository._find_reusable_artifact`**（`core/delivery_repository.py:1245`）只按 job/illust/page/output_key/verification/size/sha/quality 查询，遍历 `artifacts` 行；`_artifact_is_valid`（同文件 :372）只检查记录字段。没有打开文件，也没有当前观察。
2. **同任务 completed existing 分支**（`core/library_repository.py:2018`）直接按 completed delivery 复用 `delivery_id/artifact_id`，同样不检查磁盘。
3. **trusted 跨任务分支**（`core/library_repository.py:2049-2072`）调用 `_find_reusable_artifact` 后直接 `_link_batch_item_page` 并 `continue`，不生成新交付。
4. **关注/名单规划**（`core/follow_repository.py:400-409`）的 `_candidate_requires_write` 用 `job_id=''` 调同一查询；artifact 命中就不把作品计入需要写入，`counts_work`/预算也随之改变。
5. **调用位置在写事务内**：`_accept_source_page` 从 `core/library_repository.py:1746` 开始 `with self._transaction(conn)`，`_candidate_requires_write` 在 :1819 调用。不能在这里打开/哈希文件。
6. **没有用户意图状态**：`RemovalIntent` 只在 `core/preservation_policy.py` 的纯决策层；交付/库层没有“有意移除”记录，无法区分“用户主动删了”与“意外缺失”。
7. **没有 artifact 当前健康投影**：历史 artifact 行没有“核验于某时/当前状态”字段，也没有观察表。

## 3. 调用面

| 位置 | 作用 | 是否读盘 | 风险 |
| --- | --- | --- | --- |
| `core/delivery_repository.py:1245` | 跨任务 artifact 选择 | 否 | 旧记录即视为可用 |
| `core/library_repository.py:2050` | trusted 跨任务复用 | 否 | 跳过下载并计 existing |
| `core/library_repository.py:2018` | 同 job completed delivery 复用 | 否 | 同任务重跑/恢复也误报 |
| `core/follow_repository.py:404` | 关注候选是否需要写 | 否 | counts_work/预算被旧记录影响 |
| `core/library_repository.py:1819` | 规划时调用上述 | 否 | 与写事务耦合 |
| `core/download_service.py:310-313` | 注释把 trusted skip 交给外层记录 | 否 | 外层没有健康检查时会误跳过 |
| `core/archive_*` | 离线接受/计划 | 否（生产未调用） | 当前不是 J-01 路径 |

`_find_reusable_artifact` 直接调用者 2 个（`library_repository`、`follow_repository`）；同 job completed existing 是第三条旁路。归档新模块没有被生产 `core` 模块导入（只有 archive 模块内部互相导入），本轮不能把 A1 通过数当成 J-01 已修。

## 4. 影响与优先级

- 影响：跨任务复用、同任务重跑/恢复、关注名单完整性、批次完成数/结算、清理后重跑、归档转存前的健康判断。
- 优先级：**生产接线前 P1**。不声称真实图库已经损坏；本轮只证明合成临时库/文件上的当前行为。
- 用户意图：主动删除/转存后不得自动补下载；缺失/变化应进入 `needs_verification` 或显式修复，不应直接 completed。

## 5. 通过条件（下一轮）

1. 删除/等长替换/同尺寸换摘要后，新任务 `completed=0, existing=0, pending+needs_verification=1`；缺失场景有 1 条可派发修复。
2. 批次不得在这些页面未解决时 settle 为 completed。
3. 未改动文件仍复用，不新增交付/artifact。
4. 显式 `redownload` 仍可补齐并保留旧 artifact/收据。
5. 用户主动移除意图存在时，不自动补下载；旧记录保留。
6. 所有拒绝/修复都不覆盖磁盘现有字节，不删除旧收据。
