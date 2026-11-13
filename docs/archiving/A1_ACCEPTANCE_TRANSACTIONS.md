# A1 逻辑资产身份、expected-old 与接受事件设计（N1/N2）

> 2026-11-13 复审注：这是 N1/N2 设计与当时实现记录；“已实现”不等于整体验收。新发现的接受事件竞态、幂等及多路径问题见[当前审计](../audits/2026-11-13-a1-acceptance/README.md)。下文原设计决定保留，下一轮按 R0–R6 修订。

日期：2026-11-12。批次：`JPGPNG-A1-AR3-IDENTITY2-20261112-01`，工作包 N1/N2，对应
[最终审计](../audits/2026-11-12-a1-final/FINDINGS.md) 的 AR3-01、AR3-02、AR3-03。
base commit `87888af`。本文先定身份/键决定，再改 DDL 与代码；它不是生产接线授权，
不接触真实图库、账号或生产库。

## 1. 范围

| 编号 | 问题 | 本文的处理 |
| --- | --- | --- |
| AR3-01 | 相同字节被当成同一逻辑资产，独立文件后续分叉冲突 | 逻辑资产身份与"相同字节"彻底分离 |
| AR3-02 | expected-old 用刚读到的 current；无事务调用留下部分事实 | expected 只来自解析输入；低层写接口要求调用者事务 |
| AR3-03 | 最新诊断观察被当成已确认物理身份 | 当前绑定显式引用最后一次**被接受**的证据 |

不在本包：AR3-04（观察根版本与真实交接入口，N3）、AR3-05（混合字符段别名，N1 另一支）、
AR3-06（索引与规模，N4）、AR3-10（共享 busy 翻译，N5）。`archive_plan_store` 的计划
保存路径不在允许修改范围，其"按 revision 比较推进绑定"的问题留在 N3 处理，本文只保证
新的专用事务命令路径不经过它。

## 2. 六种身份，各自只回答一个问题

| 对象 | 稳定身份 | 允许复用 | 明确不允许推导 |
| --- | --- | --- | --- |
| 逻辑资产 | `asset_id`：枚举时由 `kind + root_id + device + file_index` 派生的 `scope_…` | 同一物理对象在同一根内的多次出现（硬链接、重扫） | 两文件 SHA-256 相同 ⇒ 同一资产 |
| 内容代次 | `(asset_id, revision)`，事实不可变 | 同资产同 revision 的重复观察 | 用另一资产的 revision 顶替 |
| 字节内容 | `sha256 + size_bytes` | 去重统计、容量估算、"这些字节已存在"的证据 | 来源真实性、替换授权、资产所有权 |
| 路径绑定 | `path_key` → `(asset_id, revision)` + `acceptance_id` | 同一目录项的已证明别名 | 用最新一条观察推断接受对象 |
| 物理身份 | `device/file_index` + 本次观察范围 | 当次候选交接、硬链接识别 | 跨时间永久同一 |
| 接受事件 | `archive_path_acceptances` 追加行 | 同 check + 同内容的重放 | 由诊断状态反推是否接受 |

结论：**摘要只证明字节相同**。`find_known_content` 返回的是"这些字节已在库中"的证据，
不再是某条可直接接管的 `ContentVersion`。只有 `known_content.asset_id == 候选自己的
asset_id`（同一物理对象、同一根）时才允许复用该代次，否则一律为本路径建立**自己的**
基线代次。

## 3. 解析规则（`resolve_content_version`）

候选与观察的物理事实（identity/size/mtime/摘要/字节数）先逐项核对，任何不一致仍是
`CONFLICT`。之后：

| 场景 | 当前绑定 | 结果 |
| --- | --- | --- |
| 首次见到该路径，同资产无已知代次 | 无 | `BASELINE`，revision = max(expected, observed) + 1 |
| 首次见到该路径，同资产已有该字节代次 | 无 | `REUSE` 同资产代次（硬链接/重复观察） |
| 首次见到该路径，**另一**资产有相同字节 | 无 | `BASELINE`（本路径自己的资产），不接管对方代次 |
| 同一物理对象、同内容 | 有 | `REUSE` |
| 同一物理对象、内容变化（含 A→B→A） | 有 | `NEW_REVISION`，同资产 revision+1，只增不改 |
| 物理对象变化 | 有 | 未确认 `NEEDS_CONFIRMATION`；已确认 `REPLACEMENT`，沿用原 `asset_id` 的 revision+1 |
| 历史摘要未知（sha 为 NULL） | 有 | `BASELINE`：建立新基线，不把旧未知摘要改成已验证 |
| 接受事件的根版本与本次候选不同 | 有 | 物理身份不再是"同一对象"证据：内容相同仍 `REUSE`，内容不同要求确认 |

"复制后独立修改"与"独立同字节文件"因此都落到各自资产的新代次，不需要覆盖任何历史行；
"A→B→A"产生第三个代次而不是复活旧 revision；"同名替换"在确认前沿用原资产，确认后
revision+1，旧计划仍指旧 revision。

