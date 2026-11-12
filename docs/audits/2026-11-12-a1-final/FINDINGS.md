# A1 后续实现独立审计：问题明细

日期：2026-11-12。首轮基线 `6790565e02330cd8b8af0a35789ecba85882caa3`；随后在 `705a99081cbf89e84033280c5f6b5930c7e08b1f` 独立副本复核收尾差异。以下状态以后一提交为准，旧反例和关闭理由分别保留。本稿只审新增实现与其共享影响；旧下载器问题另见联合计划，不因为本轮测试通过而关闭。

P1 表示阻断 A1 实际接线的正确性问题；P2 表示需要在对应能力开放前解决的契约、规模或交接问题。当前没有真实图库损坏的证据，下面的实际文件和数据库均由审计新建。所有反例、完整结果与错误尝试集中在[本批证据](../../../../.runtime/jpgpng_a1_finalaudit_20261112_01/README.md)。

**最终状态：8 项开放（4 P1、4 P2），另有 AR3-07/08 两项已被收尾提交修复并复验关闭。**

## 结论表

| ID | 级别 | 问题 | 建议工作包 |
| --- | --- | --- | --- |
| AR3-01 | P1 | 相同字节被当作同一逻辑资产，独立文件后续分叉发生版本冲突 | N1 |
| AR3-02 | P1 | 写入没有真正比较解析时的旧绑定；无事务调用还能留下部分事实 | N2 |
| AR3-03 | P1 | 最新诊断观察被当作已确认物理身份，记录检查就能绕过替换确认 | N2 |
| AR3-04 | P1 | 旧根版本观察被重标为新版本；实际观察到计划的可信交接仍可绕过 | N3 |
| AR3-05 | P2 | Windows 路径别名在键、原始拼写和持久化之间不一致 | N1 |
| AR3-06 | P2 | 新增逐文件查询缺少合适索引，历史库增长导致二次执行量 | N4 |
| AR3-07 | 已关闭（原 P2） | 非 planned 空 receipt 反例在 705a990 被明确拒绝 | N5 保持回归 |
| AR3-08 | 已关闭（原 P2） | checkpoint 忙返回已检查，长读反例明确报错 | N5 保持回归 |
| AR3-09 | P2 | 当前进度、历史报告与最终源码没有统一对应，审计时还有并发修改 | N0 / N6 |
| AR3-10 | P2 | 合并后的共享 writer 提前翻译异常，归档专用 busy 类型处理失效 | N5 |

## AR3-01：相同字节不应合并逻辑资产

位置：[find_known_content / resolve_observed_content](../../../core/archive_schema.py)、[resolve_content_version](../../../core/archive_scope.py)。

新路径没有绑定时，解析器按 SHA-256 和大小从整个 `archive_contents` 取第一条 `ContentVersion`，直接复用其 `asset_id/revision`。这里把“相同字节”当成了“同一逻辑资产”。每条路径之后又根据自己的当前 revision 加一，两种规则不相容。

独立实测 `probes01/results.json → duplicate_contents_diverge`：

1. 新建两个不同物理文件 A、B，内容都是 `AAAA`。
2. A 首次建立 revision 1；B 被复用为 A 的同一 asset/revision。
3. 原地将 A 改为 `BBBB`，保存 revision 2 成功。
4. 原地将 B 改为 `CCCC`，解析仍给出同一 asset 的 revision 2；写入报 `Content identity already has different facts`。

这不需要伪造数据库、不需要并发，也不是摘要碰撞。未知历史基线得到了保护，但普通独立文件的后续演化被阻断。硬链接两个路径也需要明确独立路径与共同物理对象的关系，不能靠跨资产任意挑一条内容行解决。

修复方向：内容去重键与逻辑资产身份分离。相同字节可以共享 blob/容量统计，但不能仅凭摘要接管另一资产的身份；同资产版本分配与分叉策略必须明确。禁止为过测覆盖旧 `archive_contents`、删除路径 owner 约束，或只把 revision 改成全库最大值而不解释资产语义。

验收：不同 inode 相同字节、硬链接、复制后独立修改、A→B→A 内容回转、同名替换，旧计划全部保持原内容；同字节去重统计仍正确。

## AR3-02：expected-old 检查失效及事务边界缺失

