# A1 有界范围枚举与临时计划适配

日期：2026-11-11。批次：JPGPNG-A1-SCOPE-ADAPTER-20261111-01。

## 目标与当前边界

承接 [A1 基础](A1_FOUNDATION.md)，在合成目录中实现显式范围元数据枚举、稳定分页游标，以及显式临时库的计划保存/重载。本页记录 2026-11-11 历史批次；2026-11-12 修复批次已改为 schema 3、分段计划与 ScopeReceipt，最新状态见[归档实施日志](IMPLEMENTATION_LOG.md)第 3C 节。

本批不扫描真实图库、不改生产数据库或下载入口，不接入 LibraryRepository._enqueue/_dispatch，不封包、上传、回读、释放或删除。生产 LibraryRepository._enqueue 影响为 HIGH（9 个直接调用者/3 条流程），本批明确不修改它。

## 范围枚举

ScopeEnumerator(root, ScopeRequest(prefixes, files, kind, root_id, root_revision, page_size), ScopePolicy(...)).page(cursor=None, cancellation=None)。

- 显式前缀表示递归目录；显式 files 只处理指定普通文件，不递归扩展。
- 根/祖先/目标重解析点、UNC/设备根和非常规目标均拒绝；拒绝输出/受管目录剪枝与敏感文件/临时文件/数据库/日志排除，普通目录名不因凭据片段误杀。
- 快照首帧有界收集元数据，记录根/目录/文件身份、大小与 mtime 并复核；嵌套目录发现与处理分开，不能漏扫或重复遍历。
- 页游标使用 HMAC 绑定枚举器、快照、根/映射/规则、页大小和边界；改游标、跨枚举器、重放、未初始化游标和根变化都拒绝，不把部分快照标完整。
- max_entries/max_directories/max_seconds/max_issues/max_exclusions 与取消只产生 partial/cancelled/budget_exhausted，不给可归档全集。
- 枚举不读文件内容、不计算内容 SHA-256、不调用 LocalObserver。ContentVersion 保持 revision=0/sha256=None；完整摘要由调用方显式调用观察器获得。
- 硬链接同身份保留每个可恢复的 raw 路径别名并共享内容身份，不能当多份独立释放空间；仅大小写解析到同一目录项的另一种拼写记为 duplicate。NFC/NFD 等物理不同文件分别保留。文件系统竞态不是原子防对抗保证。

## 临时计划适配

当前实现为 `save_archive_plan(conn, inventory, capacity, scope=...)` / `prepare_archive_plan` / `save_prepared_plan_in_transaction`，只接受显式 sqlite3.Connection，要求现有 schema 3、foreign_keys=ON、query_only=OFF 和正确的事务所有权。历史 schema 2 段落只作背景。

- 保存完整版本化 ArchiveInventory/CapacityPlan 文档：inventory 切为 `archive_plan_segments` 有界分段，header 只留 scope/摘要；内容/路径复用 ensure_archive_asset，同资产新代次可条件更新路径绑定。
- 计划包仅 planned，工具、密钥引用、大小/摘要保持未知；不写 archive_members/objects/locations/checks/releases，不形成完成账本。
- 相同计划同 ID 幂等重放；不同计划/容量/scope 或包 ID 冲突拒绝；BEGIN IMMEDIATE 全事务回滚，历史下载事实不变。
- load_archive_plan 在 query_only 短事务中重载，恢复 PRAGMA 设置，严格检查分段、摘要、版本、内容/路径、包索引，并重算清单/容量；旧计划在路径换代后仍可重载。全库审计由显式 `validate_archive_facts` 入口承担，常规重载只核对当前批次。

## 检查点与接续

.runtime/jpgpng_a1_scope_adapter_20261111_01/CONTRACT.md 保存初始契约；checkpoint/ 保存四份新源码/测试完整副本与上一批三份基础模块未变摘要。所有子代理失败 XML 和主线修正过程保留，最终报告不覆盖旧批。

下一步：
1. 将 `ScopeReceipt` 从“提供时校验、持久化并在重载时验证”推进为“所有新计划必须携带”；当前计划层仍接受不带 receipt 的计划，这是 A1-04 的剩余未闭环点。
2. 单独评估 `PathRef.target_key` 的 casefold 语义与下游 inventory 碰撞；当前范围层保留 NFC/NFD 物理不同路径，不静默丢失。
3. 在合成范围完成“枚举→观察→清单→临时计划”整链预算，随后独立评审生产 schema 3/唯一写入者接入；禁止直接投产临时迁移器。
4. A2 工具/密码、A3–A6 与 U1-B/U1-C/U2/U3/U4、E1-3/4 保留各自门槛。
