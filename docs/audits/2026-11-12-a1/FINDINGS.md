# A1 审计问题明细

审计日期：2026-11-12。范围、限制和测试统计见[总览](README.md)；执行顺序见[开发指导](DEVELOPMENT_GUIDE.md)。12 项均未完整关闭；A1-12 的文档状态已整理，代码和测试修复尚未进行。行号对应本轮冻结的源码，后续改动后以函数名和快照为准。复现均使用无敏感内容的合成文件或临时数据库。

统一证据：[probe_results.json](../../../../.runtime/jpgpng_a1_audit_20261111_01/probe_results.json)、[probes.py](../../../../.runtime/jpgpng_a1_audit_20261111_01/probes.py)、[当前测试 XML](../../../../.runtime/jpgpng_a1_audit_20261111_01/existing_tests.xml)。下列 case 名可在 JSON 内直接检索。

## A1-01 / P1：事务归属冲突，writer 草稿尚不能保存

位置：[archive_repository.py](../../../core/archive_repository.py)，82–96、109–137 行；[archive_plan_store.py](../../../core/archive_plan_store.py)，522–529、546、事务结尾；[library_repository.py](../../../core/library_repository.py)，932 行起的 _enqueue。

**已复现**：ArchiveLibraryRepository._dispatch 先进入继承的事务，再调用 save_archive_plan。后者要求连接不处于事务中，并自行 BEGIN/COMMIT/ROLLBACK。首次保存必然触发 `Archive plan save requires ownership of a new transaction`。4 项当前测试因此失败，无法到达它们原本要验证的重放、并发和损坏处理。

不能仅删除 in_transaction 检查：保存函数仍可能提交外层事务，破坏唯一写入者的命令原子性。应划分“准备不可变计划”和“当前事务内执行 SQL”，writer 拥有事务；独立临时连接入口可以用单独的外层包装器。内部 SQL 函数不得自行提交。

同一适配器还存在以下静态缺口，当前被前述失败遮挡：