位置：[apply_content_resolution](../../../core/archive_schema.py)、[bind_archive_path](../../../core/archive_schema.py)、[save_prepared_plan_in_transaction](../../../core/archive_plan_store.py)。

`apply_content_resolution` 虽然取得 `resolution.previous_content`，实际绑定时却把**刚读到的当前 key**传给 `bind_archive_path(expected_content_key=...)`。这只能验证“当前等于刚读到的当前”，没有验证解析期间是否被别人更新。`previous_content=None` 的首次绑定预期同样没有检查。计划保存路径也直接按当前版本比较大小后推进，没有携带真正的预期绑定。

实测 `lifecycle01/results.json → real_stale_absence_overwrites_new_binding` 使用实际观察和公开解析器，没有手改 resolution：

1. A 顺次产生 revision 1/2/3。
2. 新文件 B 的字节等于 A 的 revision 3，队外解析得“预期 B 未绑定、拟复用 revision 3”；暂不提交。
3. B 改成 revision 2 的字节，另一次实际观察先完成并建立 B 的 revision 2 绑定。
4. 再应用第 2 步的旧结果，程序仍成功推进 B 到 revision 3；B 实际摘要和当前绑定摘要不同。

补充 `probes01/results.json → stale_cas` 用显式旧 expected 做故障注入，也被接受。这条是参数损坏反例，不能和上述真实工作流混写。

此外 `apply_content_resolution` 没有检查 `conn.in_transaction`。在 autocommit 连接中，用已存在的 check_id 引发观察写入失败，前面的内容/资产行已经提交：`archive_contents`、`archive_assets` 从各 2 行增加到各 3 行，绑定仍旧。证据 `autocommit_partial`。这不是完整事务中的回滚故障，而是公开低层接口没有执行其“调用者持有事务”的前置条件。

修复方向：将 expected binding（含“必须不存在”）作为不可变解析输入，写事务开始后先比较，随后再写任何事实；不以当前值替代 expected。同请求重放要有独立判据，不能“版本更大就接受”。低层写接口无事务时立即拒绝，仓库提供唯一受控命令；发生冲突不自动覆盖或无限重试。

验收：真实两连接交错、预期不存在已变成存在、旧版→新版竞争、同版本不同摘要、重复 check_id、每个写阶段异常。失败前后全部相关表和当前绑定相同；旧请求重放仍有明确幂等行为。

## AR3-03：诊断观察不是替换授权

位置：[load_content_binding](../../../core/archive_schema.py)、[record_local_observation](../../../core/archive_schema.py)、[resolve_content_version](../../../core/archive_scope.py)。

`load_content_binding` 从 `archive_local_checks` 按路径取最新一条非空物理身份，没有限定当前绑定、根版本、接受状态或绑定事件。因而一次失败/changed 检查也会改变“上次认可的物理对象”。

实测 `lifecycle01/results.json → recording_changed_observation_bypasses_confirmation`：

1. 文件 A 已建立基线并绑定原物理对象。
2. `os.replace` 换成 B，实际读取 B；解析正确返回 `needs_confirmation / physical_object_replaced`。
3. 仅调用现有 `record_local_observation` 保存这次 `changed` 诊断，没有确认替换，也没有更新路径绑定。
4. 对同一次观察重新解析，变成 `new_revision / same_physical_object_content_changed`，确认要求消失。

这是两种独立事实被混用：记录“我看到了 B”，不能表示“用户同意 B 接替 A”。当前测试辅助流程是在 apply 时才记观察，因此没有覆盖正常诊断先落库的顺序；这也是接线前必须明确的契约。

修复方向：当前绑定显式引用最后一次**被接受**的物理证据或绑定事件；诊断流水只追加，不能隐式改变该引用。比较还要绑定 root revision 和逻辑资产。不要简单只挑最新 hash_match：首次基线本身是 unknown，合法的已确认替换也可能是 changed；需要显式接受事实。

验收：缺失、unreadable、changed、取消、旧根版本、同时间不同 check_id、已确认替换与未确认替换均分别测试，诊断写入不得改变授权结果。

## AR3-04：观察根版本被重新标注，交接仍依赖调用者自律

位置：[observe_candidate](../../../core/archive_scope.py)、[ObservedFile](../../../core/archive_scope.py)、[rebind_observation](../../../core/archive_scope.py)、[_receipt_json](../../../core/archive_plan_store.py)。