## 4. expected-old 只能来自解析输入

`apply_content_resolution` 的写前顺序固定，且**全部在任何写入之前完成**：

1. 连接必须处于调用者拥有的工作单元（见第 6 节），否则立即拒绝；
2. 解析输入显式声明它携带哪一种期望：
   - `resolution.expected_binding_absent is True` → 期望"该路径必须仍未绑定"。
     此时 `previous_content` 是观察用的未知占位代次，**不是**已绑定代次，不能当 expected；
   - 否则 expected = `_content_key(resolution.previous_content)`；
3. 与事务内读到的当前绑定比较；不一致时返回结构化冲突，不覆盖新绑定；
4. 唯一允许的不一致：当前绑定**已经就是本次要写的内容**（同一 `content_key` 且
   size/摘要相同）。这时不新增 revision、不改写任何事实，只追加接受事件；若其接受事件就是
   本次 `check_id`，则整次调用幂等返回同一个 acceptance；
5. 只有通过上述比较后才写内容/资产/观察/**接受事件**/绑定。

不允许把刚读到的 current 当 expected 传回去，也不允许"版本更大就接受"。
`bind_archive_path` 仍然只接受显式 `expected_content_key`（`None` = 必须不存在），
且同 key 时只允许更新接受指针，不改内容。

## 5. 接受事件与诊断分离

诊断（`archive_local_checks`）只追加，永远不改变当前绑定引用哪一条证据。新增一张
只追加的接受表，当前绑定显式外键引用它：

```text
archive_path_acceptances(acceptance_id PK, path_key, content_key, asset_id, revision,
                         expected_content_key, root_revision, check_id,
                         file_device, file_index, accepted_at, reason)
archive_path_bindings(path_key PK, asset_id, revision, bound_at, acceptance_id FK)
```

- 接受行指向产生它的那一次真实观察（`check_id`），并记录被接受的物理对象、根版本和
  expected-old，可被全库审计逐项重算；
- `acceptance_id` 由 `(path_key, content_key, check_id)` 确定，同一证据重复应用不新增行；
- `load_content_binding` 只读当前绑定 + 其接受行，**不再**按 `checked_at` 取最新一条非空
  物理身份的观察；
- 计划保存等"只有索引没有观察"的路径产生的绑定允许 `acceptance_id` 为空，其物理对象
  明确为未知；此后内容变化必须人工确认，不能凭旧诊断升级。

## 6. 事务归属

`archive_schema` 的所有写函数都不断言、不提交事务：

| 接口 | 事务要求 |
| --- | --- |
| `ensure_archive_asset` / `bind_archive_path` / `record_local_observation` / `record_path_acceptance` / `record_transfer_intent` / `record_archive_observation` / `record_release_event` / `apply_content_resolution` | 必须先有调用者工作单元，否则 `ArchiveTransactionRequiredError`，且在任何读写之前拒绝 |
| `ArchiveLibraryRepository.apply_archive_resolutions`（本包新增的唯一受控命令） | 仓库 writer 在一个 `BEGIN IMMEDIATE` 内完成整批接受；任何一步失败整批回滚 |

"调用者工作单元"的具体判定（`_require_write_transaction`）：

- `conn.in_transaction` 为真：直接使用，模块不 BEGIN/COMMIT/ROLLBACK；
- `isolation_level is None`（真正的 autocommit）：**立即拒绝**，这正是审计里留下
  半批内容的调用方式；
- 其余隔离级别（Python 默认的 `""` 或显式 DEFERRED/IMMEDIATE）：由模块补一条
  `BEGIN IMMEDIATE`，让 expected 比较与所有写入落在同一写锁内；提交/回滚仍由调用者的
  `with conn:` 块或显式 COMMIT 决定。

这样既满足"低层写接口 autocommit 立即拒绝"，也不强迫调用者改用手写 BEGIN。

`record_local_observation` 对同一 `check_id` 的同事实重放幂等，事实不同则拒绝，避免
"先记诊断、确认后再应用"这种真实顺序因主键冲突失败。

## 7. schema 3 → 4

新增 `archive_path_acceptances`、`archive_path_bindings.acceptance_id` 和相应索引属于
DDL 改动，因此：

- `ARCHIVE_SCHEMA_VERSION` 由 `3` 升为 `4`，不再声称旧 schema 3 库原样兼容；
- 迁移只接受 v1 基础库；已是 v4 的库走"已是最新"分支并做完整对象比对；
- v3（含审计期间的 schema 3 草稿）与 v2 一律明确拒绝，错误信息写清原因；
- 旧批次/旧计划不改写：它们仍引用旧 `(asset_id, revision)`，重开、重放、审计判定一致。

## 7.1 schema 4 → 5（R4，AR4-07）

schema 5 与 4 的**表结构相同**，只多两条显式命名的覆盖索引（顺序即列前缀）：

```text
CREATE INDEX idx_archive_contents_digest ON archive_contents(sha256, size_bytes, asset_id, revision)
CREATE INDEX idx_archive_assets_path   ON archive_assets(path_key, asset_id)
```