- 保存前用只读连接算 expect_replay，再入队强行比对。两个相同首次请求都可能看到不存在；第二个串行执行时应当成功重放，却会被当冲突。重放判定必须在唯一写入事务内完成。
- size_payload 仅包含 batch_id、scope 和布尔值，没计入 inventory/capacity。继承的每命令/总排队字节限制无法约束实际计划负载；不能据“队列有上限”声称内存已受控。
- scope 字典引用直接入队，调用者可在排队后修改。冻结数据类也不自动冻结其内部可变对象。应在入队前校验、复制并冻结实际内容，再以同一冻结内容计费和写入。
- 111、135 行使用 `with self._readonly_connection()`，没有显式 close。SQLite Connection 上下文管理的是事务，不负责关闭连接；依赖 GC 释放会使句柄和锁生命周期不确定。使用 closing 或 finally.close，并测试异常分支。[Python 3.12 官方说明](https://docs.python.org/3.12/library/sqlite3.html#how-to-use-the-connection-context-manager)

验收重点：相同计划并发首存仅创建一次，其余返回同一结果；不同内容同 ID 必须冲突；内部失败整批回滚；调用者变更原字典不能改变已入队命令；关闭后无遗留读连接/写线程；实际负载纳入预算。

## A1-02 / P1：显式范围绕过敏感和输出排除

位置：[archive_scope.py](../../../core/archive_scope.py)，765–790、804–827、932 行起；case：sensitive_explicit_bypass。

同一合成根目录放 pixiv_config.json、my-cookie.txt、archive_store/nested/package.7z。递归选择根时，三者正确被排除；改为显式 files 选择，它们全部进入范围且 full=true。选择 prefix=archive_store/nested，也会纳入 package.7z。

原因：显式文件路径直接进入 _capture_file，没有应用文件排除；显式目录前缀只检查末段名称，没有检查被排除的祖先目录。调用方式改变了保护规则。

风险是将来封包误收授权文件或自己的归档输出；本轮没有真实敏感文件被打包。仅修 UI 不够，底层显式选择仍必须执行同一保护策略。实现统一资格判断，检查规范的根内相对路径、每个祖先、文件规则和实际配置的输出根；递归、显式文件、显式前缀都调用它。排除需要有原因，不能悄悄消失；普通用户选择不能关闭凭据/自身输出的保护。不要只硬编码 archive_store 名称而漏掉自定义输出目录。

## A1-03 / P1：Unicode 去重可漏掉不同原件

位置：[archive_scope.py](../../../core/archive_scope.py)，78–79、751–756、833–836 行；case：unicode_distinct_files。

在本机 Windows 合成目录实际创建 é.png 和 é.png（预组字符与 e+组合重音）。两个文件有不同物理身份和 SHA-256。枚举只返回一个，将另一个记 duplicate_candidate，仍宣称完整。

根因是 NFC+casefold 被同时用作排序键与文件等价性依据。文本显示相同不证明文件相同。现有 Unicode 测试只有单个变音文件及 ASCII 大小写选择，没有覆盖这组实际共存文件。

应保留原始相对路径；排序规则与身份规则分开。确认为同一实际文件/相同目标路径的别名才允许合并。遇到内部 PathRef 键无法表达的不同物理文件，应明确冲突并保留两个候选，不能选择一个后报完整。后续可扩展路径键表达能力，但不能顺手改动整个下载器的 PathRef 语义；先做影响分析。硬链接可以共享内容，恢复时仍要保留所有路径别名，释放容量也不能按别名重复累计。

## A1-04 / P1：分页完成语义没有贯穿到计划保存

位置：[archive_scope.py](../../../core/archive_scope.py)，275–300 行；[archive_inventory.py](../../../core/archive_inventory.py)，498 行起；[archive_plan_store.py](../../../core/archive_plan_store.py)，350 行起及保存前校验；case：partial_page_saved_as_complete。

3 文件、每页 1 文件的范围，第一页 has_next=true，但 completion.full=true，因为它描述的是后台快照已枚举完。只观察第一页的文件，构建 inventory 并附上“快照 3、已消费 1、尚有下一页”的 scope 字典，保存仍成功；重载后的 inventory.scope_complete=true，只有 1 文件。

单独看快照 full 的定义未必错误；错误在于下游没有证明收到全部页，scope 又是任意 JSON。Inventory 的完整仅证明“传给我的条目没有问题”，无法证明“用户选定范围已经完整消费”。这是当前有效调用可以造成的跨模块反例，尚不是生产遗漏事故。

需要版本化范围凭据，绑定 request/rules/root_revision/snapshot、期望条目及排除计数、清单摘要和消费完成状态。聚合器必须验证页连续、不重不漏、同一快照、无剩余游标；准备计划时交叉核对集合与凭据，保存时校验其绑定。至少区分枚举完整、消费完整、内容核验完成、计划就绪。明确选择某个子批次是允许的，但它必须有自己的范围描述，不能用整个根目录的完成标记。

## A1-05 / P1：普通几千文件规模无法保存计划

位置：[archive_schema.py](../../../core/archive_schema.py)，68–74 行和 JSON 事实校验；[archive_plan_store.py](../../../core/archive_plan_store.py)，350–356 行；case：two_thousand_file_plan。

构造 2,000 个小条目后，scope_json 为 1,660,226 字节。入口规范 JSON 允许该大小，但插入后的全库校验限制单字段 1 MiB，保存失败并回滚。不是图片总字节太大，而是全清单同时含 assets 和 package.members，多次重复路径/内容字段。此规模在用户使用范围内。

回滚正确，但功能不可用。建议将计划成员规范化持久化，或使用严格有界、可重建的分段清单；scope/capacity 留小型元数据及摘要。选择一种主表示，避免两份清单都能独立修改。planned 成员不能冒充已经封存的 archive_members。超预算应在昂贵读取/入队/写入前给出可执行的缩批原因。

不可一律放大所有 JSON 上限、删除校验或截断尾部文件来过测试。补 500、2,000、10,000 文件的整链保存、关闭、重开、完整重载及边界用例；记录真实 JSON/命令大小与峰值内存。10,000 文件若超过明确预算，应提前拒绝并可分批继续，不能在最终提交时才意外失败。

## A1-06 / P1：相同资产不能复用，Row 工厂造成假冲突

位置：[archive_schema.py](../../../core/archive_schema.py)，490–507 行；[archive_plan_store.py](../../../core/archive_plan_store.py)，405–419、567–570 行。cases：same_content_second_batch、sqlite_row_factory_alias。

第一批保存成功后，第二个 batch_id 引用完全相同资产/路径，因无条件 INSERT archive_assets 违反 path_key 唯一键而失败。第一批保留，第二批全回滚。重复分包、重新规划和断点后新批次都可能遇到它。

另一个独立错误：既有内容行直接与 tuple 比较。sqlite3.Row 和 tuple 不按这里期望的值语义相等；启用 Row 的连接中，完全相同的 size/hash 被判为不同事实。正式 writer 使用 Row，所以不能只用默认 tuple 连接验收。

ensure 应当是“查找并核对所有不可变事实，完全相同则复用，否则拒绝”，不能盲目 INSERT、INSERT OR IGNORE 或 REPLACE。已有内容的不同路径可合法引用同一内容；多个批次应引用资产而非各自拥有一份。重载校验目前还硬编码 artifact_id=None/source_kind=user_selected，不能把合法 observed_existing/download_receipt 记录改标签来适配它。

## A1-07 / P1：内容换代和事件历史不能按设计表达

位置：[archive_schema.py](../../../core/archive_schema.py)，144–162、310–325 行；[archive_scope.py](../../../core/archive_scope.py)，945–949 行。cases：new_revision_same_path、release_event_append。

**内容换代已复现**：同一资产同路径，从 revision=1 换到 revision=2、hash 改变，第二条记录被全局 UNIQUE(path_key) 拒绝。当前枚举还固定生成 revision=0，缺少与既有资产/代次的解析入口。若用覆盖旧行解决，会破坏旧封包成员与历史回读证据。

应明确分离不可变内容代次、历史路径关联和当前路径绑定。路径现在对应哪个内容可以更新，但必须对旧绑定做条件比较；过去包里是什么不能更新。没有历史摘要的新观察可建立当前基线，不得回填成旧下载时的证明。schema 尚未投产，应在临时副本阶段把这一模型定好。

**释放事件是前瞻模型缺口**：archive_releases 以 release_id 为主键，同时又定义 UNIQUE(release_id,event)。用同一操作 ID 写 prepared 后不能追加 released。如果 release_id 意图是事件 ID，则缺少将两事件关联为同一次释放的明确操作 ID。应明确 operation_id 与 event_id/序号，允许追加事件并拒绝重复冲突；不要 UPDATE prepared 来抹掉准备历史。本轮不实现删除能力，更不据这张表批准删除。

## A1-08 / P1：NULL 比较漏掉证据与内容不一致

位置：[archive_schema.py](../../../core/archive_schema.py)，421–449、550–580 行；case：null_hash_verified_fact。

在合成库中，内容表摘要为 NULL，关联的成员/观察使用一个具体摘要。record_archive_observation 根据调用者传入的 ContentVersion 检查 observed_size/hash，而没有核对其完整事实与数据库内容行一致；validate_archive_facts 的普通 != 比较又漏掉 NULL 情形。结果接受 user_returned_verified，内容表仍没有摘要，全库验证通过。

SQL 的 NULL != 非 NULL 不是 true，而是 unknown；应按字段语义使用 NULL 安全比较，并对已封存/已核验状态显式要求非空。不能简单把 NULL 都改为空字符串。SQLite 的 IS/IS NOT 提供相应比较语义。[SQLite 官方表达式文档](https://www.sqlite.org/lang_expr.html)

修复须在写入口和事实校验两端绑定 asset_id、revision、size、sha256、content_key、成员、对象版本和位置；只匹配 asset_id/revision 或只证明 location 有这个 content_key 不足够。未知摘要内容必须先经过明确的新基线流程，不得利用一条“已核验”观察偷偷升级旧事实。补 NULL/已知/冲突的交叉矩阵，以及篡改数据库后重载拒绝的测试。

## A1-09 / P2：证据来源标签和持久事实不充分

位置：[archive_schema.py](../../../core/archive_schema.py)，488–507、533–546 行；[local_observation.py](../../../core/local_observation.py) 的 LocalObservation；case：receipt_without_artifact。

已复现：source_kind=download_receipt、artifact_id=None 被接受，全库校验也通过。外键只保证非空引用存在，不能保证这条收据支持该资产的大小、摘要、页来源及质量事实。将来以 source_kind 做质量展示或去重，会把无收据数据说成有来源证明。

此外观察器产出的物理 FileIdentity、mtime、读字节数、取消/预算原因等不全部进入 record_local_observation；它只保存 LocalEvidence。仅凭内存观察不能支持重启后的现场解释，也不足以支撑将来的精确释放。此项后半部分是静态持久化缺口，本轮没有执行重启后的删除。

要求来源分级有实际引用和字段核对；既有文件使用 observed_existing，不冒充下载收据。保存版本化观察结果及原因、身份、根版本、run/check ID，严格限制敏感字段；新的核验应追加，旧记录不原地升格。人工回读证明内容相同与程序独立确认远端持有，仍是不同证据等级。

## A1-10 / P2：枚举二次增长和全库扫描会拖慢大收藏

位置：[archive_scope.py](../../../core/archive_scope.py)，759–766、793、940–944 行；[archive_plan_store.py](../../../core/archive_plan_store.py) 的保存/重载；[archive_schema.py](../../../core/archive_schema.py) 的 validate_archive_facts；case：scope_identity_comparison_growth。

身份去重每个文件线性遍历所有已接受文件。合成实测 200/400/800 文件产生 20,115/80,215/320,415 次身份比较，呈二次增长。对应约 0.065/0.141/0.333 秒仅是本机小文件样本，不外推 500 GB 的耗时。显式文件列表 pop(0) 也有整体搬移成本。

保存前后、重载及适配器外层重复调用全库事实验证，其中包括全部内容、JSON 和外键检查。历史增长后，小计划也承担全库成本，长事务还占用唯一写入者。这里为代码路径判断，尚未做大库计时。

用身份索引和 deque/迭代器消除无意义的二次成本；保持别名事实。将当前批次的必需约束检查留在事务内，数据库约束持续生效；昂贵全库审计留给明确的维护入口和迁移门槛，不能简单删除校验。建立整批字节、条目、时间、排队负载预算：单文件 1 GiB/60 秒之类的限制不能自动等价于整批受控。优化不得改成默认不读摘要或降低原图质量。

## A1-11 / P2：无关目录活动导致快照误失效

位置：[archive_scope.py](../../../core/archive_scope.py)，635–648、953–964 行；case：unrelated_ancestor_change。

先取图库第一页，再在它的父目录新建一个兄弟文件；图库根身份/状态未变，下一页仍返回 root_changed。代码保存一路祖先目录的完整状态，并用包括 mtime/size 的比较判断根变化。维护报告或其他项目的正常输出即可打断分页。

祖先链主要用来证明路径未被替换、未变为重解析点；实际选择根/子目录的成员变化才属于范围快照语义。分别比较这两类事实，保留根替换、根离线和链接替换拒绝。不要通过取消全部根检查来消除误报。

## A1-12 / P2：当前状态和测试无法可靠交接

未收尾目录：[writer 草稿](../../../../.runtime/jpgpng_a1_writer_compat_20261111_01/)。其三份改前核心副本与当前文件一致，但新增 archive_repository.py/test_archive_repository.py 已在工作树；两份测试报告分别 6 失败、5 失败，没有完成回执。此前 TASK_PLAN 和页首仍指向上一批“1214 通过”。本轮已将它登记为未完成草稿，并把当前下一步改为本审计的修复门槛；不改写旧通过报告。

测试方面：[test_archive_repository.py](../../../tests/test_archive_repository.py)，140–150 行先提交两个任务，只激活队首，却停止第二个排队任务，然后期待 settle_stop=True。排队任务在 request_stop 时已直接变 paused。独立使用原 v1 LibraryRepository 复现同样结果；这是测试前提错误，应核对目标任务状态或激活真正要测的任务，不能改 settle_stop 的正常语义。

该测试文件 99 行还把 item.plan 放入 set，StoredArchivePlan 含 scope 字典，不能据 frozen=True 假定整个对象可哈希。此处是被更早失败遮住的静态问题；修复时比较结构/确定性指纹，而非删掉并发断言。

已有测试缺少真实 Unicode 双文件、跨批相同资产、实际 writer 使用的 Row 连接、分页消费完整证明、2,000 文件保存、NULL 事实矩阵及排除规则在各入口的一致性。应将本轮反例变成明确的行为测试，不追求只增加测试数量。历史阶段状态与当前树状态必须分开，索引查询也不能覆盖掉直接源码证据。原联合总表提及的 foundation/test_counts.json 本轮未找到，现已改为以原始 XML 核对，不凭不存在的汇总文件验收。

## 尚未验证、不可扩大的结论

- 没有进行真实图库规模基准、云端请求、实际封包/纠错、密码交接或原件释放。13 个探针不是全部故障空间。
- 文件观察前后检查不提供针对恶意并发替换的原子保证；摘要一致也不是深度图片解码或 Pixiv 来源证明。应保持各证据层独立。
- 真实符号链接测试跳过项仍是覆盖缺口；junction 拒绝等旧证据不等于所有链接/挂载/竞态都已验证。
- 既有生产 J-01、依赖维护、筛选解释、覆盖摘要及整理分类仍按原计划待做；此次只是 A1 增量审计。