实测 `probes01/results.json → stale_observer_root`：候选 root_revision=2，实际 `LocalObserver` root_revision=1。读出的证据明确写 1，但解析接受，`rebind_observation` 用候选字段重新生成证据，最终库里是 2。旧观察被升级成了新根的观察。run 参数与候选 run 也未有一致性检查；如果两者本来代表不同阶段，应明确区分，不要继续使用含混的同名字段。

`ScopeReceipt v2` 对物理身份的绑定确实比上版更好，已绑定 receipt 也拒绝改摘要。不过**未绑定的 receipt**仍能在 prepare 阶段根据任意清单绑定摘要，完全不要求真实 observation：新建 `AAAA`，把传入 ContentVersion 的摘要填成 64 个 `f`，save/load/audit 都成功，观察表 0 行。证据 `auto_bind_unobserved`。

后一项不是密码学攻击，也不说明现有代码已将 planned 标为云端已验证；`content_verified` 仍为 false，source 为 user_selected。它说明目前的计划 API只能证明自洽，不能作为“这些摘要来自实际读取”的产品入口。705a990 新增 `archive_observed_plan.prepare_observed_plan`，将 ContentResolution 转成清单/容量/PreparedArchivePlan，能汇总冲突和未确认项；这是有效的纯转换层。但它不读取文件、不接收 ObservedFile、不写数据库，仍不是候选→实际观察→接受事件→唯一 writer 的完整应用服务。

**最新入口的自然交接反例：**`latest_integration01/result.json → new_observation_old_receipt`：先枚举 AAAA 得旧 receipt；将同 inode 文件等长改成 BBBB，重新枚举并真实观察得到新的 snapshot_id/mtime；把新 resolution 传给 `prepare_observed_plan`，却提供旧 receipt。入口仍接受，新 SHA-256 被写进旧 snapshot/旧 mtime 的 receipt。该接口把候选转为 ArchiveEntry 时丢掉 snapshot_id 和 mtime，后续只比路径/身份/大小。这里没有伪造摘要或直接构造 resolution，属于两个合法流程结果的错误配对。

修复方向：检查原始观察的 path、root revision、run/check、大小、mtime、物理身份、字节数和摘要；字段只能验证，不能用候选值覆盖来消除不一致。给生产接线准备一个小型应用入口，强制完整观察与决策记录；纯计划层允许未验证输入时必须有明确未验证类型/状态，下游不能当成已观察事实。增加 HMAC 不会补齐遗漏字段或漏掉的调用。

验收：旧 observer+新 candidate、旧 run、换根但相同物理文件、任意摘要清单、同路径同大小不同物理对象、观察后变化。真实整链不再需要调用者手写 replace(ContentVersion) 或绕过 writer 直接写表。

## AR3-05：路径键与原始拼写没有形成一致别名模型

位置：[PathRef.identity_key](../../../core/task_contracts.py)、[ensure_archive_asset](../../../core/archive_schema.py)、[ScopeEnumerator](../../../core/archive_scope.py)。

两个真实 Windows 反例：

| 输入 | 结果 |
| --- | --- |
| 先保存 `gallery/A.bin`，再通过 `gallery/a.bin` 实际观察同一个文件 | identity_key 相同、解析 REUSE，但 immutable path_json 大小写不同，apply 报 `Existing asset has different immutable facts` |
| `gallery/画A.bin` / `gallery/画a.bin` 分别单独选择 | `os.path.samefile=True`，两次都 scope_complete，但产生两个不同 identity_key；一起选择时才被物理去重 |

第一条见 `probes01/results.json → alias_reselect`；第二条见 `supplement01/results.json → mixed_alias`。原 `probes.py` 对第二条误用了参数名 explicit_files 而失败，这次工具使用错误保留，不充当产品反例；补充脚本已用实际 `files` 参数重做。

按段 ASCII casefold 修好了“画/A vs 画/a”这一种，但混合字符位于同一段时仍不一致；单纯改成全 Unicode casefold 又会破坏 NFC/NFD、straße/strasse 等真实不同文件。键与原始 path_json 也不能各自决定别名语义。

修复方向：在受控根内依据真实目录项和物理事实解析规范路径/别名，持久化稳定身份与恢复拼写；显示路径不替代身份。旧计划保留旧拼写/引用。不要全项目批量替换 target_key，不要合并真正不同的 Unicode 文件或删掉硬链接恢复路径。

