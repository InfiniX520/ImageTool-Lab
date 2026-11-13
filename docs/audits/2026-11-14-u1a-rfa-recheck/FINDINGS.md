# RFB-01..06：问题、证据与修复边界

版本 `f7c389b` / 产品 `48a6968`，相对 `26afa48` 审查。源码链接均以此冻结树为准。P1 表示核心质量判断错误；P2 表示推进、恢复或资源控制的实质缺口。未发现本轮反例直接删除原图；不能把“保留了字节”与“判断/流程正确”混为一谈。

## RFB-01 · P1：健康缓存漏检原地等长改写

位置：[library_repository.py:2847](../../../core/library_repository.py#L2847)，复用入口 [2756](../../../core/library_repository.py#L2756)，缓存消费 [2579](../../../core/library_repository.py#L2579)。契约 v3 §1.3 第 6 条及解释段存在错误的职责归属。

触发：真实 executor 完成两页 → 新任务以 1 文件/1 切片观察第一页并保存进度 → 原地写入另一张等长有效 PNG（不换 inode）→ settle/resume 新 batch → 默认预算继续。同一测试分别保留/不保留原 mtime。

实测：两种情况均仅观察第二个文件，结果 `completed=2, existing=2, needs_verification=0`。预期第一张应核验为 changed 并 hold，第二张可复用。文件的 device/inode/size 没变，但 SHA 已改变。

根因：`_progress_object_is_unchanged` 只有 stat 身份与大小，旧 HASH_MATCH 直接进入本次 snapshot；trusted 复用仅建立 artifact 链接，没有新交付，因此不会运行 `_verify_owned`、发布或确认。检查 mtime 可以降低常见漏检，但不能把恢复了 mtime 的等长异图变成严格摘要通过。

修复要求：把历史观察缓存与本次严格字节证据分开。健康缓存只能提示候选；若不能证明文件不可变/持有有效保护，当前受理的严格复用需重新验证选中内容。不要把新 HASH_MATCH 改名为 QUICK_MATCH 后仍按严格成功统计，也不能去掉哈希以满足性能。必须与 RFB-02 一起设计，避免“每次全重哈希→又卡前 N 个”。已经提交 receipt 的幂等重放是不同语义，仍可零哈希返回历史事实。

反例：`test_cached_hash_cannot_approve_an_in_place_equal_size_edit[False/True]`。

## RFB-02 · P2：进度丢失不是仅仅变慢，会无法推进

位置：[library_repository.py:2542](../../../core/library_repository.py#L2542)（close_slice），[2691](../../../core/library_repository.py#L2691)（加载上限），[2733](../../../core/library_repository.py#L2733)（持久化失败吞掉）。

反例采用 6 张真实合成 PNG，1 文件/片、2 片/次，连续 8 次 settle/resume。两种故障注入：

| 条件 | 8 次受理结果 | 实际观察 |
| --- | --- | --- |
| 将保留条数缩至 3，保持生产的最新优先截断算法 | 全是 `page_slice_limit`，无提交 | 16 次哈希，仅覆盖 5 个不同 artifact，第 6 个始终没到 |
| `record_page_observation` 持续拒绝写入 | 全是 `page_slice_limit`，无提交 | 16 次哈希，仅覆盖头 2 个 artifact |

这是缩小容量的算法反例；没有宣称实测 4096 条缓存上限。生产默认最多保留 4096 条进度、一次最多 256 个 fresh 观察，每页又可有多份历史 artifact（最多 8 候选/页），所以合法输入候选数可以超过“保留容量 + 本次可新增数”。按固定顺序重新观察和最新优先淘汰会反复淘汰前缀；`truncated` 没参与进展决策。200 条事件窗口和观察过期也可能降低可用进度，不能统一描述为“只多哈希”。

写入失败时即使没有截断，已经做完的切片也没有可恢复事实；但 close_slice 仍照常用尽并抛同一个可重试预算结果。进度实际上是多次调用能前进的前置条件，而非可有可无的加速器。

修复要求：先规定支持容量、可续状态与非可续错误。正常支持范围应能完成；若进度持久化/容量不支持，必须给明确的不可继续原因并保留 cursor，不得建议同输入无限重试。每次 `page_slice_limit` 必须能展示有效、可接续的净进展。最终一片已覆盖所有项目时不要仅因计数正好到上限再次停止。容量错误与 transient retry 要分开。反例当前按支持路径要求提交；若 S0 决定故障时明确拒绝，必须增加并审查对应的 typed 结果及控制面验收，不能只把断言放宽到“任意异常”。

反例：`test_missing_progress_cannot_turn_a_large_page_into_an_endless_retry[cache_cap/progress_write]`。

## RFB-03 · P2：检查限额被误当成清理队尾

位置：[page_delivery.py:939](../../../core/page_delivery.py#L939)–953；上游 [delivery_repository.py:694](../../../core/delivery_repository.py#L694)–720。重试 tier 自己的空结果回绕也需一起审查。

`_scan_cleanup_tier` 返回空 facts 有两种原因：确实 EOF，或检查到 examine_cap 但这些行全坏。返回值没有区分。`_sweep_cleanup` 只看 rows 为空且 cursor 非零就将双游标清零，丢掉已经扫描过的前缀。

实测：先用真实取消生成 prepared 样板，合成坏 JSON 前缀再接一条合法清理记录。limit=1 使用 11 条坏记录；默认 limit=200 使用 2001 条坏记录。连续 5 次 recover_local，合法尾项的访问次数都是 0；新行游标分别固定在 6、1001。默认窗口也复现，不只是把 limit 缩小造成的测试现象。

修复要求：返回结构至少区分 next_cursor、exhausted/EOF、budget_exhausted，并且按 tier 独立维护。预算到点保留游标，下次继续；只有已证实 EOF 才回绕。检查工作量仍有界，不能改为扫完整张表来让反例变绿。新行/失败重试保留位继续有效；没有重试行时不能无故损失有效容量。

反例：`test_cleanup_sweep_does_not_treat_examine_budget_as_end_of_queue[1/200]`。

## RFB-04 · P2：有界窗口漏掉中间节点的显式 handover

位置：[delivery_lineage.py:180](../../../core/delivery_lineage.py#L180)、[window_successor:323](../../../core/delivery_lineage.py#L323)；受影响公开入口 [delivery_recovery.py:413](../../../core/delivery_recovery.py#L413)。

结构：C 是早期 completed/artifact；A 是后来的待修复页，已由 B 取代；B 又记录 reuse handover 指向 C。A 没有直接 artifact 事件，B 是 A 后面的 cancelled/superseded 行，而 C 的 rowid 早于 A。

实测：直接解析 B 能找到 C（`reason=event`）；解析 A 只报告 `superseded_only:1`、`successor=None, reason=none`。恢复 C 的 PNG 后，公开 `recover_verification_page` 对 A 返回 `refused/superseded_unresolved`，没有跟到已存在的有效 artifact。

测试说明：C/初始 repair 来自真实 executor/repository；中间取代行与 handover 由合成 SQLite 构造，随后使用真实 resolver 和公开恢复入口。证明的是合法保存结构的解析缺口，不宣称已在真实用户库生成过此链或所有任务都会永久卡住。

根因：窗口扫描只计数 superseded，不展开这些节点的事件；`supersede_chain` 只是“前面出现过 cancelled”的标签。移除递归确实改善复杂度，但同时丢掉旧解析器能跟随的边。`reason=none` 还把“没有继续检查这条边”表述成没有后继。

修复要求：有界节点集合里批量读取并解析相应 handover，按权威顺序遍历一次；边/节点/SQL 都设预算，超预算返回 `budget_exhausted`。显式坏目标不能静默跳到其他 completed。读预检、writer CAS、carry 共用同一结果与拒绝语义。禁止恢复指数递归；禁止在调用方另添“随便找一条 completed”补洞。

反例：`test_superseded_intermediate_event_can_lead_to_an_older_completed_artifact`。

## RFB-05 · P2：无法读取账本仍报告普通可恢复

位置：[page_delivery.py:613](../../../core/page_delivery.py#L613)–616，[recover_local:873](../../../core/page_delivery.py#L873)–876。

两条独立路径：

1. 补偿写失败后 `_classify_after_race` 再读 ledger；读也失败却直接返回 `(recovery_needed, fallback_code)`。
2. 公开 recover_local 中，publish_authorized 抛 CorruptDataError → `_db` 转 `_StorageFault` → 被每行 catch 吞掉，仅返回 recovery_needed，继续循环；prepared 行与字节保留，但结果没有存储故障分类。

实测两个反例均未收到本应区分的存储异常。第二条输出 `((id, recovery_needed),)`，数据库行仍 prepared。没有伪造 completed，也没有丢失字节；问题是调用方无法区分正常可恢复状态与不可信/不可读账本，无法按既定契约执行阻断。

契约 v3 §2.6 自身有矛盾：伪代码写“except _StorageFault 后继续”，后文又写“真正账本不可读上抛 storage/trust fault”。下一轮必须修正文档与实现，不能任选一段解释全部通过。

修复要求：保留合法 CAS 失败的赢家收敛；读通但写拒绝可报告未持久化的 recovery_needed；读不通则沿所有调用路径传播 typed 故障，或明确的结构化 fatal 结果并使控制面停止派发。不能仅修私有分类器，外层 catch 同样要接好。保留所有 prepared/.part，禁止通过再次猜写状态掩盖失败。

反例：`test_unreadable_ledger_is_not_reported_as_a_recovery_classification`、`test_recover_local_surfaces_an_unreadable_publication_ledger`。当前冻结契约选择上抛；若改结构化 fatal，需同时更新调用方与对应等价断言，经主审确认，不能变成一般 recovery_needed。

## RFB-06 · P2：单文件不受声明的切片字节限额控制

位置：[library_repository.py:2586](../../../core/library_repository.py#L2586)–2647，[slice 检查:2677](../../../core/library_repository.py#L2677)，[独立单文件预算:2911](../../../core/library_repository.py#L2911)。

实测一个真实已保存 PNG（70 bytes），`PageBudget(max_bytes=1)` 仍完成接受，而非明确不可推进的 page_byte_budget。这不是只超出一块缓冲：page 预算完全没有传到 LocalObserver，后者仍使用独立 4 GiB/30 秒预算。fresh_bytes 只在整个文件观察完后累加；byte/time 限额只在下一个文件开始前检查，末文件之后没有检查。默认 slice 为 512 MiB，允许的大文件与该上限也不一致。

修复要求：先冻结 max_bytes/max_seconds 是硬 I/O 限额还是文件间调度阈值。目前契约 §1.1 明确保留“单 page_spec 自身超限”结果，所以当前静默提交不符合该约定。若保留硬预算，向流式观察传剩余资源和取消信号；文件自身无法放入时显式拒绝，不能无限空切片。若确需软阈值，应明确最大超调、单文件硬上限与取消响应，并同步调用者/测试，而非事后把所有预算解释为提示。

反例：`test_a_single_file_cannot_silently_exceed_the_declared_slice_byte_budget`。时间边界在代码上存在同类检查空档，但本轮只实测字节反例，没有伪称测得具体超时/暂停延迟。

## 非新增缺陷的保留项

- events 的 LIKE 查询/全表扫描、carry 长事务和 claim 语句数仍存在；本轮没有重做真实规模基准，不能仅以语句数常量宣称所有成本常量。
- root_state 的挂载身份、最后一次 hash→unlink/accept 的竞态、finish_source 最终结果查询、completed 重核验、W5 接线、真实迁移与回退仍未证明。
- 统一导航修正是文档整理，不计入上述 6 个产品缺陷；历史材料保留原日期和结论。当前状态应从最新总入口读取。
