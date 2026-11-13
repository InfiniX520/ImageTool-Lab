# U1-A RFA 轮修复记录（JPGPNG-U1A-RFA-FIX-20261114-01）

日期：2026-11-14。起点是 [2026-11-13 复审](../audits/2026-11-13-u1a-rf-recheck/README.md)：在 `3839c90`（产品 `b07c2ed`）上确认 RFA-01..07 共 7 项问题（2 P1、5 P2）。本轮把它们全部按行为关闭，并处理了随后只读审计追加的 2 项 P2 与 2 条邻接路径。

## 1. 基线与范围

| 项 | 值 |
| --- | --- |
| 审计基线 | `26afa48`（审计固定 `3839c90`，产品 `b07c2ed`） |
| 修复分支 | `fix/a1-u1a-rfa-20261114` |
| 集成工作树 | `.runtime/jpgpng_u1a_rfa_fix_20261114_01/integration_worktree` |
| 契约 | [U1A_STATE_CONTRACT_V3.md](../optimization/2026-11-14/U1A_STATE_CONTRACT_V3.md)（v3 → v3.4 五次修订都记录在同一文件） |
| 解释器 | `D:\Miniconda3\envs\JuiLiu\python.exe`（CPython 3.12.14），`PYTHONNOUSERSITE=1` |

全部验收使用合成 SQLite 与合成 PNG：没有真实账号、图库、网络、迁移、部署或数据删除。库 schema 仍是 `"1"`，没有新增表/列/索引/依赖。

## 2. 修了什么

| 编号 | 原问题 | 修法 | 证据（测试） |
| --- | --- | --- | --- |
| RFA-01 P1 | 整页预算耗尽即硬失败，恢复后重复核验同一批 32 个文件 | 预算改为"一次观察切片"：`PageBudget.max_slices`；进度事实（`page_observation_progress`，键不含 batch）跨 resume 复用；复用要过六条判据（行身份、quality、根版本、策略、900 s、stat 对象身份）；缓存与 stat 不占 `max_files` | 审计探针 `test_default_page_budget_can_progress_a_33_image_work_after_resume`；`test_u1a_rfa_observation.py` 24 条；T 的 9 条 R-A 场景 |
| RFA-02 P1 | 根掉线被当成"文件不存在"，已发布交付被取消、清理虚报完成 | 新模块 `core/file_presence.py` 四态探针；取消矩阵按 final/temp 的 PRESENT/ABSENT/UNREADABLE/ROOT_UNAVAILABLE 分格；任何不确定都保持 `recovery_needed` | 探针三连（before_publish 掉线、已发布掉线重连、清理掉线）；S2 的 18 条、T 的 7/8 |
| RFA-03 P2 | 清理事实生产者与消费者字段不一致，已清理行占住窗口 | 单一规则 `cleanup_is_terminal`：新事件带 `cleanup_completed/temp_sha256/temp_size/schema_version`，旧 `outcome=cleaned` 仍算终态；窗口 SQL 排除终态、rowid 分页、坏证据只跳过 | 探针两条 + `test_a_mixed_cleanup_window_advances_at_every_limit` + T 的 9/10 |
| RFA-04 P2 | 精确 handover 被更晚的无关活动行盖过 | 权威顺序改为事件优先 → 窗口 completed → 活动 → 有界链；事件目标校验含页面/路径、quality_key 作用域、字节质量、self-reference、owner completed；不可用即拒绝，不回退 | 探针 `test_explicit_handover...` + S3 的 26 条 + T 的 12/13/17/18 |
| RFA-05 P2 | 同一子图被重复展开（32 条 82,901 条 SQL） | 单次窗口读 + visited 去重 + `MAX_SUPERSEDE_NODES=64`/`MAX_LINEAGE_QUERIES=4`；超限返回 `budget_exhausted` | 探针 `test_lineage_resolves_each_candidate...`；8/16/24/32/64 两套变体（见 §5） |
| RFA-06 P2 | 取消写入往返后按旧身份 unlink，可删掉期间替换的文件 | 取消命令带 `expected_state/expected_prepared_sha256/expected_generation`；`_discard_owned_temp` 是唯一删除实现，DB 往返后再 probe+verify | 探针 `test_cancel_preserves_temp_replaced...` + S0 的 5 条原语 + S2 的 `test_a_replaced_staged_file_survives_the_cancel_round_trip` |
| RFA-07 P2 | `recover_local` 补偿里的二次写入逃出恢复循环 | `_settle_local_failure` 重读并按赢家状态收敛；`_refuse_cancel` 同样分类；只有账本不可读才上抛 | 探针 `test_local_recovery_fault_handler...` + S2 的交错/补偿 CAS/补偿写失败三条 + T 的 11 |
| 审计 P2-1 | 清理重试在持续负载下不可达 | 窗口保留重试位（`limit≥8` 时 `max(1, limit//8)`）+ tier0/tier1 双游标 + tier1 空则同调用内回绕一次 | `test_a_retried_row_is_reached_under_continuous_load`、`test_the_two_cleanup_cursors_advance_independently`、T 的 1 |
| 审计 P2-2 | 一页里不可复用观察超过 256 个时永不推进 | 负面记录（`UNREADABLE/UNKNOWN/MISSING`）120 秒内可复用，只用于 hold/repair，绝不当健康；`ROOT_UNAVAILABLE` 不放宽 | `test_a_page_of_three_hundred_unreadable_files_still_commits`、`test_a_page_of_three_hundred_missing_files_still_commits`、T 的 2/3 |
| 审计 P3-1 | carry 自带 rowid 扫描，绕开 handover | 删掉中间扫描，页链接之后直接走共享解析入口 | `test_carry_and_recovery_resolve_the_same_successor` |
| 审计 P3-2 | `_refuse_cancel` 竞争时逃逸成 `_StorageFault` | 捕获后走重读分类；`_classify_after_race` 不再二次写 issue | `test_a_refused_cancel_reports_a_competing_completion`、T 的 4 |

