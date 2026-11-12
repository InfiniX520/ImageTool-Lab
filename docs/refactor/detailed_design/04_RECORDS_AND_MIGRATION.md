# 04 数据、下载名单与新记录核心

版本：v3，2026-10-21。用户已允许替换不适合的旧记录并接受必要时重下。本版取代 v1“必须沿用三库并持续兼容旧格式”的路线；旧稿保存在实施基线中。P3/P4 离线核心已验收，当前生产仍使用旧库，新库尚未启用。

## 1. 审查结论

**继续使用 SQLite，建立独立的单库记录核心；旧库可选导入，可读名单继续保留。**

SQLite 的事务、索引和本机规模适用。问题集中在现有模型：任务、断点和成果分散，缺少批次与交付身份，绝对路径参与身份，兼容 JSON/Markdown 需要反复物化。新核心不继续为这些旧写入协议承担复杂度。

2026-10-21 只读副本检查：页记录 104,215 条，作品 56,768 个；主库、关注库与待办库完整性均为 ok；有 1 条待办，104,211 条页记录保存 Windows 绝对路径。这证明数据库结构可读，不等于图片全体经过内容核验。

源码和隔离实验确认现有 SQLite upsert 已保护“下载完成”不被后续失败降级；该能力保留到新设计，不把它误列为现存 bug。旧记录没有哈希/验证等级，不能因库正常就升级成新校验成果。

依据：[原下载仓库](../../../core/sqlite_record_repository.py)、[记录物化](../../../core/record_manager.py)、[旧待办](../../../core/download_task_store.py)、[旧关注状态](../../../core/follow_crawl_state.py)、[实际库汇总](../../../../.runtime/jpgpng_refactor_20261021_iteration01/record_audit.json)、[合成记录实验](../../../../.runtime/jpgpng_refactor_20261021_iteration01/record_semantics.json)。

## 2. 新旧边界与切换方式

| 内容 | 新核心策略 |
| --- | --- |
| 新数据库 | `download_logs/library.sqlite3`；初始 schema_version=1，文件名与旧三库不同 |
| 原有三个 SQLite 文件 | 原路径保留；新运行链不持续写它们，不做双向同步 |
| 新安装/重新收集 | 允许新库从空库开始，不强制导入、不等旧数据迁移完成才能运行 |
| 旧完成记录 | 用户选择后只读导入，标为 legacy_trusted，不伪装成新版本已验证 |
| 旧关注游标/待办 | 只有能证明账户、条件、位置与路径映射时才导入；否则保留旧任务，明确重新枚举/重建 |
| 原 JSON/Markdown 文件 | 原件保留；不要求新程序实时维护每个旧文件名与目录结构 |
| 新下载名单 | 本批和累计查询来自新库；按结束批次或显式请求导出 Markdown，不靠兼容物化反推事实 |
| 旧门面/API | 仅作为阶段切换的适配边界，未承诺永久保持每个内部函数的历史契约 |
| 原图片库 | 默认仍可选用原目录；无需为换记录库搬图片或删原库 |

没有导入旧记录的任务会重新下载可访问作品，但不能覆盖无法证明归属的同名旧文件：使用明确的新输出目录或确定性冲突命名。允许重下不等于允许无条件覆盖。旧记录导入和新核心启用互相独立。

## 3. 表与关键约束

候选实现位置 `core/library_repository.py`，只有这一存储边界写业务库；标准库 sqlite3，不加 ORM。精确 DDL 随 P2 编码与测试固化，以下字段和约束必须落实。

