# U1-A 独立问题明细

基线 `8b1c751`，产品代码 `d3c5cf8`。定位行号均对应该基线。P1 表示进入日常运行前优先修复；P2 表示完整 U1-A 验收前修复或明确隔离。没有发现已发生的真实图库损坏，本轮反例全部来自合成资料。

| 编号 | 优先级 | 问题 | 证据性质 |
| --- | --- | --- | --- |
| UA-01 | P1 | 下载完成与下一页预检竞争，完好图片被降为待核验 | 现有全量失败 + 确定性交错复现 |
| UA-02 | P1 | EXCLUDED 读取跨任务，取消/恢复却只处理部分任务 | 2 项公共接口复现，实际假下载重建文件 |
| UA-03 | P1 | 根不可用时返回 held，但 pending 可继续下载并重建根 | 公共接口 + 假 executor 实际落盘 |
| UA-04 | P2 | 旧版本摘要已匹配，恢复仍使用第一个 changed 结论 | 公共接口，多代真实合成 PNG |
| UA-05 | P2 | carry 链接外任务 hold，恢复又拒绝其所有权 | 公共接口跨任务接续 |
| UA-06 | P2 | repair/park 没有与 reuse 对称的观察验证 | 时间失效注入；源码确认其他分支缺检查 |
| UA-07 | P2 | 文件预检占用唯一控制循环，暂停/查询被堵 | 有界 I/O 闩锁，无大文件或长等待 |
| UA-08 | P2 | carry 在每个作品循环里重新解析整个健康列表 | 计数探针 + 两类来源源码 |
| UA-09 | P2 | 已接受的同页重放仍重读完整图片 | I/O 调用计数探针 |
| UA-10 | P2 | 损坏的移除意图被跳过，变为允许补下载 | 合成库故障注入；既有 F14 的独立确认 |
| UA-11 | P2 | 传输中接受 EXCLUDED 后仍发布最终文件 | 假流式响应回调；既有 F9 的独立确认 |
| UA-12 | P2 | 当前进度入口和实际开发树脱节 | 文档/分支核对，本轮已整理入口 |

## UA-01：同批完成交错产生虚假的待核验代次

**位置：** `core/library_repository.py:2024`、`:2642`、`:2676`、`:2764`；控制入口 `core/task_coordinator.py:141`。

流程：第一来源页给作品安排 pending；下一来源页再次包含同一作品。预检只选 `state='completed'`，此时第一次下载尚未完成，所以 health 为空。写者处理第二页前，第一次下载完成并提交 artifact；接受路径重新看到 completed，却拿不到本次 health，便新建 `needs_verification / artifact_unknown`，覆盖本批 page link。文件完好、下载一次，批次仍变成 partial。

原全量排行榜用例正好触发这一交错。合成库 `integrity_check=ok`；作品 100 第一代 completed、第二代 needs_verification，不是 Windows 单纯调度慢。独立用例 `test_completion_between_preflight_and_accept_does_not_create_a_false_hold` 无需 sleep 即复现。

**修复条件：** 候选/完成代次改变时，返回明确的“需重新预检”，由事务外有界重试；不能直接把新完成行视为健康，也不能用 unknown 持久污染已完成页面。保持原收据、来源 cursor 原子性与只下载一次。补同批、跨批和两个重叠来源的交错测试。

## UA-02：移除意图的作用域不一致

**位置：** `core/library_repository.py:2244`、`:2319`、`:3097`；`core/delivery_recovery.py:767`。

`_removal_intents_for_page` 按作品/页/output/root_revision 读取全库意图；`_record_removal_intent` 却只取消记录入口所属 `job_id` 的排队交付。用户从已完成的旧任务标记 excluded 时，另一个任务此前排好的 missing 修复仍是 pending。claim 和 recovery 没有重新检查当前意图。

两条确定性反例：

1. 历史任务 A 完成 → 用户删图 → 任务 B 接受 pending 修复 → 从 A 记录 excluded → B 仍执行 1 次假 GET，文件重新出现，交付 completed。
2. B 已是 changed hold → 从 A 记录 excluded → 文件移除 → 对 B 调 recover → 返回 `repair_queued / artifact_missing`。

用例分别为 `test_excluded_from_historical_task_stops_other_task_pending_repair` 与 `test_excluded_from_historical_task_prevents_recovery_repair`。

**修复条件：** 明确并统一意图键及有效范围；既然任务接受使用全库意图，取消、claim、repair 和发布授权也必须遵守同一作用域与最新意图代次。撤销必须显式，不能把“点击恢复”当作默认撤销 excluded。