## 3. 契约演进

同一份 v3 文档里记录了五次修订：v3（三份接口冻结）→ v3.1（复用前必须 stat 复核对象身份）→ v3.2（清理窗口排序、诊断边界、补偿结果）→ v3.3（负面记录 120 秒窗口）→ v3.4（清理重试保留位与双游标、carry 顺序、`MISSING` 并入负面窗口）。

## 4. 代码与测试改动

产品：`core/observation_snapshot.py`（`ObservationKey/ProgressRecord/PageObservationProgress/PageBudget.max_slices`）、`core/library_repository.py`（计划/切片/复用/进度命令、负面窗口）、`core/file_presence.py`（新）、`core/page_delivery.py`（四态取消、单一删除实现、补偿收敛）、`core/delivery_cleanup.py`（`cleanup_is_terminal`）、`core/delivery_repository.py`（清理事实与窗口、carry 顺序）、`core/delivery_lineage.py`（事件优先的有界解析）、`core/delivery_recovery.py`（把解析原因带进结果）。

测试：`tests/test_u1a_rfa_boundaries.py`（审计 12 条原样）、`test_u1a_rfa_primitives.py`（5）、`test_u1a_rfa_observation.py`（24）、`test_u1a_rfa_delivery.py`（18）、`test_u1a_rfa_lineage.py`（26）、`test_u1a_rfa_scenarios.py`（25，独立验收）。

## 5. 验证（本轮实测，全部离线合成）

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 审计 12 条探针（原件逐字导入） | **12/12 通过**（修复前 10 红 / 2 绿） | 红基线 `red01/red-336541b.xml`；绿 `full01/rfa-all.xml` |
| RFA 全套 6 个文件 | **110 项：0 失败、0 错误、0 跳过** | `full01/rfa-all.xml`、`rfa-all.log` |
| 非 Tk 全量 | **1,831 项：1,827 通过、4 跳过、0 失败、0 错误** | `full01/full2.xml`、`full2.log` |
| Tk 两文件 | 逐条隔离 **9/9**；同一进程反复创建/销毁 Tk 解释器会 `TclError: Can't find a usable tk.tcl`（三次落在不同测试，失败记录保留） | `full01/tk-notes.md`、`tk-collection.xml`、`tk-source.xml` |
| 旧反例 | 15（`test_u1a_fix_boundaries`）+ 14（`test_u1a_rf_*`）全绿 | 含于全量 |
| 只读审计 | 222 例独立复跑全绿；0 P1、2 P2、5 P3；P2 与 P3-1/P3-2 已修，P3-3..P3-5 记录为边界 | 见 §2 表 |

