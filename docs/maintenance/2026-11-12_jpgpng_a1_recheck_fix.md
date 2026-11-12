# JpgPng A1 复审修复批次记录（F1/F2/F4）

批次：`JPGPNG-A1-RECHECK-FIX-20261112-01`  
分支：`fix/a1-recheck-f1-f4-20261112`  
起点：`09e447d audit/a1-recheck-20261112`  
日期：2026-11-12  
状态：F1/F2/F4 已离线通过；F3 长任务实现中；F5 独立整链与 F4 复审并行中。**不是生产发布，不是真实图库迁移，不是删除授权。**

## 1. 本批提交

| 提交 | 内容 |
| --- | --- |
| `4fbda0c` | F1/F2：读取端凭据/引用内容闭包/分段读取边界 |
| `447f823` | F3 候选→观察→内容代次交接设计 |
| `4a9c3f3` | F4：范围目录 I/O 与页内取消/预算 |
| `5f73f03` | identity_key 授权边界记录 |

## 2. F1/F2 读取事实

- 当前版本计划重载、重放和全库审计要求非空 `scope_receipt`。
- 当前批次校验检查 `archive_contents` 的 content_key、大小、摘要，不再只信任资产行。
- 重载先检查文档字节/段数预算，再逐段累计读取；拒绝超界、重复 JSON 键。
- 新增 AR2-01/02/05 回归测试。
- 证据：`..\.runtime\jpgpng_a1_recheck_fix_20261112_01\focused_f1f2.xml`、`full_f1f2.xml`。
- 当时全量：216 专项通过；1317 项 / 1314 通过 / 0 失败 / 0 错误 / 3 跳过。

## 3. F4 范围 I/O

- 显式选择按父目录建立一次有界名称索引；只为显示键碰撞组解析真实拼写。
- 分页稳定性改为 `_StabilitySession`；每页有界复核，结束页全量复核。
- `_WorkGuard` 覆盖根/祖先、lstat、scandir 每一项、复核和返回前。
- 新增 `tests/test_archive_scope_io_budget.py` 22 项。
- 实测：显式平铺 320 文件目录项访问 51,360→0；分页 160 目录纯分页 lstat 26,871→1,749。
- 范围三文件：117/117 通过。
- F4 代理最终全量：1340 项 / 1337 通过 / 0 失败 / 0 错误 / 3 跳过。
- 主线复跑同一全量：1340 / 1335 / 1 失败 / 1 错误 / 3 跳过；两个非通过均是 Tk/Tcl GUI 环境，不是归档范围失败；两份 XML 保留。

### F4 语义变化

- `ScopePolicy.max_seconds` 现在表示“一次有界操作的工作时间”，不是整段会话墙钟。
- 已失败/取消/预算耗尽快照的后续页不做页级复核，避免把预算状态误降级。
- 新增 `NAME_INDEX_BUDGET_EXHAUSTED` 与名称索引/复核上限常量。

### F4 未解决

- 页级复核约 2×目录数，不是零成本；结束页全量复核线性。
- 成员稳定性仍依赖 size/mtime/名称集合，防不住对抗性 mtime 恢复。
- 繁忙目录可能 `directory_changed`；名称索引超限无降级；单次阻塞 OS 调用不可中断。
- 未做真实图库、RSS、2,000 文件整链（F5）、崩溃/WAL。

### F1/F2 独立复核追加（已转 F3）

- **R1/P2**：全库审计只判断 receipt 非 null/只对 planned，load 则校验完整 receipt、根、段、摘要；审计与 load 不是同一协议。migrate_archive_copy 对 schema-3 源依赖该审计，迁移前校验不足。
- **R2/P2**：prepare 允许 8 MiB plan header，审计 `_stored_json` 只允许 1 MiB；合法 1.5 MB scope 计划 save/load 成功但 audit 失败，schema-3 迁移可能永久变红。必须统一预算。
- **R3/P2**：段读取先物化 payload 再检查 1 MiB/8 MiB；9 MiB 行在拒绝前已分配。应先查 `length(CAST(payload AS BLOB))`/计数。
- **R4/P2**：receipt 与清单只有单向 snapshot⇒scope_complete；子批 receipt + 完整清单、完整 receipt + 不完整清单可互相矛盾。
- **R5/P3**：资产校验未钉 source_kind，`observed_existing` 可改成 `user_selected` 仍通过；`output_ref_json` 未走严格 JSON。
- **R6/P3**：receipt MAC 从不校验；snapshot_id/digest/root_revision 无对照物；不能把“凭据验证”说成“内容已验证”。
- **R7/P3**：非法 `created_at` 在独立 load 被包装，仓库路径可能逃逸裸 ValueError。
- **R8/P3**：9,000 条目上限只在 prepare，读取端未检查。
- **R9/P3**：读取端不查 path bindings，旧计划仍可读；符合设计，但下游只能以计划内资产为准。

