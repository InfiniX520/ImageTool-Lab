# JpgPng U1-A / J-01 W0–W4 修复记录

日期：2026-11-13。分支：`fix/a1-u1a-j01-20261113`，从 `bb2cb7f`（产品代码 `dda6fc1`，schema 5）接续。评审批次：`JPGPNG-U1A-WIRING-20261113-01`。本批仍为离线合成验证，不接真实图库、生产库、账号、网络，不执行上传、删除或释放。

## 1. 结论

- J-01 的三条生产复用旁路已改为“当前文件 SHA-256 健康观察才允许复用”：跨任务 trusted、同 job completed、follow/名单 `_candidate_requires_write`。
- 删除、等长非图替换、同尺寸不同摘要 PNG 不再计为 `completed/existing`；缺失进入 pending 修复，变化进入 `needs_verification`；批次不能在这些页面未解决时结算为 completed。
- 未改动文件仍可复用；显式 `redownload` 仍可补齐并保留两代 artifact；旧 artifact、delivery、批次计数逐字段不变。
- W4 用追加式 `events(kind='removal_intent')` 接入 `EXCLUDED`/撤销；`OFFLOADED` 仍明确拒绝，未对外声称支持。
- 第二轮红队 F1–F8 已修复：resume/claim/carry 回灌、EXCLUDED 跨批、完整 carry 健康门、未绑定 resolver、needs_verification 出口。
- W5 恢复入口 `recover_verification_page` 已完成仓库层与 41 项测试；UI/CLI 与“批量恢复/全库巡检”仍未接线。
- W6 的 2,000 文件整链、第二批缺失修复、进程退出后重开子集在 `d8de8dc` 上通过合成证据（见第 6 节）；新提交 `dc3acbc` 的最终规模复跑由独立代理进行。
- 全量（不含 Tk）1,589 项、0 失败、1 个 Tk 环境错误、4 跳过；Tk 展示文件单独 4/4。
- **W5 的 UI/CLI/全库巡检、W6 的根不可用/链接/快速策略边界与迁移演练仍未完成。本批不是生产发布声明。**

## 2. 实现

| 工作包 | 落点 | 内容 |
| --- | --- | --- |
| W0 | `docs/audits/2026-11-13-u1a-wiring/W0_DECISIONS.md` | 状态/事务决定、复用判定表、根绑定、I/O 边界 |
| W1 | `core/artifact_health.py` | `ArtifactExpectation` / `ArtifactHealth` / `ArtifactHealthPolicy`；完整 SHA-256 才可复用；缺失/变化/不可读/根不可用/读取中变化/未知均不可复用；payload 严格往返；过期/根 revision 不匹配即失效 |
| W2 | `core/library_paths.py`、`core/library_repository.py` | `PathResolver.root_path/root_revision`；`bind_path_resolver`；预检只读快照先关闭，再做 `LocalObserver` 文件 I/O；check 记录冻结进 command payload，写者短事务复核 |
| W3 | `core/delivery_repository.py`、`core/library_repository.py`、`core/follow_repository.py`、`core/page_delivery.py` | 候选查询与健康门分离；三条复用路径接入；无根绑定/无健康记录 fail-closed；executor 构造时注册根 |
| W4 | `core/library_repository.py` | `record_removal_intent` 追加事件；`EXCLUDED` 抑制缺失自动补齐；撤销 `NONE` 后重新待修；旧记录保留；`OFFLOADED` 拒绝 |

关键语义：

- `hash_match`（完整 SHA-256、稳定身份/大小/mtime）→ `existing=1`，不派发。
- `missing` → 新 pending 交付代次，可修复；不覆盖旧 artifact/delivery。
- `changed` / `unreadable` / `root_unavailable` / `changed_during_check` / `unknown` → `needs_verification`，不自动联网、不覆盖磁盘。
- `EXCLUDED` 且不可复用 → `cancelled`（`intent_excluded`），pending/needs_verification 均为 0；撤销后才重新待修。
- 没有根绑定就**不**回退到旧的“数据库记录即复用”行为；这是 fail-closed。生产在 `PageDeliveryExecutor` 构造时绑定 `PathResolver`。

