# RF 修复后问题清单

固定 `3839c90` / 产品 `b07c2ed`。编号 `RFA-01..07`，避免与上一轮 RF 编号混淆。P1 表示下一轮优先关闭的功能/记录风险；P2 表示明确但有条件的可靠性、性能或协议问题。下面的行号对应审计基线；代码链接指向该分支文件。

## 总表

| 编号 | 级别 | 问题 | 新反例数 |
| --- | --- | --- | --- |
| RFA-01 | P1 | 整页预算耗尽没有可续进展，默认 32 文件上限可使历史复用永远卡住 | 1 |
| RFA-02 | P1 | 根不可用被解释为文件不存在，已发布交付被取消、清理被虚报完成 | 3 |
| RFA-03 | P2 | 清理写入与查询协议不一致，已清理行占住重试窗口 | 2 |
| RFA-04 | P2 | 精确 handover 被更晚但无关的活动行盖过 | 1 |
| RFA-05 | P2 | 后继解析重复展开同一子图，4 跳限制不能限制总工作量 | 1 |
| RFA-06 | P2 | 取消写者往返之后按旧身份直接 unlink，可删掉期间替换的文件 | 1 |
| RFA-07 | P2 | recover_local 异常补偿中的状态竞争仍能抛出，未实现完整收敛 | 1 |

另 2 个绿色控制检查：验证前已经存在的外来 temp 会保留；gate-wins 语义仍通过。完整函数名和结果见[统计](evidence/results.json)。

## RFA-01：默认 32 文件预算使一个 33 页作品反复重来

位置：[library_repository.py](../../../core/library_repository.py) 2326–2378；[task_coordinator.py](../../../core/task_coordinator.py) 28、248–255；[follow_collection.py](../../../core/follow_collection.py) `_accept` 与 `_Budget` 处理。

`_PAGE_BUDGET = PageBudget()` 默认 max_files=32。预检把完整来源页作为一个不可分割输入；到第 33 个已有 artifact 前抛 `PreflightCancelled(page_file_budget)`，前 32 项观察没有可续缓存或受理进展。来源结束/再次 resume 后仍收到同一页，重新读取同一批文件。

实测先通过真实交付器保存一个 33 页合成 PNG 作品，再用默认预算进行可信复用，连续两批都是 `page_file_budget`；共观察 64 次，唯一 artifact 只有 32 个，checkpoint 始终没有前进。没有网络或慢盘参与。一个来源页包含多个多页作品时同样可能触发；并不限于超大图库。

这是预算限制与推进协议组合的缺口，不能靠加大 32、自动跳过整页或把“未观察”写成健康关闭。需要定义可续的观察进度，或者在保留原始 page receipt/作品全页完整性/断点原子语义的前提下拆分受理。完整观察保留 freshness、根版本和写者代次复核，不能永久缓存健康结论。

验收：至少覆盖 32/33/64 页、多个作品共页、多版本候选、暂停/恢复和预算耗尽后再次推进；全页提交前不前推来源 cursor，不把部分作品冒充完成。反例 `test_default_page_budget_can_progress_a_33_image_work_after_resume`。

## RFA-02：根掉线不是不存在的证据

位置：[page_delivery.py](../../../core/page_delivery.py) 471–529（取消）、583–613（清理）。

`_abort_unpublished` 只看 final.exists 和 temp.exists。根目录已掉线时两者均为 False；temp 不存在的分支没有证明 `temp_owned`，仍调用取消 CAS。`cleanup_cancelled_delivery` 也会把根掉线记作 `already_absent/cleaned`。下载开始时已有的根守卫并不能保护这些后续阶段。

三条合成实测：

