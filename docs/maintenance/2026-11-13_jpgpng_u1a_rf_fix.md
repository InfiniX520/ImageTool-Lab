# U1-A RF 复审问题修复（RF-01..RF-09）

- 日期：2026-11-13
- 批次：`JPGPNG-U1A-RF-FIX-20261113-01`
- 分支：`fix/a1-u1a-rf-20261113`
- 审计基线：`b334a29`（产品 `3ee638f`，15 项复审反例）
- 契约基线：`b1a4f1d`（[U1A_STATE_CONTRACT_V2.md](../optimization/2026-11-13/U1A_STATE_CONTRACT_V2.md)）
- 产品最终提交：`b07c2ed`（本记录所在的文档提交紧随其后）
- 环境：`D:\Miniconda3\envs\JuiLiu\python.exe`，`-s -B -X utf8`、`PYTHONNOUSERSITE=1`；只用合成 PNG、临时 SQLite、假 HTTP。未访问真实图库、生产数据库、账号或网络。

## 1. 结果

| 验证 | 数量 | 结果 |
| --- | --- | --- |
| 非 Tk 全量 | 1,721 | 1,717 通过 / 0 失败 / 0 错误 / 4 跳过 |
| Tk 展示（首轮 / 复跑） | 9 | 首轮 8 通过 + 1 个 Tcl 初始化错误；复跑 9/9 |
| 合计（复跑） | 1,730 | 1,726 通过 / 4 跳过 / 0 失败 |
| 复审 15 项反例 | 15 | 15/15 通过 |
| 上一轮 14 项反例 | 14 | 14/14 保持通过 |

4 个跳过沿用既有平台边界（3 个真实符号链接 + 1 个 W 阶段登记）。Tk 首轮 `tcl_findLibrary`/ttk 初始化错误与审计记录一致，复跑通过；两次结果都保留，不合并成“从未失败”。

## 2. 修复对照

| 编号 | 现象 | 修法 | 主要提交 |
| --- | --- | --- | --- |
| RF-01 | `recovery_needed` 后排除，`recover_local` 抛 `_StorageFault` | 取消绑定期望状态（prepared/recovery_needed 均 CAS）；final 已存在走 `recovery_needed/published_before_abort`；temp 归属失败保留；CAS 竞态转 `recovery_needed`，不再逃出启动恢复 | `c4a9ab8`、`b07c2ed` |
| RF-02 | 外任务接管后原任务续跑失路 | 后继解析顺序：handover 事件 → rowid 更新 → superseded 跟随；carry 不复制 cancelled 引用；外任务**活动**槽拒绝收养（`successor_foreign_active`）；历史链接与收据不改写 | `c4a9ab8`、`8b366f9`、`b07c2ed` |
| RF-03 | accept 遇坏意图仍激活后续任务 | `CorruptDataError` 在观察线程置 `_dispatch=False` 并持久 block；CollectionRuntime 映射 `intent_unreadable`；普通单图失败不升级 | `da7e8fb` |
| RF-04 | 无观察的 held 仍可派发 | pending 有历史 artifact 但无 resolver/观察失败/None → PARK 成 NV（reason 明确）；无历史 artifact 仍 `repair_queued`；不伪造 MISSING | `8b366f9` |
| RF-05 | negative 证据未绑定 artifact 行 | payload 恒带 `observed_artifact_id`，reuse 另带 `matched_artifact_id`；reuse/repair/park 统一校验行、`matches_row`、页面/质量/路径/root/policy/freshness/expected-old；拒绝零退休 | `8b366f9` |
| RF-06 | 受理基于锁外旧 phase | 每批 admission 在 `_guard` 内开关；epoch 只进观察队列；队列满/Future 取消归还登记；settle 用正式 outstanding | `da7e8fb`、`b07c2ed` |
| RF-07 | owner 异常退出后新线程不退出 | 四循环同查 `_closing/_shutdown`；owner finally drain 四队列并给 Future 终态；close 幂等、可重试、最后关 writer | `da7e8fb` |
| RF-08 | 暂停不取消剩余哈希、无整页预算 | 预检在候选间检查 cancel；`PageBudget` 在**开始下一个文件前**检查；`PreflightCancelled` 不写行/不推 cursor/不转 MISSING；暂停归类为 `_Stopped`，预算归类为 `_Budget`，不再落成 `source_runner_failed` | `da7e8fb`、`b07c2ed` |
| RF-09 | 取消后的 `.part` 无清理重试入口 | `CleanupFact` 只从 `cancelled + prepared_json` 构造；有界候选查询排除已清理行；重验 identity/size/sha 后删除；失败保留待办；`recover_local` 有界重试 | `c4a9ab8`、`b07c2ed` |