## UA-03：held 结果仍可派发，离线根被重建

**位置：** `core/delivery_recovery.py:208`、`:693`；`core/library_repository.py:1371`；`core/page_delivery.py:197`。

先生成 missing 的 pending 修复，再把合成输出根移到同一临时目录里的 offline 位置。恢复观察正确识别 `root_unavailable`，返回 `outcome=held`，但 `RecoveryAction.NONE` 保持 delivery 为 pending，仍出现在 `list_dispatchable`。真实 executor 路径会 `mkdir(parents=True)`，重建原根，再执行假 GET 并完成下载。

`test_pending_recovery_with_root_offline_is_really_held` 同时证明状态矛盾与实际重建，而非仅检查枚举值。外置盘卸载/目录临时不可用的实际系统表现会因平台不同，本轮没有操作真实磁盘。

**修复条件：** 对 pending 的 root_unavailable/unreadable/unknown/changed_during_check 等不可用结论，必须真正 park 为不可派发状态；返回字段与持久状态一致。根的“原先绑定位置暂不可用”和“首次初始化输出子目录”分开处理，不能靠 mkdir 当作恢复外置根。补模拟根掉线和实际外置卷小样门槛。

## UA-04：选中了匹配旧版本，却按首个不匹配版本作决定

**位置：** `core/delivery_recovery.py:587`、`:344`。

同一路径先后保存 A、B 两个合法 PNG 并保留两代 artifact；后来文件 changed 导致 hold，用户从备份放回 A。`_observe_recovery_page` 找到 A 的 hash_match 后返回 `(primary=B_changed, chosen=A_match)`。调用者把 `primary.state` 传给决定函数，于是仍返回 `held / artifact_changed`，结果甚至携带匹配 A 的 artifact ID。

用例：`test_recovery_uses_matching_older_artifact_when_latest_version_differs`。

**修复条件：** 决定、展示和事件证据必须基于实际选择的匹配观察；如没有匹配，另返回有明确优先级的不可用结论。不能只换 outcome 文案，需核对结果内摘要、时间和 artifact ID 属于同一观察。

## UA-05：跨任务 hold 共享后没有恢复出口

**位置：** `core/delivery_repository.py:1461`、`:1472`；`core/delivery_recovery.py:681`。

为避免 active_output 唯一约束冲突，carry 遇到其他任务的 needs_verification 会直接把当前批页面链接到该 delivery；恢复随后要求 delivery.job_id 与 batch.job_id 相同，返回 `page_owner_mismatch`。临时两页作品（一页完成、一页失败），另一任务建 hold，再恢复原任务即可复现；即使把正确文件放回也无法在当前批恢复。

用例：`test_carried_foreign_hold_can_be_recovered_by_its_current_batch`。

**修复条件：** 分开“页面属于哪个批次”和“哪个任务拥有活动输出槽”。可用只读阻塞引用或明确的转移协议，不能裸链接一个自己永远不能操作的 hold。禁止直接去掉所有权检查或抢占另一任务的 transferring/prepared 槽。

## UA-06：只有 reuse 做了完整观察校验

**位置：** `core/delivery_recovery.py:369`、`:700`、`:713`、`:767`。

命令的 `health` 只在 chosen 存在时提交，missing/changed 的 repair/park 把观察留在 draft，写者不验证其新鲜度、策略、根 revision 和原 artifact 绑定。把 missing 观察时间改成一小时前（当前 max_age=900 秒）仍得到 repair_queued，并退休旧 hold；同样过期的 hash_match 会被拒绝。

用例：`test_recovery_rejects_stale_missing_evidence`。这是对时间窗口的故障注入，不表示磁盘读取自然花了一小时；正常入口仍存在排队延迟与根变化窗口。此次未发现旧文件被覆盖，executor 的冲突保护仍在。

**修复条件：** 所有产生写入/派发授权的动作都携带观察 token、expected-old page/delivery/intent/root 代次，写者复核；失效时不退休旧 hold，不写“已修复”。repair、park、reuse 不能各维护一套松紧不同的证据协议。

## UA-07：哈希阻塞协调器控制命令

**位置：** `core/library_repository.py:1234`、`:2024`；`core/task_coordinator.py:141`、`:252`；`core/follow_collection.py:113`。

accept_source_page 在调用线程同步哈希，生产调用线程正是唯一 coordinator owner。I/O 期间 snapshot/pause Future 均不完成；虽然 pause 可立即置 stop Event，观察器没有收到该取消信号，数据库暂停和控制响应仍被堵住。单 artifact 预算允许 30 秒/4 GiB，source accept Future 等待为 20 秒；多文件没有统一命令预算。