### F4 独立复审追加（pre-existing，已转 F3 修复）

- P1：枚举期间父目录出现无关新条目被误判 `root_changed`、0 条目；祖先链不应比较完整 NodeState。
- P1：父前缀+子前缀/后缀显式文件重叠选择被遍历两次，预算翻倍、自指 duplicate、`scope_complete` 假通过。
- P2：首页即 abort 页时 `consume_scope_pages` 的 `last_page_index=-1` 抛 ContractError，应返回未完成回执。
- P3：prefix 的两种大小写拼写优先级受输入顺序影响。
- 这些不是 F4 提交新引入；F4 的最终 checkpoint 让 abort 首页路径更容易触发。

## 3B. F5 2,000 文件真实整链

- 独立 worktree `f5_worktree` 固定 HEAD `4a9c3f3`，未改产品源码、未提交。
- 2,000 个真实小文件 / 441 目录节点；逐项路径/大小/SHA-256/work/page/path_key/content_key 全部一致，missing/extra/差异均为 0。
- 计划 12 段、payload 2,300,857 B（8 MiB 的 27.4%）；临时库 6,131,712 B，assets/contents/bindings 各 2,000，integrity/FK 正常。
- 冷/热整链 23.703/10.447 s；观察 17.676/5.198 s；保存 3.041/2.954 s；重开加载 0.496/0.594 s。
- `tracemalloc` 验证峰值 61,065,092 B（26.54× 载荷）；全新进程单次 load 峰值 26,936,770 B（11.71×）→ 8 MiB 不是堆上限。
- page_size 1/100/1000 与显式选择：计划内容逐字节一致；scandir 均约 441 次/0 次，无页数爆炸。
- 15 个负向对照均被计划加载拒绝；但**全库审计仍接受删除一个计划段**（只查预算上限，不核对 header segment_count/inventory_sha256），已转 F3 修复。范围凭据仍只绑路径+大小（AR2-03），已在 F5 真实数据复现。
- 相关归档 328 passed/3 skipped；全量 1,337 passed/3 skipped。未覆盖真实图片/解码、500 GB 吞吐、并发/崩溃/WAL、symlink/硬链接，不能称 F3 完成。

## 3C. F6 崩溃/WAL/多进程

- 61 项合成 schema-3 测试全通过；真实子进程 + 真硬杀；BEGIN 后 COMMIT 前注入、原始连接对照。
- 结论：跨进程锁有效；并发写恰好一个写者；同计划重放、不同 scope 冲突；崩溃后 0 行、integrity/FK 正常、重开可再保存；WAL 未提交帧丢弃；关闭后连接/句柄/WAL 释放。
- 修复 F6-1：`migrate_archive_copy` 改用 `closing()` 并在返回前 `wal_checkpoint(TRUNCATE)`；新增“只拷主文件仍为 schema 3”回归。
- 修复 F6-2：`ArchiveLibraryRepository.save_archive_plan` 将 SQLITE_BUSY 翻译为 `ArchiveLibraryBusyError`。
- 记录 F6-3：单写者是应用层协作锁；绕过仓库直接用 `save_archive_plan(conn,…)` 仍可写，需文档化，不能宣称数据库级排他。
- 未覆盖：断电报错、网络盘/SMB、真实工具/数据、生产接线、>200 资产、杀软、长读检查点饥饿、其他 SQLite 版本、磁盘满/权限中途失败。
- 提交：`c1b5306`（迁移/忙错误）；F6 原始报告 `f6_agent/REPORT_F6.md`。

## 4. F3/F5 并行状态

- F3（AR2-03/04/07）长任务实现中；设计见 `docs/archiving/A1_IDENTITY_HANDOFF.md`。F3 允许修改 `PathRef.identity_key`，但 `target_key` 不变。
- F5 2,000 文件整链在独立 worktree `f5_worktree`（HEAD `4a9c3f3`）执行，避免与 F3 工作树冲突。
- F4 独立只读复审同时进行，专找 F4 语义反例。

## 5. 不可误读

- F1/F2/F4 只关闭对应离线问题；AR2-03/04/07 仍由 F3 未完成。
- 任何全量计数以实际 XML 为准；代理报告和主线复跑都保留。
- 没有真实图库、生产库、账号、密码、上传、删除、释放或网络操作。
- 旧审计、旧检查点标签和失败报告不覆盖。

## 6. 下一步

1. 收 F3、F5、F4 复审结果；逐项合并/拒绝并保留反证。
2. 修复 F3 后再做独立复审，随后 F5 整链门槛和生产接线评审。
3. 仍不接生产；A2–A6 保持独立验收。