| 表 | 主键 / 关键字段 | 用途与不变量 |
| --- | --- | --- |
| schema_meta | key/value | schema_version、最低读写版本、产品记录格式；未来版本拒绝写入 |
| jobs | job_id；parent_job_id?、request_version、request_json、scope_fingerprint、account_ref、state、时间 | 不可变收集意图与条件；无凭据 |
| batches | batch_id；job_id、command_id UNIQUE、budget_json、phase、stop_reason、时间 | 一次开始/继续；结束批次保留原始结果 |
| batch_items | (batch_id, illust_id)；元数据快照、decision、reason_codes、required_pages、result_state | 本批作品名单、筛选结果、作品名额；不能只记成功而失去失败/过滤范围 |
| deliveries | delivery_id；job_id、origin_batch_id、completed_batch_id?、work/page、output_ref、quality、source_identity、generation、state、attempts、retry_at、checkpoint、prepared | 页待办和恢复；在途目标唯一，重试复用交付 ID |
| artifacts | artifact_id；delivery_id UNIQUE、work/page、output_ref、品质、大小、hash、verification、completed_at、legacy_origin? | 已交付成果，成功事实不被尝试失败覆盖；同一页可以有明确的不同输出/代次 |
| checkpoints | (job_id, source_id, pass_id, entity_id)；artist_id?、ordinal?、cursor、item_anchor、coverage、updated_at | 关注画师、来源分页、快照和新作水位；游标必须落后于或等于候选持久确认 |
| source_items | (job_id, source_id, pass_id, occurrence_key)；entity_type、entity_id、payload | 关注快照、榜单日期名次、交集来源出现；共享同一结构，不无限内存集合 |
| events | event_seq INTEGER；event_uuid UNIQUE、accepted_command_id?、job_id、batch_id?、kind、safe_payload | 关键状态与可重放事实；不逐块写传输进度 |
| legacy_imports | import_id；source_digest、source_format、scope、state、cursor、counts、时间 | 可选导入的去重、审计、断点与回退；无原始敏感配置 |

必要索引：batches(job_id,started_at)、batch_items(batch_id,decision,illust_id)、batch_items(artist_id,batch_id)、deliveries(state,retry_at)、deliveries(job_id,illust_id,page_index)、artifacts(artist_id,completed_at,artifact_id)、artifacts(illust_id,page_index)、checkpoints(job_id,source_id,pass_id)、source_items(job_id,source_id,pass_id,entity_id)、events(job_id,event_seq)。

接受命令的 accepted_command_id 对非空值唯一；后续事件引用命令但不重复占该键。在途 delivery 对“规范输出目标+页+品质”使用部分唯一索引；终态保留重下代次。并发判定由数据库唯一键保证，不只靠 Python set。

页结果与文件成果分开：失败页保留在 deliveries/batch_items，不覆写 artifacts；UI 通过已确认成果聚合多页完成状态。必需字段未知用 null，旧 is_ai 默认 false 无法证明非 AI 时标为历史未注明。

## 4. 事务与文件提交

一个数据写入线程拥有新库写连接，读查询使用短只读事务；WAL、foreign_keys=ON、关键提交 synchronous=FULL。正常工作不打开旧三库写连接。不得在数据库事务中等待 HTTP 或 GUI。

### 4.1 接收候选与游标

同一事务写本批候选结论、来源出现、作品名额、所需页待办及已确认游标，然后返回确认。正在处理一页时可逐候选推进页内锚点；只有所有候选有持久结论才能推进到下一页。

内存队列只传 delivery_id。持久化后队列满可以等待或下次恢复；游标不会越过尚未入库的候选。metadata_unknown 等未决项有明确引用，不因来源遍历结束而丢掉。

### 4.2 文件交付

1. 按 [02](02_DOWNLOAD_INTEGRITY.md) 验证同目录临时文件，flush/fsync，保存大小、摘要与格式等级。
2. 新库提交 delivery=prepared 及文件身份；失败就保留片段，不发布完成。
3. 在明确路径所有权下发布正式文件，拒绝覆盖无关旧文件。
4. 新库单事务写 artifact、完成事件、delivery=completed、completed_batch_id 和本批计数。
5. 提交确认后通知界面；名单导出失败只影响导出状态。