4 个跳过仍是三处真实符号链接权限限制 + 一个 W 阶段登记，与本轮无关。

### 5.1 测量（同一脚本、同一合成样本）

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 后继解析 32 条：语句数 / 重复扫描 | 82,901 / 2,517 | **2 / 0**（事件路径） |
| 后继解析无事件路径 | 递归展开（8→329 … 32→82,901） | 恒 **2 条语句、1 次窗口扫描**；节点 = min(N,64)；64 条为 `budget_exhausted` |
| 解析耗时（32 条） | 2.5–3.5 s | <1 ms |
| 清理 `limit=1` | 返回已清理行、队列不动 | 返回未清理行、`next_rowid=61`、带游标确认前进 |
| 清理 `limit=200` | 160 条（含 60 已清理），40 位被坏证据吃掉 | **100 条**全部真实候选；检查 141 行、跳过 41 |
| `recover_local` 后候选 | 仍 160 条 | **0** |
| 根掉线 cleanup | True + `cleaned/already_absent` | **False** + `failed/root_unavailable` |
| 33 页作品 | 两次尝试各 32 次观察、checkpoint 恒 null | 一次受理 **committed**、33 观测/33 唯一、checkpoint 前进、复用链接 33→66 |
| accept 3×10 / 2,000 页 | 311 / 18,025 | 311 / 18,025 |
| claim 30 / 2,000 页 | 61 / 8,021 | 61 / 8,021 |

语句数、唯一 SQL、扫描次数、观测文件数、返回/删除条数、事件字段都是确定性指标，可直接比对；秒数与毫秒只作同机量级参考。

## 6. 已知边界（不在本轮修）

1. 写者侧没有 stat 身份复核：预检与写事务之间（毫秒级、同一次受理内）文件被替换时，写者只按时间/根版本/策略接受复用结论。
2. `root_state` 只判 `is_dir()`：同一路径换成另一块盘（或 Linux 空挂载点）仍被判 AVAILABLE。
3. 观察进度只读该 job 最近 200 条事件、最多 4096 条记录；读不到只是重新哈希。
4. `_settle_local_failure` 两次 CAS 都失败时返回 `recovery_needed` 作为结果标签，行本身未被改写。
5. 取消成功且 temp 已删除时不立即写终态事实，等下一轮 sweep 看到 ABSENT 再写 `cleaned/already_absent`。
6. 41 条坏证据行（`prepared_json` 不可解析）每次读取都会被重新检查：不阻塞新记录，但固定扫描成本存在。
7. 已登记遗留不变：`finish_source` 最终结果查询协议、`AdmissionTicket` 未实例化、意图投影/索引、carry 长事务、2,000 页作品 claim 约 8,021 条语句、真实迁移/备份/外置卷、W5 UI、completed 重新核验、旧 `record_manager` 健康门。

## 7. 复现

```powershell
$env:PYTHONNOUSERSITE='1'
& 'D:/Miniconda3/envs/JuiLiu/python.exe' -s -B -X utf8 -m pytest `
  tests/test_u1a_rfa_boundaries.py tests/test_u1a_rfa_observation.py `
  tests/test_u1a_rfa_delivery.py tests/test_u1a_rfa_lineage.py `
  tests/test_u1a_rfa_scenarios.py tests/test_u1a_rfa_primitives.py `
  -q -p no:cacheprovider --basetemp=<空目录>
```

测量（合成样本，脚本在 `.runtime/jpgpng_u1a_rfa_fix_20261114_01/probe/`）：

```powershell
$env:PIXIVKEEP_MEASURE_REPO='<集成工作树>'
& 'D:/Miniconda3/envs/JuiLiu/python.exe' -s -B -X utf8 `
  '.runtime/jpgpng_u1a_rfa_fix_20261114_01/probe/measure_rfa.py' --label postfix
```