## 3. 测试证据

命令环境：`D:\Miniconda3\envs\JuiLiu\python.exe`，`PYTHONNOUSERSITE=1`，`-s -B -X utf8`，`-p no:cacheprovider`，独立 basetemp/XML。

| 证据 | 结果 |
| --- | --- |
| [W0 基线](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w0_baseline.xml) | 17 项、7 失败、2 跳过、0 错误（预期红） |
| [W1 健康契约](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w1_contract.xml) | 20 项全过 |
| [W2/W3 复用门](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w2_gate_tests.xml) | 6 项基线全过；复核后扩到 10 项 |
| [J-01 独立文件](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w4_j01.xml) | 16 过、1 跳过（W 阶段边界） |
| [复核后全量（不含 Tk）](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w17_full_no_tk.xml) | **1,589 项、0 失败、1 Tk 环境错误、4 跳过**，249.92 s |
| Tk 展示文件单独运行 | 4 项全过（当前环境；全量中 Tk/Tcl 初始化偶发环境错误，单独运行排除） |

新增/激活的 U1-A 测试：

- `tests/test_u1a_j01_health.py`：17 项，16 过、1 跳过；7 个原红全部转绿，8 个守卫保持绿。
- `tests/test_u1a_artifact_health.py`：20 项，状态机、payload、过期、根 revision、快速策略不升级。
- `tests/test_u1a_reuse_gate.py`：11 项，无根绑定 fail-closed、根绑定冲突、过期/伪造策略、I/O 不在 writer 线程、缺失/变化预算、同 job resume、claim-only 回灌、旧 needs_verification 解除、carry 完好页保持 existing。
- `tests/test_u1a_removal_intent.py`：6 项，EXCLUDED 抑制/撤销、接受后取消 pending、同 job 撤销、跨批 suspended 取消、结算后记录/重放、OFFLOADED 不虚假宣传。
- `tests/test_u1a_recovery.py`：41 项，显式恢复 API 的状态机、hash_match/missing/changed/root_unavailable、pending 幂等/park、command_id 重放、事务/线程边界。

测试调整说明：`tests/test_delivery_repository.py::test_all_pages_flag_is_not_page_byte_quality` 原先只写数据库记录、不写文件；J-01 后这不再是可复用状态。该用例改为写入真实 1,000 字节并绑定同一根，仍只验证 `all_pages` 不是页面字节属性，未放宽断言。另有两个旧断言随语义收紧更新：resume 重试完成后旧批 works 仍按 delivery join 重算；`needs_verification` 不再永久占用输出槽，新任务会把旧 hold 标为 `failed / superseded_by_new_task`。`test_query_collection` 的终态等待在 Windows 全量负载下由 8 s 放宽到 20 s（仍有界，且产品自身 coordinator 命令为 8 s）；这是测试稳定性调整，不改变产品超时。

## 3.1 独立复核发现与修复

独立代码复核与红队探针（只读，合成临时库）在 `3b63f9c` 上发现并复现：