## AR3-06：目录 I/O 修好后，数据库出现新的二次成本

位置：[find_known_content](../../../core/archive_schema.py)、[load_content_binding](../../../core/archive_schema.py)、[ensure_archive_asset](../../../core/archive_schema.py)，及 schema 的索引集合。

现有摘要查询按 sha256/size 查找，观察查询按 path_key 排时间，owner 查询按 path_key 找资产；索引分别缺少这些可用于直接查找的前缀。实际 `EXPLAIN QUERY PLAN` 显示 SCAN，部分还建临时排序 B-tree。

对三份独立合成 schema-3 库，每库填 n 条合法内容/路径/观察，再执行 n 次对应查询。用 SQLite progress handler 每条 VM 指令计数，避免单凭墙钟判断：

| n | n 次不存在摘要查询 VM 指令 | n 次当前绑定查询 VM 指令 |
| --- | ---: | ---: |
| 100 | 31,104 | 45,269 |
| 200 | 122,204 | 170,469 |
| 400 | 484,404 | 660,869 |

证据 `supplement01/results.json → sql_growth`。这些查询在 400 条的小库仍很快；问题是增长趋势及累计历史库，并不声称已测得 500 GB 的实际耗时。2,000 文件正向实测观察约 20.73 s、解析/应用约 4.27 s，也不能掩盖以后多批积累的扫描成本。

修复方向：先完成 N1/N2 的身份与绑定语义，再为真实访问方式加索引/批量查询；明确实验 schema 迁移或版本拒绝，不能在同版本下悄悄变 DDL。用 n/2n 的历史库和固定大小新批次分别测；不能以关闭摘要核验或删历史来加速。

## AR3-07：非 planned 状态审计缺口（收尾修复已复验关闭）

**最新结论：**`705a990` 已在非 planned 状态调用 `_decode_scope`、容量和来源/资产验证；`probes_latest01/results.json → nonplanned_receipt` 返回 `ArchiveSchemaError`。以下是早先 6790565 上的反例，保留追溯，不再计作开放缺陷。生命周期其他状态的具体可用操作仍需后续设计。

位置：[validate_archive_facts](../../../core/archive_schema.py)。

当前 planned 计划的 receipt、段、来源摘要与引用内容闭包已经复用完整加载校验，这是应当保留的改进。但只有 `state == planned` 才调用完整协议校验。

实测 `probes01/results.json → nonplanned_receipt`：合法计划仅改 state 为 completed 并将 receipt 设 null，全库审计仍接受。普通 load 由于 state 不是 planned 而拒绝；这不能证明 completed 的证据有效。迁移和维护依赖全库审计时，该状态分支会漏检。

修复方向：不可变计划文档校验与可变生命周期校验分开；所有携带本格式计划的状态验证 header/segments/receipt/content/source，状态转换再检查应有的包和检查记录。未实现状态可明确拒绝，不按 state 跳过真实性/一致性字段。保持无计划的其他合法批次类型有清楚的格式分派。

收尾差异待复核：旧 FR-05 的部分清单计数已有单独修复提交；不得以本项继续指控已修的计数。读取条目上限、组合载荷上限、非法时间戳包装及旧协议拒绝也应列入 N5 的一致性矩阵，区分已经实测通过与仅静态建议。

## AR3-08：checkpoint 忙返回被忽略（收尾修复已复验关闭）

**最新结论：**`ef9038f` 已检查 checkpoint 返回值，备份摘要也改成流式；在 705a990 重跑长读者反例，明确抛出 `ArchiveMigrationError: Archive migration checkpoint could not drain WAL`，不再回报成功。原脚本为旧版成功路径编写，最新退出 1 是修复后的预期拒绝，日志保留在 `wal_latest01.log`。以下为 6790565 的旧反例。

位置：[migrate_archive_copy](../../../core/archive_schema.py)，特别是末尾 `PRAGMA wal_checkpoint(TRUNCATE)`。

连接用 closing 关闭的修复有效；正常无读者场景已经能留下完整主文件。但程序没有读取 checkpoint 返回的 busy/log/checkpointed 三个值。

实测 `wal01/result.json`：