## 3. 新增不变量

- “未发布”不是单个状态：prepared 与 recovery_needed 都必须有 final 不存在 + 自有 temp 的证据；已发布文件不因随后排除被删。
- 清理只信 prepared 证据，不扫目录；清理成功不改变交付状态、不重新发布。
- superseded 是后继关系，不是用户放弃；历史行不动，当前活动批次解析后继。
- `held` 必须与调度一致：危险/未知原因要么 PARK 成 NV，要么 refused；`repair_queued` 只用于真正可修复的 pending。
- 受理是进程内票据 + durable stop；暂停后新页不得入队，暂停前已登记的在途页有界收尾。
- 取消/预算是控制结果，不是文件损坏；不得生成 MISSING/CHANGED 或推进 cursor。

## 4. 只读审查与返修

独立只读 fork 审 `b1a4f1d..HEAD`：1 P1 + 3 P2 + 若干 P3。`b07c2ed` 修了：

- P1-1：暂停被当 `source_runner_failed` 并停整轮 → FollowCollection 把 `PreflightCancelled("cancelled")` 映射 `_Stopped`，`page_*` 映射 `_Budget`；runtime 增加专门分支。
- P2-1：已清理行永久占据 200 条候选窗口 → 候选查询排除已有 `cleanup_completed` 事件的行。
- P2-2：收养外任务活动 pending 导致永远不可派发 → 拒绝 `successor_foreign_active`。
- P2-3：CAS 竞态/库故障仍可逃出 `recover_local` → 转 `recovery_needed`，清理查询包 try。
- P3-3：carry 后继 gate 的 `not enforce_completed_health or ...` fail-open → 强制 gate。

新增测试：`tests/test_u1a_rf_budget.py`（预算与暂停分类）、`tests/test_u1a_rf_recovery.py` 外任务活动后继拒绝。

## 5. 未关闭 / 遗留

1. `finish_source` 的 `result(20)` 仍是旧语义；`_WaitTimeout` 只保证不重投，未实现“超时后查询最终结果”。
2. 信任故障按 `CorruptDataError` 与两个 stop_reason 分类；其它库级故障仍按单任务处理。
3. `AdmissionTicket` 类尚未实例化，epoch 未进写者；当前准入是 `_admission_open + active batch` 的进程内 bool（跨进程不保留）。
4. 清理事件去重只看最近 200 条；极端历史库可能重复一次 `already_absent`。
5. 意图投影/索引未做；`delivery_cleanup`/`recovery_owner_transfer` 查询仍扫 `events`。
6. carry 长写事务、claim 对 2,000 页作品约 8,020 条语句/70 ms（M 路基线）仍未优化。
7. 真实迁移/备份/外置卷/真实重解析点、W5 UI、completed 重新核验、旧 `record_manager` 健康门仍未做。
8. 首轮 Tk 的 Tcl 初始化错误未归因到本轮产品改动，复跑 9/9，保留两次记录。

## 6. 复现

```powershell
$env:PYTHONNOUSERSITE = '1'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest tests `
  -q -p no:cacheprovider `
  --ignore=tests/test_collection_presentation.py `
  --ignore=tests/test_source_presentation.py

$env:TCL_LIBRARY = 'D:\Miniconda3\envs\JuiLiu\Library\lib\tcl8.6'
$env:TK_LIBRARY  = 'D:\Miniconda3\envs\JuiLiu\Library\lib\tk8.6'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest `
  tests/test_collection_presentation.py tests/test_source_presentation.py -q
```

证据：`.runtime/jpgpng_u1a_rf_fix_20261113_01/`（`red01/` 红基线、`full03/` 最终全量、`tk01-02/` Tk、`lanes/*/run*/` 各路记录、`probe/m1/` 规模测量）。

## 7. 改动文件

- 新增：`core/delivery_cleanup.py`、`core/delivery_lineage.py`、`docs/optimization/2026-11-13/U1A_STATE_CONTRACT_V2.md`
- 产品：`core/library_repository.py`、`core/page_delivery.py`、`core/delivery_repository.py`、`core/delivery_recovery.py`、`core/task_coordinator.py`、`core/collection_runtime.py`、`core/follow_collection.py`
- 测试：`tests/test_u1a_fix_boundaries.py`（审计 15 项原样带入）、`tests/test_u1a_rf_pipeline.py`、`tests/test_u1a_rf_recovery.py`、`tests/test_u1a_rf_control.py`、`tests/test_u1a_rf_cross.py`、`tests/test_u1a_rf_budget.py`