数据库内部步骤 4 原子完成，不再分成三个库互相确认。文件系统与 SQLite 之间仍不可能一个普通事务全部原子，prepared 恢复协议必须保留。FULL 与 fsync 增强耐久性，但不保证任何硬件故障下都绝对不丢数据。[SQLite WAL](https://www.sqlite.org/wal.html)

## 5. 恢复判定

| 状态 | 恢复动作 |
| --- | --- |
| candidate/游标事务未提交 | 重新枚举，稳定 ID 与唯一键去重 |
| pending 已提交但不在队列 | 按新批次预算重新调度 |
| transferring，有确认片段 | 验证前缀与远端版本，安全续传或全量重取 |
| prepared，只有临时文件 | 核对摘要并尝试拒绝覆盖发布 |
| prepared，正式文件在，artifact 无 | 仅核验该文件与 prepared 凭据；匹配后完成同库事务，否则 recovery_needed |
| artifact 和 completed 均有 | 幂等完成；不再靠日志或重新下载确认 |
| prepared 但文件缺失/变动 | 保留事实与错误，待处理；不猜、删或覆盖 |
| 完成文件后来被用户搬走 | 普通去重信任完成成果；显式核验/重下单独操作，不在启动扫描全库 |
| 旧 running 批次所有者已退出 | 标 interrupted，完成本地对账；用户继续才恢复联网 |
| 记录/游标 JSON 损坏 | 报告具体对象，保留原始数据；不返回空游标掩盖损坏 |
| 导出中断 | 旧成品保留，临时导出可恢复或重建，不改变下载成功 |

本地自动对账只处理未决交付涉及的文件；不解码图库、不全量 stat。取消/暂停/覆盖不确定都在任务详情可见。

## 6. 名单、历史和导出

| 视图 | 内容与查询 |
| --- | --- |
| 本批结果 | 新增成功、已有跳过、部分完成/失败、待核验、属性过滤分别统计；默认作品行，可展开各页 |
| 累计历史 | artifacts 与历史导入成果；按画师、作品名称/ID、下载时间、品质和位置查询 |
| 来源详情 | 实际榜单日期/名次、排序、所查看范围与停止原因 |

origin_batch_id 表示首次产生待办；completed_batch_id 表示真正完成该交付的批次。恢复旧待办而本批写成的页算本批新增；重放完成事件不重复计数。旧导入没有可证批次就显示“历史导入”，不猜造任务归属。

主要字段：作品、画师、所需/完成页数、结果、下载时间；可展开收藏数、AI、来源、路径和失败原因。作品链接和打开目录由用户点击；未知字段不为名单额外联网查询。

每页默认 100 行，可选 50/100/200；按时间和稳定 ID 做游标分页。查询不重建全部表格，不读取原图。新时间存 UTC 并按本机用户时区显示；无时区旧文本保留，不能猜转换。

导出 Markdown 使用查询条件快照和事件水位，流式分页；进行中名单注明仍在变化。使用 artifacts/追加事件重建对应水位，不把当前表 updated_at 过滤冒充历史快照。导出同目录唯一临时文件，成功后发布，取消不损坏旧成品。转义表格和链接，内容只允许安全引用。

P4 当前导出选择在一次调用中保持单个 SQLite 只读 snapshot，第一次读取取得事件水位，分块输出后立即关闭；最长 300 秒并支持取消。它不提供重启后任意指定旧水位的历史重建 API，不用 updated_at 假装重建。单次 fetch 是短事务，连续翻页各自返回当前水位；导出期间的 WAL 保留量需在 P5 实测。该实现边界不放大为完整历史事件重放已完成。

近期 3000 条只是可选视图窗口，不是累计总量限制。新运行不周期性重写旧 master JSON、每画师 JSON、目录 MD 和月度 MD 全套副本；用户确实要的可读名单通过新导出提供。

## 7. 相对路径与配置

PathRef = kind(project/external)、root_id、relative_path。项目根动态解析自 AppPaths；外部根保存明确绝对目录。output_root_id 与相对文件路径参与身份，不把当前盘符写进 job/delivery 主键。

旧绝对路径只有能证明属于旧项目根时才映射；外部目录保留，未知归属要求明确选择。没有映射证据的旧待办不自动新建重复任务。运行读取经统一路径解析，不依赖当前 shell 目录。数据库使用已验证的本地文件系统，网络共享位置不当作已验证 WAL 部署。

配置仍按现有文件位置过渡：应用外观在 config/config.yaml，下载方案在 pixiv_config.json；认证只沿现有专门边界处理。增加独立 settings_schema_version，显式旧值优先于默认；不把 YAML 默认覆盖 JSON 已保存下载参数。运行任务使用数据库不可变 request_json，不读取控件实时值。

新配置默认历史补全、原图/多页开启，最低收藏关闭、排除 AI 关闭；旧显式设置保留。配置损坏不以空字典覆盖，未知键和原始备份保留。业务代码无本机盘符。

## 8. 可选旧记录导入

提供两种明确模式：

- **全新收集**：新库不读旧历史来跳过作品；选择新输出目录或冲突策略后重新收集。无需清空旧库。
- **导入历史用于去重**：对旧库一致性副本只读导入有效完成页，带来源摘要/旧主键与 legacy_trusted 等级。用户搬走文件仍可按该记录跳过；若用户要重新验证/重下，可限定画师或作品范围。

导入以来源摘要和旧主键幂等，分批提交、可恢复，失败不留下“已全量导入”的标记。旧来源、未支持字段、缺页/页数不明如实标注。不得把作品 ID 列表直接推断成全页完成，不给无内容验证的旧图伪造哈希或新校验等级。

旧关注游标和失败待办是可选独立导入，不成为新核心开发前提；默认保留可查看的旧摘要，能可靠映射才导入。新画师仍可直接开始新的历史任务。

## 9. 切换、备份与回退

1. 记录当前工作树、源码与业务指纹，副本先行；本轮 baseline 不代替后续每轮起点。
2. 新库先只在隔离根运行，通过单库事务、崩溃恢复、名单与路径验收后，才为生产创建新文件。
3. 切换前相关 JpgPng 写入安全停止并取得单实例锁。旧三库做一致性备份和清单，不影响其他项目；活库用 SQLite backup API，分别快照不等于跨库共同快照。[SQLite Backup API](https://www.sqlite.org/backup.html)
4. 可选导入在副本演练，检查计数、状态、路径、页身份和来源摘要；核对后才导入生产新库。未来 schema 直接拒绝写入。
5. 新入口采用新记录适配器，旧 UI 暂存期间也不得同时用旧核心写同一新文件；按来源逐步切换。
6. 回退旧程序时旧库仍在原位置；新库、新图片与导出全部保全。若要让旧程序识别新下载，另做有验收的单向导出，不能默认承诺兼容。
7. 新库升级要备份、事务迁移与中断恢复；还原旧快照前先保全升级后的新增事件和文件，不能直接覆盖丢成果。

诊断日志可以按明确策略轮转，业务库、导入历史、备份、名单和未决下载片段不按缓存清理。默认不自动删除旧库，是否归档等新版本稳定后再做。

## 10. P2 实施结果与后续边界

2026-10-21 已实现新单库任务核心、同事务接纳/游标、命令幂等、页所有权、批次名额、暂停/取消/恢复和有界单写线程。生产 `library.sqlite3` 尚未创建。旧记录导入仍是可选功能，在 P8 的副本与切换验收中单独完成；不把它作为新核心开发或运行的前提。

新库打开前先只读核对产品/版本以及实际已知表和索引声明；相同版本但约束不兼容的早期隔离试验库也拒绝写入。读查询使用短事务，阶段、计数和事件 revision 来自同一快照；读取失败不使用忽略 WAL 的 immutable 回退。

页收据另有 `source_pages`，唯一键为 `(job_id, source_id, pass_id, entity_id, page_identity)`。同页重放先核对内容摘要；事务失败会同时回滚候选、待办、来源出现、收据和游标。P3 将在同一写边界补充文件提交和成果查询，不让第二个存储器绕过它。

## 2026-10-24 本次切换取舍

本次按用户允许重新下载的偏好采用全新单库收集，**未选择旧历史导入；导入器没有发布**。第 8 节的导入模式是保留设计，不能据此声称现版可导入。原三库、配置和旧图片原位保留，独立新版账本为空库开始；旧程序通过 `--legacy` 访问旧库，禁止把它指向新库。

新版偏好位于 `config/collection_v2.json`，从实际旧设置按允许字段迁入数量、并发、命名、间隔和筛选；输出位置转为相对路径。外部位置由 `config/output_roots_v1.json` 在本机映射。备份、异地目录重开与续跑、还原旧快照前保全新成果已有实测，详见[发布记录](../../../../docs/maintenance/2026-10-23_jpgpng_release_and_cleanup.md)。