| 编号 | 问题 | 处理 |
| --- | --- | --- |
| P1-1 | resume/claim 复制旧批 `batch_item_pages` 时覆盖当次健康判定，缺失页回到 completed | 复制改为 `ON CONFLICT DO NOTHING`；claim 前对源作品 completed 页做 health 预检；无健康证据的 carried completed 页新建 `needs_verification`，不再复制 artifact |
| P1-2 | changed 产生的 `needs_verification` 永久占用 `ux_deliveries_active_output`，后续任务 OutputConflict | 新任务对**其他 job 的 needs_verification** 置为 `failed / superseded_by_new_task` 后重试；pending/suspended/prepared 仍按输出冲突处理，不静默抢占 |
| P1-3 | 先 accept 产生 pending 再记录 EXCLUDED，pending 仍在 dispatchable 并会重建文件 | 记录 EXCLUDED 时同批同 output 的 pending/suspended/retry_wait 转 `cancelled / intent_excluded` 并刷新页面 |
| P1-4 | 同 job 撤销 EXCLUDED 后旧 cancelled delivery 被当作 existing 复用 | cancelled 不再走 existing 复用；撤销后新建 pending 代次并可修复 |
| P2-5 | 结算后不能记录意图；同 command_id 重放抛 InvalidState | 意图命令不再要求 running batch；先查 command event，结算后记录/重放可用 |
| P2-6 | 每页一次全表 `json_extract` 扫描 intent | 每个 SourcePage 只扫描一次并建最新映射 |
| P2-8 | 写者信任 payload 的 quick/max_age | `_health_map` 只接受与 `_ARTIFACT_HEALTH_POLICY` 完全相等的 policy；过期/错根/错行仍丢弃 |
| P3-9 | health 字节未计入 8 MiB admission | `size_payload` 纳入 health |
| P3-11 | `_confirm_delivery` 更新所有批次的页面链接 | 只更新 current batch 的 artifact link；旧批仅刷新 work 状态 |

仍未关闭：P2-7（预检仍在 accept 同步路径，源泵 20 s Future 超时/大文件预算）、P3-10（root_revision 恒为 1，跨会话 external 重映射）、P3-12（legacy `record_manager` 仍按记录跳过，未纳入 v2 健康门）。这些在 W5/W6 明确列为残余，不声称全产品 J-01 已关闭。

## 3.2 第二轮红队（F1–F8）

红队报告：`E:\Zero9\JuiLiu\.runtime\jpgpng_u1a_w5_20261113_01\redteam2\REPORT.md`。在 `d8de8dc` 上复现并已在 `3aeb642` 修复：

| 编号 | 问题 | 处理 |
| --- | --- | --- |
| F1 | 旧 job resume(retry_failed) 在别 job 占槽时抛原始 sqlite IntegrityError | 重置前检查同 output 活动槽；冲突抛 `OutputConflict`，事务回滚，不再泄漏 sqlite 异常 |
| F2 | claim 补建 carried NV 撞别 job 活动 NV，协调器空转 | 已有 NV 时链接该 hold 而非再插；其他状态冲突转 `OutputConflict`，不再原始 IntegrityError |
| F3 | EXCLUDED 只取消当前批，旧批 suspended 仍可派发并重建文件 | 按 **job + output_key** 取消 pending/suspended/retry_wait/needs_verification，跨批生效 |
| F4 | carry 只传 enforce 不传 health，完好 completed 变 NV 并逐批传播 | carry 调用线程对源作品 completed 页做完整健康预检；写者按实际 health 复制；完好页保持 existing，changed/missing 仍 fail closed |
| F5 | 未绑定 resolver 的 claim 复制旧 completed | 无 resolver/无目标页证据时按 fail closed 处理，旧 completed 不复制 |
| F6 | `enforce_health=False` 可关闭健康门 | 公共 carry 只接受 True，False 直接拒绝 |
| F7 | needs_verification 无出口，永久 partial/占槽 | 新增 `repo.recover_verification_page(batch_id, illust_id, page_index, *, command_id=None)`：hash_match 复用、missing 生成 pending 修复、changed/root_unavailable 保持 hold、旧行保留；预检在写事务外 |
| F8 | intent 事件按全局最新再比 revision，其他 revision 会遮蔽当前 EXCLUDED | 最新映射键加入 root_revision；当前 revision 查询不再被其他 revision 遮蔽 |

恢复 API 设计/契约见 [U1A_W5_RECOVERY_ENTRY.md](../optimization/2026-11-13/U1A_W5_RECOVERY_ENTRY.md)，41 项测试全过。F9（transferring 中记录 EXCLUDED）、F13（intent 全表扫描）、F14（损坏事件跳过）仍作为已知残余，不在本轮扩大。

