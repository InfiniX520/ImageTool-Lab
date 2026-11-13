# 问题、复现与判定依据

版本固定在 `3ee638f`。下列行号均为该版本一基行号。P1 表示应在继续集成前修复的工作流/恢复阻断；P2 表示必须补齐的可靠性边界。没有 P0，也没有发现真实图库受损的证据。

## RF-01 · P1 · recovery_needed 后排除使本地恢复抛错

位置：[page_delivery.py](../../../core/page_delivery.py#L485) 485–500、637–653；[library_repository.py](../../../core/library_repository.py#L3606) 3606–3611；[task_coordinator.py](../../../core/task_coordinator.py#L363) 363–368。

传输完成、prepare 已提交，最终发布前发生本地故障，交付成为 `recovery_needed`。随后用户记录 EXCLUDED，再调用 `recover_local()`：发布门正确拒绝，但 `_abort_unpublished` 调 `cancel_unpublished_delivery`；后者仅允许 `prepared`，拒绝 `recovery_needed`，经 `_db` 包装为 `_StorageFault`。recover_local 未捕获此类型。启动 coordinator 会调用 recover_local，因此此组合可以使启动恢复失败。

反例 `test_excluded_recovery_needed_can_finish_local_recovery` 输出 `_StorageFault recovery_needed`；最终名不存在、`.part` 保留。它通过真实 executor 的 `before_publish` 故障钩子制造状态，无需损坏数据库。

修复必须覆盖“未发布且有归属证据的 recovery_needed”，同时区分已 link 未 confirm 的 recovery_needed。不能只放宽 SQL 状态集合，误取消已发布文件。验收须包含 prepared/recovery_needed × final 不存在/属于自己/外来文件 × EXCLUDED/无排除/意图不可读。

## RF-02 · P1 · 外任务接管成功后，原任务续跑失去恢复路径

位置：[delivery_recovery.py](../../../core/delivery_recovery.py#L973) 973–1000、1053–1119；[delivery_repository.py](../../../core/delivery_repository.py#L1432) 1432–1442。

恢复把外任务 NV 置 `cancelled/superseded_by_recovery_*`，只重链当前接管批次。原任务仍指向取消的交付，且没有可供后续 carry 使用的 successor 解析。原任务续跑时复制这个取消引用，恢复按 `unsupported_previous_state` 拒绝；即使图片已正确恢复，原任务页统计仍为 cancelled。

`test_foreign_handover_does_not_strand_original_owner[reuse/repair]` 两项均失败：接管方分别复用/完成补下载，原任务续跑后均为 `refused unsupported_previous_state`，pages.cancelled=1。上一轮只测到“接管方成功”，没有继续测试“原所有者再续跑”。

需要区分“用户取消”和“被后继交付取代”，对新的活动批次解析可核验后继关系；历史批次/收据保持原事实。禁止通过批量重写历史链接或把所有 cancelled 当可恢复来修。

## RF-03 · P1 · accept 遇全库坏意图后仍启动后续任务

位置：[task_coordinator.py](../../../core/task_coordinator.py#L177) 177–190、335–341、422–426；[collection_runtime.py](../../../core/collection_runtime.py#L235) source runner 异常处理；[library_repository.py](../../../core/library_repository.py#L2471) 2471–2487。

worker 的 `claim_delivery` 异常分支会 `_dispatch=False`，但 observer 的 accept 异常只设置 Future。CollectionRuntime 收到异常后调用 `request_block(..., 'source_runner_failed')`；coordinator 完成 blocked 结算后仍保留 `_dispatch=True`，于是激活下一任务。全库意图损坏的问题会逐任务重演，来源线程也可能继续发元数据请求。

反例 `test_accept_corruption_stops_activation_of_later_jobs`：在临时库注入一个坏意图事件，accept 抛 CorruptDataError；按生产 source runner 相同步骤 block 后，下一个任务从 queued 变为 running。输出 `CORRUPT_ACCEPT_ACTIVATION True running`。本反例没有实际请求网络。

需要统一的“全库信任故障”传播路径，覆盖 accept、claim、恢复与发布；保留确切诊断，停止整轮激活，不只在某一个工作线程里关开关。普通单图失败不应被升级为全库停机。

## RF-04 · P2 · 无观察的 held 仍可派发

位置：[delivery_recovery.py](../../../core/delivery_recovery.py#L211) 211–220、386–389、766–772。

带实际 ROOT_UNAVAILABLE 观察的 pending 已修成 PARK。但 resolver 未绑定或观察器无法产生记录时，返回 `held + NONE`，写者走 NONE 直接返回，pending 状态不变。

两个反例分别模拟观察返回 None，以及**直接重新打开临时库且未绑定 resolver**；后者不需 monkeypatch。均为 held，pending 仍出现在 list_dispatchable。接口称“挂起”与调度事实矛盾。

必须区分“没有历史 artifact、普通新下载尚未开始”和“有历史事实但无法核验”。前者可以保持 repair_queued；后者要真实停车或明确 refused，不能 held 却仍派发。不要伪造 MISSING 观察来获得修改权限。

## RF-05 · P2 · negative 恢复证据仍未绑定 artifact 行

位置：[delivery_recovery.py](../../../core/delivery_recovery.py#L423) 423–426、875–901。

提交 payload 仅在 matched 时携带 artifact_id。repair/park 的 negative 观察通常为 artifact_id=None，写者只有 selected 非空才查询 artifact 并调用 expectation.matches_row；因此 negative 分支跳过了承诺的行绑定。

`test_negative_recovery_binds_the_recorded_artifact` 分别将真实 missing 观察的 expectation.artifact_id 改成不存在值、sha256 改成其他摘要、size_bytes 改为另一大小；时间、策略、页面键均合法。三者都生成 `repair_queued`，旧 hold 被退休。它们是接口抗错误/损坏证据测试，不是声称正常观察器会主动伪造摘要。

repair/park/reuse 应统一携带被观察 artifact 的身份，再分别验证 verdict；不以“本次不能复用”为由丢弃证据的来源行。验证失败不得退休旧 hold、占新输出槽或消耗预算。

## RF-06 · P2 · 暂停 admission 依赖锁外的旧 phase

位置：[task_coordinator.py](../../../core/task_coordinator.py#L104) 104–125；[library_repository.py](../../../core/library_repository.py#L2580) 2580–2587。

accept 先在 `_guard` 外读 running，再进锁入观察队列。若 durable pause 在两步间完成，入队仍带 admitted_running=True；写者允许它在 pausing 下提交。暂停结算判断依赖 qsize/_observing，也无法看见这个尚未登记的 accept。

`test_pause_admission_cannot_be_based_on_a_stale_database_read` 用 Event 固定读 phase→pause 提交→入队顺序，另固定 settle 的边界，结果仍接受源页并生成 pending。没有靠 sleep 碰概率。这里只测得该 admission 漏洞，不把所有暂停后完成都判错：已经正式受理且登记的在途工作可以按契约收尾。

修复应把“受理资格、在途登记、暂停边界”作为一个协议，使用每批 admission 代次/票据；不能靠一个公开 bool 或仅加一遍 phase 查询代替。另应测试队列已取出、_observing 尚未增加的空窗，避免 settle 提前完成。

## RF-07 · P2 · owner 异常退出后，新线程不退出

位置：[task_coordinator.py](../../../core/task_coordinator.py#L153) 153–176、431–447、449–480。

owner finally 设置 `_shutdown`，但 observer/control 的空队列退出只检查 `_closing`。启动恢复失败或运行中 owner 致命异常而尚未显式 close 时，两个新增非 daemon 线程永久轮询；owner finally 也只 drain 老队列。

反例 `test_fatal_owner_stops_the_new_service_threads` 注入 recover_interrupted 的启动异常，owner 已退出、shutdown 已置位，新增两线程仍存活。最后显式 close 清理，测试不留挂起进程。

统一 fatal/close 退出信号和四类队列的 Future 完成规则，处理未启动、启动部分失败、关闭超时、重复 close。不能用 daemon=True 隐藏泄漏，也不能在线程仍可能用库时提前关闭 writer。

## RF-08 · P2 · 控制响应已改善，但暂停不取消剩余哈希

位置：[library_repository.py](../../../core/library_repository.py#L2280) 2280–2307、2369–2376、1267–1306；[task_coordinator.py](../../../core/task_coordinator.py#L177) 177–194；[follow_collection.py](../../../core/follow_collection.py#L113) 113–118。

ObservationRequest 已有 cancellation 能力，接线未传入；每个候选使用独立预算，没有整页累计预算。`_ACCEPT_PREFLIGHT_SECONDS=2` 只在某次预检结束并触发 RetryPreflight 后检查，并不限制一次预检耗时。把哈希移到新线程让按钮能响应，但真正的暂停/关闭仍可能等完整候选页读完；source runner 仍用 result(20)，超时也不撤销已受理工作。

`test_pause_cancels_remaining_file_observation`：第一张在观察边界停住，pause 已返回后再释放，仍开始第二张，输出 `OBSERVATIONS_AFTER_PAUSE 2`。没有为证明这一点真的读数 GiB；最大规模总耗时未实测，不能写成一个确定秒数。

下一步接入取消 token、整页累计字节/时间预算和明确的超时后状态查询。取消/预算耗尽不是文件损坏，不能转为需要补下载的健康判定。已有 20 秒 Future 等待与重试预算需一起设计，单独放宽超时无效。

## RF-09 · P2 · 已取消交付的 .part 没有清理重试入口

位置：[page_delivery.py](../../../core/page_delivery.py#L495) 495–522、637–640；[delivery_repository.py](../../../core/delivery_repository.py#L499) 499–505。

排除路径先提交 cancelled，再核验并 unlink 自己的临时文件。若进程在两步间中断，或 unlink 因临时文件锁失败，残留被保留；但 list_recoverable 只查 prepared/recovery_needed，cleanup_delivery 又只允许 completed。注释称“可恢复”，实际没有现成重试入口。

`test_cancelled_owned_temp_can_be_cleaned_after_transient_unlink_failure` 仅让一次 `.part` unlink 抛 PermissionError，之后恢复权限；recover_local 返回空，cleanup_delivery=False，仍留一个已知属于本交付的 .part。用户空间有限，反复排除会积累不再清理的完整下载。

应有可重试的 cleanup pending 事实/查询，删除前重验证精确归属，外来字节永不删。禁止 rglob 全盘删 `.part`；清理完成不等于交付成功，也不能凭临时文件存在重新发布被排除内容。

## 记录与验证缺口（不计入 9 项产品缺陷）

1. 入口漂移已在本审计分支修正文档导航；上一轮维护页的 full03/tk04 勘误见 README，历史证据仍保留。
2. Tk 设置变量后首轮仍报 tcl_findLibrary 初始化错误，单项和整组复跑通过；现有证据不足以归因本轮产品改动，也不足以称环境永久修好。
3. 发布读门后排除仍完成符合当前 gate-wins 解释，最终是通过探针，不计缺陷；严格“直到 link 前都可取消”是另一个协议。
4. F13 意图全表扫描、carry 长事务、真实卷/迁移/回读、旧记录路径和 W5 UI 等已登记遗留，不能冒充本轮新发现或已完成。