1. prepare 后、发布门前掉线，归属证据不可读取，却得到 `cancelled/intent_excluded`，断开的目录中仍有一份 .part。
2. **已经 link 成正式原图，after_publish 故障导致 recovery_needed**；后续记录排除，在本地恢复时根掉线。交付被改为 cancelled，重连后 recover_local 返回空；正式图片仍存在，artifact 仍为 None。这违背“已发布不因后来的排除被取消”的既定契约。
3. 取消清理曾因文件锁失败，根随后掉线；cleanup 返回 True，重连后 .part 仍在。RFA-03 暂时使它仍在队列；修好排除标记后反而会被永久跳过，因此两项必须一起验收。

本轮没有实际拔盘，也没有真实丢图。三个场景通过限定在临时目录内的 rename 模拟根不可达；正式文件保留，但完成/清理事实错误。

修复要求：对 final 和 temp 采用“可访问且确定存在/可访问且确定不存在/无法观察”三态；取消与 already_absent 必须有根在线证据；OSError、权限错误、未绑定根、掉线均保留可恢复状态。不要在掉线时 mkdir 重建根；不得取消已发布行或据此删除正式文件。

## RFA-03：清理事实生产者与消费者字段不一致

位置：[delivery_repository.py](../../../core/delivery_repository.py) 516–572、1079–1124；[delivery_cleanup.py](../../../core/delivery_cleanup.py) 114–126。

候选查询排除包含 `"cleanup_completed":true` 的事件；实际 `_mark_cleanup_completed` 只写 delivery_id/batch_id/outcome/error_code。已有 `cleanup_event_payload()` 能生成所需字段，却没有接入写入路径。

真实 executor 完成清理后，事件为 `outcome=cleaned,error_code=already_absent`，同一个 delivery 仍被列为候选。`limit=1` 的两个合法取消交付中，第一条已清理，第二条仍进不了窗口；默认 200 条窗口是同一问题，不是超时。原幂等测试只检查事件数量，没有验证队列继续推进。

修复不能只补一个新字段：必须解释已有无布尔标记的 cleaned 事件，保持追加历史不重写；记录解析与选择采用同一协议。还要验证坏/无法清理的最旧记录不会永久饿死新记录，不能先 LIMIT 再静默过滤后一直重扫相同窗口。

反例 `test_cleanup_fact_written_by_real_executor_is_excluded_from_candidates`、`test_cleanup_window_advances_past_a_cleaned_delivery`。清理完成事实应绑定具体 prepared 身份；调用者传入 `outcome='cleaned'` 本身不是文件证据。

## RFA-04：后继选择未遵守精确 handover 优先

位置：[delivery_lineage.py](../../../core/delivery_lineage.py) 121–171；对照[契约 v2](../../optimization/2026-11-13/U1A_STATE_CONTRACT_V2.md) §2.1。

契约规定先查 `recovery_owner_transfer` 指定的 artifact，然后才用 rowid 后继。实现先遍历更晚行，并在发现 completed 或活动行时立即返回，最后才查事件。

实测通过合法 API 先产生跨任务 reuse handover，精确 artifact 指向更早的 completed；再创建无关的第三个 redownload pending。同一个被取代 hold 的 resolve_successor 返回第三任务 pending，忽略已经登记的 artifact。

这条测试直接验证共享解析 API；不能据此宣称全部 UI 续跑都会失败。carry 另有 page-link/旧 completed 回退，可能绕开本问题；但 recovery 读写两端使用共享解析结果，活动第三任务会进入 foreign-active 拒绝分支，语义因此与 carry 分叉。

明确一种权威顺序，统一 read/write/carry。事件目标缺失、损坏、质量不匹配时必须定义诊断和回退，不能无条件信任事件；精确 handover 也仍需当前字节核验。反例 `test_explicit_handover_is_authoritative_over_a_new_unrelated_pending`。

## RFA-05：有深度上限，仍有组合爆炸

位置：[delivery_lineage.py](../../../core/delivery_lineage.py) 148–158；[delivery_recovery.py](../../../core/delivery_recovery.py) 1159。