用例：`test_hash_preflight_does_not_block_coordinator_control_commands` 只用 150 ms 有界闩锁证实两个 Future 必须等 I/O 释放，不声称实测了 500 GB 或具体生产吞吐。它独立确认了既有 P2-7，并给出控制线程根因。

**修复条件：** 有界观察执行器与控制 owner 分开；整批预算、取消传递、在途命令状态及超时后的结果归属要一起设计。增加线程数、放宽 20 秒、只迁出 SQLite 写线程都不算解决。

## UA-08：carry 写事务内重复解析全量健康列表

**位置：** `core/follow_repository.py:178`；`core/query_repository.py:415`。

每个 item 都调用 `_health_map(payload['health'])`。W 个作品、H 个观察形成 W×H 的反序列化/时间/根校验；三作品三观察实测 `[3,3,3]`，应一次解析三条。既有规模报告的 200 作品/2,000 页对应 400,000 次记录解析机会，10.8 秒事务不能简单归因于磁盘。

用例：`test_carry_parses_the_batch_health_map_once`；query 路径经源码确认同结构，本轮计数运行走 follow。

**修复条件：** 每命令解析一次、按 artifact/页索引；事务仍在使用点检查所需代次/有效期。大的 carry 拆为可接续有界事务，不能无期限锁住 writer；用解析次数与总时间共同验收，禁止仅移动定时器。

## UA-09：幂等页重放重复全文件读盘

**位置：** `core/library_repository.py:1242`、`:2358`。

完整预检发生在检查本批来源页 receipt 之前，同一已接受 SourcePage 调两次，返回相同结果却哈希两次。规模重放、恢复重试会放大无用 I/O，违反 W0 中“同批同 artifact 只观察一次”的明确约定；现有 skip 也承认该缺口。

用例：`test_same_page_receipt_replay_does_not_hash_again`。

**修复条件：** 对完全相同的已接受命令先走只读重放快路径，写者仍作权威幂等校验；一般缓存另绑文件身份、size、mtime、根版本、策略与本批代次，不永久相信历史 hash。不同 page 或文件被修改后的核验不能被这个优化跳过。

## UA-10：意图事件损坏时静默放行

**位置：** `core/library_repository.py:2256`–`:2268`。

`_removal_intents_for_page` 遇坏 JSON、缺键或非法意图直接 continue。合成库中把一条有效 excluded 事件损坏后，下一任务正常产生 pending 修复。不能从“无法读懂用户决定”推导“用户未排除”。

用例：`test_corrupt_intent_does_not_silently_allow_automatic_repair`；独立确认既有 F14。

**修复条件：** 返回可诊断的损坏错误或保守阻断相关范围，保留原事件；不要删除坏行后默默继续。后续做带索引的当前意图投影与追加历史，校验投影来源及迁移回退；不能靠内存 dict 掩盖持久记录损坏。

## UA-11：传输中移除意图未传到发布门

**位置：** `core/library_repository.py:2325`；`core/page_delivery.py` 的 prepared/publish 协议；`core/delivery_repository.py:834`。

流式假响应在 transferring 时调用 record_removal_intent(excluded)，返回成功后传输继续，最终文件出现且 completed。现有取消列表排除 transferring/validating/prepared，发布前也无 intent revision 检查。

用例：`test_exclusion_during_transfer_prevents_publication`，独立确认既有 F9。不是要求数据库强制把 prepared 改 cancelled；那会破坏已经公开文件后的恢复协议。

**修复条件：** 定义排除的线性化点。若排除先被接受，则合作停止/发布门阻止落盘；若发布已先完成，命令明确报告“本次已完成，仅影响后续”，不得伪称已经阻止。需覆盖 validation、prepare、rename、confirm 与进程退出的交错。

## UA-12：版本/进度入口落后于实际实现

修复分支增加健康与恢复模块后，TASK_PLAN 和 LOG 仍以最初 J-01 评审为最新条目，联合实施入口仍停留 AR4；维护记录中旧版本规模段、最终规模段和“待独立代理复跑”同时存在。日常主目录、默认 main、最新功能分支也不是同一版本；更名说明未合入该功能分支，仍显示旧私有仓库称谓。

本轮已在报告分支整理当前入口、机器计划、日志和名称说明，保留历史原文并标明其版本。审计报告分支未自动替换主目录源码。后续每个工作包必须写清 base/product/docs/final commit、测试命令、数量、未通过项及下一步，最终由唯一集成人维护当前入口。