1. 新建合成 v1 库，另开一个原始 SQLite 只读事务保持旧快照。
2. 调用现有迁移，返回 `migrated`、target 3。
3. 同时新连接带 WAL 读到 schema 3，但复制**主文件本身**到另一个新文件后读到 schema 1，原 WAL 仍有 251,352 B。

活库并未因此损坏；SQLite 正常把新提交保存在 WAL。问题是不能从迁移成功推导“主文件已独立可搬迁”。该场景违反“调用者保证所有连接已关闭”的意图，却没有被应用锁拦住——普通读者不会持有 writer.lock。F6 旧报告也明确没有覆盖长读检查点饥饿，不能拿那 61 项通过来关闭本边界。

修复方向：检查 checkpoint 结果；busy 时返回明确的“迁移已提交，主文件尚未独立化”状态或保守错误，保留数据库、WAL 和备份，关闭读者后有界续做。需要可移植副本时用 SQLite backup 和重开核验，不删除 WAL 强迫通过，不宣称已回滚已提交的迁移。

## AR3-09：进度和证据需要一次收口

审计开始时，TASK_PLAN 写 F1–F6 offline complete，但 evidence/latest_overview 仍指上轮 1310 通过的报告；联合入口仍写 8 个旧问题，实施日志还写 F3 运行中。F5 的 2,000 文件和 F6 的 61 项报告固定在 `4a9c3f3`，F3 的校验哈希又对应其未提交中间树，不能自动代表 `6790565` 或之后提交。

本轮还实际遇到并发写入：起初看见 `f516bb1`，冻结时已有 `6790565`；随后归档源码和测试继续变化。已中止在可变树上的 full01（退出 -1，不计通过），改用独立 detached worktree 完整审计。用户确认另一窗口尚在收尾，最终提交须另行核对。

整理措施见[总览](README.md)：本批只有一个证据根和三份主报告，旧报告/失败样本均保留；当前导航和机器计划用新结论覆盖“当前入口”，不篡改旧审计的历史测试数。版本归档与发布分开；不能因为保存到 Git 就说 A1 已验收或 A2–A6 已完成。


## AR3-10：并行修改分别正确，合并后异常协议不再相容

位置：[共享 writer](../../../core/library_repository.py)、[ArchiveLibraryRepository.save_archive_plan](../../../core/archive_repository.py)。

`8a7002e` 在共享 writer 内将 sqlite3.OperationalError(BUSY/LOCKED) 翻译为 `LibraryBusyError`。归档仓库的 save 路径仍只捕获 sqlite3.OperationalError 再翻译为 `ArchiveLibraryBusyError`；异常在到达它之前已经变了类型。两种错误不处于同一继承链，依赖归档专用类型的调用者无法识别。

`latest_integration01/result.json → archive_busy_type` 用真实原始 SQLite 连接持有 BEGIN IMMEDIATE，临时将被测 writer 超时设为 100 ms，沿 ArchiveLibraryRepository.save_archive_plan 调用：实际收到 `LibraryBusyError`，`isinstance(..., ArchiveLibraryBusyError)==False`，cause 是 OperationalError。释放锁后同一 writer 保存成功。不是数据库损坏，但错误处理契约发生了真实合并回归。

修复：选定一个明确异常协议；归档边界适配共享 busy 类型，或统一公共基类并回归调用方。用 SQLite 错误码区分锁竞争与磁盘/I/O/约束错误，保留 cause，不能字符串匹配后把所有 OperationalError 都当可重试。分别测试 v1 和 archive 的 save/load/启动路径，真实锁超时后 writer 可继续使用。这里没有必要再次改造整个队列或引入重试循环。

## 额外观察：日志测试有时序依赖，不能用反复跑绿掩盖

705a990 第一次全量：1,391 项中 1,387 通过、1 失败、3 跳过。失败是 `tests/test_log_view.py::test_window_dual_limits_hold_and_removals_are_reported`，预期一次 drain 得 30 行，实际为 0。`LogView.drain` 默认工作预算 8 ms，测试使用真实 monotonic，但无条件要求所有行一次取完。该文件独立复跑 17/17 通过。

这是与调度/负载相符的测试不稳定迹象，不足以证明用户日志永久丢失，也不能称第一次全量通过。建议容量计数测试注入现有可控 clock；真实期限/帧预算单独测试，多次有界 drain 应最终收全。禁止为了测试把产品的 8 ms UI 预算取消。第二次全量结果见总览，首份失败 XML 保留。