每个 superseded 行递归查询所有更晚同页行，没有 visited/memoization；同一后继被从不同路径反复遍历。即使已找到足够的 completed，循环仍继续展开。MAX_SUPERSEDE_HOPS=4 只限制调用深度，不限制调用总数。

| 合成 superseded 行 | SQL 数 | 本机秒数 |
| --- | ---: | ---: |
| 8 | 329 | 0.007 |
| 16 | 5,037 | 0.120 |
| 24 | 25,905 | 0.753 |
| 32 | 82,901 | 2.708 |

16 条样本中，同类 descendant 查询执行 **2,517 次，仅 17 种不同 SQL**。单次恢复读阶段和写事务内均可能执行解析，故会放大写者阻塞。样本按真实表结构复制合法 superseded 历史以测量规模，不声称实际用户目前已有 32 次取代。

先修 RFA-04 可消除本样本的精确事件场景，但无事件链仍须验证。应把本次操作读成有界图/候选集合，按唯一标识去重访问，对总节点/查询量给上限；超限返回明确未解析，不能凭空挑一个后继。不要只提高跳数或改 LIMIT。性能验收应以查询计数和响应性为主，不能把此机 2.708 秒作为跨机器阈值。

## RFA-06：写者往返后旧 temp 身份已过时

位置：[page_delivery.py](../../../core/page_delivery.py) 498–539。

executor 先 `_verify_owned(temp,evidence)`，随后同步等待取消事务，返回后直接 `temp.unlink()`。这段等待期间若文件被替换，删除仍按路径作用于新文件。

故障注入在取消 API 的边界替换 temp：新文件内容为无关字节，取消仍成功，替换文件被删除。作为对照，替换发生在初次归属检查之前时程序正确保留；因此不是没有身份校验，而是校验没有覆盖破坏性操作的实际时点。需要外部文件变化或另一执行路径竞争；单进程无外部修改的常规流程未复现此问题。

至少取消路径与重试清理共用一套核验/删除实现，DB 往返之后重新验证 prepared 指纹和实际文件身份。若目标是并发修改下的严格保证，必须设计并验证 Windows 句柄/独占访问或隔离后复核方案；单纯再加 exists/hash 仍留最终 TOCTOU 缝隙，不能宣称原子安全。无法证明归属时保留文件并记录失败。

## RFA-07：异常处理中的二次写入仍可越出恢复循环

位置：[page_delivery.py](../../../core/page_delivery.py) 754–761；[delivery_repository.py](../../../core/delivery_repository.py) 1037–1040。

`except _StorageFault` 能捕获 try 主体内的失败，捕获不到前一个 `except (_Fault,OSError,ContractError)` 分支内 `_db(set_delivery_issue)` 新抛出的异常。

确定性插入另一次恢复作为赢家：它完成 confirm；当前恢复随后遇到文件故障，补偿写尝试把 completed 设回 recovery_needed，仓库正确拒绝，但包装后的 `_StorageFault` 逃出 recover_local。赢家文件和 artifact 保持正确，失败方却中止整个恢复调用。这是同一仓库存在交错恢复调用时的 API 边界，不宣称常规单次启动必现。

对补偿也使用 expected-old/CAS，并重新读取赢家结果：completed 就报告 completed，取消/换主分别收敛；真实数据库不可用须报告 storage/trust fault，不能吞掉后谎称“仍可恢复”。若架构禁止并发恢复，应在 API 层显式互斥并验证，不能只靠当前启动时机假设。

## 不计入新产品缺陷的记录问题

README 仍描述 `3ee638f`；docs/README 与总实施页页首已写 `b07c2ed`，其当前总表和下方导航仍写 RF 待修；TASK_PLAN 的 active_workstream 指向新修复，但 audited_commit/evidence 和 unified_implementation 留在旧审计。这些已在本审计分支统一；原修复维护文、契约、历史 session 不改写。

AdmissionTicket 未实例化、finish_source 无查询协议、事件全扫、carry 长事务等已由开发者主动登记，作为后续工作保留，不包装为本轮新发现。