## 4. 边界与残余

- 未接触真实图库/生产库/账号/网络；未上传、删除、释放或回读云端。
- 未做 UI/CLI 展示“核验于某时/状态/原因/重新核验/补齐/标记有意移除”；W5 待做。
- W6 的 2,000 文件整链、第二批缺失修复、进程退出后重开子集已做（第 6 节）；根不可用/链接/快速策略的 UI 边界与迁移演练仍待做。
- 快速策略只保留契约，未默认启用；完整 SHA-256 是当前唯一复用策略。
- `root_unavailable` 目前保守转 `needs_verification`，尚无单独观察投影/UI 原因；W5 待补。
- `OFFLOADED` 意图已拒绝写入，避免“记录了但没接线”的虚假能力；待 W5 设计转存核验后再开放。
- 用户意图已可在结算后记录；但事件外键挂在 job/batch 上，删除任务会级联删除意图，长期意图存储仍需 W5 设计。
- P2-7：预检仍在 accept 同步路径；大文件/多候选可能超过源泵 20 s Future 超时，W5/W6 需要独立执行器或分页预算。
- P3-10：`root_revision` 目前恒为 1；跨会话 external 根重映射尚未有持久版本。
- P3-12：legacy `record_manager` 仍按“记录即跳过”，不属于 v2 健康门；本批只关闭 v2 三条复用旁路。
- F9：transferring 中记录 EXCLUDED 仍可能完成并落盘；F13：intent 仍是 events 全表扫描；F14：损坏 intent 事件被静默跳过。三者列入后续 W5/W6 清理。
- `recover_verification_page` 已能单页恢复，但尚无 UI 按钮、批量恢复、跨会话缓存或清理旧 `.part` 的入口。
- `dc3acbc` 的 2,000 页/claim/carry/退出最终复跑由独立规模代理另行执行；当前第 6 节数字仍对应 `d8de8dc`。
- 未做真实迁移、备份演练或旧库升级；本批不新增数据库 schema 对象。

## 5. W6 规模与进程退出证据

合成临时库：`E:\Zero9\JuiLiu\.runtime\u1a_scale_ilv7d16b`，仅 70 B 合成 PNG；不接触真实数据。

| 场景 | 结果 |
| --- | --- |
| 首批 2,000 页 | 2,000 个 artifact，`pages.completed=2000`；批次 03:42:17–03:47:25（约 308 s，含 2,000 次发布/fsync） |
| 第二批 1 页删除 | `existing=1999, pending=1`，无新增 artifact；修复 0.176 s；结算 `completed`，`new_completed=1, existing=1999`，artifact 2,001 |
| 第三批 2,000 页全在 | 预检/接受 17.909 s，`existing=2000, pending=0, needs_verification=0`，无可派发，批次 `completed` |
| 进程退出 | 子进程接受 2,000 页后 `os._exit(99)`；重开 `existing=2000`，artifact 数不变（2,001），旧 artifact/delivery 逐字段不变，批次可正常结算 |

证据：[scale_verify_result.json](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/scale_verify_result.json)、[crash_probe_result.json](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/crash_probe_result.json)。上述规模数字在 `3b63f9c` 核心树上测得；复核后的 claim/carry 健康门与 intent 单扫描改了恢复路径，最终提交需再跑一次同规模（未在本批完成）。这是 W6 的规模/退出子集，不等于完整 W5/W6 验收；未做真实迁移、备份演练、根不可用/链接/快速策略的 UI 接线。

## 6. 复核入口

- [W0 决定](../audits/2026-11-13-u1a-wiring/W0_DECISIONS.md)
- [评审总览](../audits/2026-11-13-u1a-wiring/README.md)
- [开发指导](../audits/2026-11-13-u1a-wiring/DEVELOPMENT_GUIDE.md)
- [全量 XML](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w14_full_no_tk.xml)