- 摘要查询从 `SCAN + TEMP B-TREE` 变为单条覆盖索引查找；路径所有者查询从扫描
  `sqlite_autoindex_archive_assets_1` 变为 `(path_key, asset_id)` 查找，触发器与
  `_replacement_conflicts` 的那次扫描一起消失。
- `_check_existing_objects` 仍要求对象逐条相等：v5 库少一条索引、或同名索引换了列前缀，
  都会得到 `Missing or incompatible archive object: …`，不会静默退化为扫描。
- **v4 草案显式拒绝（不迁移）**：工作区里 1,534 个 v4 库全是 `.runtime` 合成临时库、
  项目树内 0 个，没有生产库；因此 `_inspect_source` / `_inspect_source_for_migration`
  共用 `_refuse_source_version`，文案点名 "schema 4 acceptance-event draft, superseded by
  schema 5; no production library exists" 并给出"从 schema 1 重建"的动作，源文件与备份
  都不被改写。v2 / v3 / 未知 / 未来版本同样拒绝。
- 迁移入口仍只接受 v1：`migrate_archive_copy` 逐条执行 `ARCHIVE_DDL` 时自动建立两条索引，
  随后 `_check_existing_objects`、完整性、外键与维护审计照常复核；已是 v5 的副本再次迁移
  返回 `already_current` 且 `sqlite_master` 不变。
- 计划协议（plan 3 / receipt 2）与旧计划正文不变；旧计划不可跨版本搬运，临时库从 v1 重建。

## 8. 验收矩阵

每个正向都检查最后路径绑定、全部历史行和恢复路径，而不只是"不抛异常"：

1. 两个独立同字节文件分别改写，互不冲突；
2. 硬链接两个路径共享同一逻辑资产与同一代次；
3. 复制后只改副本；
4. 内容 A→B→A 得到第三代的同字节 revision，旧行保留；
5. `os.replace` 同名替换：未确认拒绝、已确认沿用资产并 revision+1、旧计划仍读旧内容；
6. 解析时不存在、提交前已建立绑定：拒绝，绑定不变；
7. 旧版 expected + 新内容竞争：拒绝，全部相关表计数不变；
8. 同 check_id 不同事实：拒绝；同 check_id 同事实：幂等；
9. 每个写阶段异常：整批回滚，writer 可继续；
10. autocommit 调用任一写接口：写入前拒绝；
11. 缺失/unreadable/changed/取消/旧根版本的诊断写入后，授权结果不变；
12. 已确认与未确认替换分别测试；
13. v3 库被拒绝，v4 库重开、旧计划重载与全库审计一致。

## 9. 实现状态（2026-11-12，分支 fix/a1-ar3-identity2-20261112）

| 目标 | 状态 | 证据 |
| --- | --- | --- |
| AR3-01 身份与同字节去重分离 | 已实现 | 独立同字节 A/B、复制件、硬链接、A→B→A、同名替换 |
| AR3-02 expected-old 与事务边界 | 已实现 | 陈旧 expected（含"必须不存在"）拒绝、autocommit 拒绝、重放幂等、同 check_id 不同事实冲突、失败整批回滚、仓库单事务命令 |
| AR3-03 接受事件与诊断分离 | 已实现 | changed/missing/unreadable/cancelled/旧根/同时间诊断都不改变接受指针 |
| schema 3 → 4 | 已实现 | v1→v4 迁移、v3 拒绝、备份可重开、主文件独立复制后审计与旧计划可读 |
| AR3-04 观察/回执不得重标 | **本包修一半** | `observe_candidate` 拒绝候选根版本与观察根版本不一致，`rebind_observation` 不再用候选值覆盖；`prepare_observed_plan` 增加 receipt 与 candidate 同源校验（snapshot_id / mtime / 物理对象 / 大小 / 已绑定摘要） |
| AR3-05 混合字符段别名 | **未实现（有意保留红）** | ASCII 大小写重选已修好（同一 identity_key、保留首次拼写、不再报 immutable facts）。`画A.bin`/`画a.bin` 需要 identity_key 对非 ASCII 段也折叠，或引入按真实目录项规范化的路径层；两者都会改变资产键、receipt 事实与旧计划的键语义，无法在 archive_scope 内自证安全，按裁决未用全局 casefold 硬过测 |

已知边界：

- `archive_plan_store.save_prepared_plan_in_transaction` 仍按"当前 revision 比较"推进
  绑定；本包未修改该文件，新的受控命令不经过它。接线时必须把 expected binding 作为该
  计划的不可变输入传进去（N3 任务）。
- 旧审计脚本 `docs/audits/2026-11-12-a1/evidence/probes.py` 直接以 autocommit 调用
  `ensure_archive_asset` 等写接口，按新契约会得到 `ArchiveTransactionRequiredError`；
  它是历史证据，不在本包修改范围。
- 计划保存产生的绑定没有接受事件（`acceptance_id` 为空，物理对象未知），
  因此其后的内容变化仍要求人工确认。
