# JpgPng post-tidy 修复轮维护记录（2026-11-14）

本轮处理[整理后独立审计](../audits/2026-11-14-post-tidy/README.md)提出的 **PA-01（P1）、PA-02（P2）、PA-03（P2）** 以及一直未关闭的 **WR-01..07**。产品源码基线是日常 `4ba891e`，审计包落在 `5f19553`；源码本身与上一轮被审 `e4eb6ce` 相同，所以这一轮是**真正的产品修复**，不是又一批文档整理。

冻结条件见[契约 v6 修订 A1–A6](../optimization/2026-11-14/U1A_MAINTENANCE_CLOSURE_CONTRACT_V6_AMD1.md)；[契约 v6](../optimization/2026-11-14/U1A_MAINTENANCE_CLOSURE_CONTRACT_V6.md) 本身未改动。

## 1. 起始事实（自己实测，不引用别人的数字）

| 项目 | 值 |
| --- | --- |
| 日常冻结点 | `4ba891e`（`daily/u1a-w5-w6-20261114`） |
| 审计包 | `5f19553`（`audit/post-tidy-20261114`） |
| 集成分支 | `fix/a1-post-tidy-closure-20261114` |
| 审计 9 条反例 | **7 红 / 2 绿**，3.873 s（审计原件）/ 4.257 s（我复现），红 XML `red_baseline/red.xml` |
| 既有套件 | 2,149 项：2,145 通过、4 跳过；两个 Tk 展示文件各自独立进程 9/9 |
| 真实库 | 4,435,968 字节、`sha256 C0BA55B8…` |

## 2. 逐项关闭

| 编号 | 根因 | 修法 | 行为证据（测试名） |
| --- | --- | --- | --- |
| PA-01 P1 | `verify_backup` 三次独立打开，sealed/logical/health 没有共同版本 | 一次私有隔离副本（同级优先、系统临时目录回退）：边复制边哈希、复制前后 `fstat`+`lstat` 绑定、逻辑与健康共用同一只读连接与读事务、结束时再 stat 全组；失败 → `changed_during_check` | `test_a_sealed_backup_is_verified_from_one_isolated_copy`、`test_a_write_during_the_copy_is_refused_as_changed`、`test_a_write_made_inside_the_check_is_reported_as_changed`、`test_no_single_generation_can_satisfy_an_inconsistent_manifest` |
| PA-02 P2 | `name NOT LIKE 'sqlite_%'` 里 `_` 是通配符，`sqliteXuser` 被当系统表排除 | 唯一定义 `_is_internal_object`（字面量 `sqlite_` 前缀、大小写不敏感），对象枚举全部复用 | `test_the_internal_object_test_is_literal_and_case_insensitive`、`test_a_legal_sqlite_similar_user_table_is_fingerprinted` |
| WR-04 P2 | 清单语义不严：`copy_name` 可不在 sealed、`bytes` 不校验类型 | 先验清单再碰文件：`bytes/sha256/members/copy_name/sealed_members/-shm/mode/fingerprint` 全项校验，违规 → `manifest_*`、退出 3 | `test_a_seal_that_skips_the_database_is_refused`、`test_a_member_size_that_is_not_a_byte_count_is_a_typed_refusal`、`test_the_shared_memory_file_can_never_be_sealed` |
| WR-01 P1 | 恢复步骤是无条件覆盖主库 + 删除 WAL | 七步协议（关进程→先验备份→暂存匹配组→当前组整组移入保留目录→放入匹配组→重开对照→失败整组退回）；新增 `restore-check` 子命令判 `orphan_wal` | `test_the_online_protocol_keeps_the_old_group_and_restores_the_snapshot`、`test_a_leftover_wal_beside_a_restored_snapshot_is_refused`、`test_the_generated_steps_verify_before_they_move_anything` |
| WR-02 P2 | CLI 不加载 `output_roots_v1.json`，且拿库目录猜根 | `load_output_roots()` 只读加载：缺失=无外部根；定义非法（相对/重叠/链接/版本/解析/读不了）→ `mapping_unreadable` 退出 3；定义合法但目录不在 → 逐行 `root_unavailable` 退出 2 | `test_a_saved_external_root_matches_without_guessing`、`test_a_broken_mapping_is_a_typed_mapping_error`、`test_an_unmapped_root_never_falls_back_to_the_project_root`、`test_wr02_an_overlapping_root_is_refused_below` |
| WR-03 P2 | 201 行续跑末页冒称全库 | `scan_library_report`：默认全程、裸 `--after` 恒 `scope=continuation`/`coverage=partial`/退出 5、严重度单调 | `test_a_continuation_from_a_later_cursor_is_a_partial_walk`、`test_a_continuation_at_the_end_of_the_ledger_still_exits_five`、`test_a_missing_file_on_a_later_page_is_not_overwritten_by_a_green_tail` |
| WR-05 P2 | 面板只列 50 行、没有翻页 | 端口带 `after/next_cursor/truncated/total/read_revision`；面板上一页/下一页 + 「本页 N / 范围内 M」 | `test_controller_reaches_the_fifty_first_row_and_acts_on_it`、`test_the_fifty_first_row_is_reached_by_the_returned_cursor`、`test_controller_previous_page_returns_to_the_stored_cursor` |
| WR-06 P2 | 恢复命令没带用户所见身份 | `expected_delivery_id` 由控制器传入，后端在**任何文件观察之前**比对，不符 `state_changed` + `health=None` + 零观察 | `test_a_stale_delivery_id_is_refused_before_any_file_is_observed`、`test_the_matching_delivery_id_still_walks_the_observation_path`、`test_the_port_refuses_to_act_without_the_expected_delivery_id` |
| WR-07 P2 | 列表失败被当成空列表、summary 与 pages 非同一读取 | `loading/ready/error` 三态；失败清空行/游标/选择并保留刷新作重试；`read_revision` 标注两次读不同版本 | `test_controller_turns_a_failed_page_read_into_an_error_state`、`test_panel_error_state_removes_the_rows_and_every_action`、`test_controller_labels_a_header_read_from_another_transaction` |
| PA-03 P2 | 归档清单 `st_ino/st_nlink` 全为 0，「身份关系未变」无证据 | 采集器改用 `open`+`fstat` 取身份、`lstat` 复核路径；零值写 `identity: unknown`；两侧缺证据永不判等 | `test_plain_file_identity_comes_from_the_opened_file` 等 14 项（`.runtime/workspace_tidy_20261114_01/test_identity_capability.py`） |

## 3. 反例与回归

| 范围 | 结果 |
| --- | --- |
| 审计 9 条反例（`tests/test_u1a_post_tidy_boundaries.py`，与原件逐字节相同，仅 ROOT 一行与两处已记录的注入点/夹具偏差） | **10/10 全绿**（5.68 s；多出的一条是新加的“重叠根必须被拒绝”） |
| 主体全量（排除三个 Tk 模块，带 `TCL_LIBRARY`/`TK_LIBRARY`） | **2,229 项：2,225 通过、4 跳过、0 失败、0 错误，662.6 s**（基线 2,149/4 跳过，净增 80） |
| Tk 三个模块 | `test_collection_view.py` **25/25**（单文件进程连跑 3 次全绿）；`test_collection_presentation.py` 4 项 + `test_source_presentation.py` 5 项按既有方法**逐条独立进程 9/9** |
| 上一轮 WC 12 条边界 | **12/12**（3.51 s） |

## 4. 真实库与真实备份（只读）

驱动：`probe/real_rehearsal.py`；报告 `verify/real_rehearsal.json`。

| 步骤 | 结果 |
| --- | --- |
| 库字节与 `sha256` 前后 | **完全一致**：4,435,968 字节、`0cba55b8…4d3d`，前后逐字节相同 |
| `record_backups` 目录条目 | 11 个条目，前后**一字未增**（已核验的备份目录仍是同一个） |
| `inspect` 真实库 | exit 0：`compatible`、49/49 对象、`quick_check/integrity_check=ok`、外键 0 冲突、WAL 0 字节、`-shm` 32,768 字节（簿记文件） |
| `verify record_backups/pre-w5w6-proof`（v2 封存快照） | exit 0、`ok=True`；`sealed/logical/health/compatible` 四项全 `ok`；`evidence`：隔离副本落在同级目录、复制 4,435,968 字节、复制 0.0094 s / 回读哈希 0.0066 s / 读取 0.0868 s、`cleanup=ok` |
| `verify record_backups/pre-w5w6-switch`（v1 旧清单） | exit 2、`ok=False`、`manifest_too_old`：旧清单证明不了内容，如实拒绝（不是回归） |
| `scan` 真实库（全程） | exit 0、`scope=full_walk`、`coverage=complete`、`pages_examined=1`：11 行全部 `match`，读取 95,574,715 字节、0.25 s |
| 隔离副本残留 | 0 个：`record_backups` 的父目录下没有任何 `*.verify-*` |

## 5. 与审计原件的两处偏差（都写清楚理由，不悄悄改）

1. **WR-02 的夹具本身不合法**：审计把外部根放在项目根**里面**，而 `PathResolver` 明令禁止重叠根（同一批文件两个身份），桌面端也不会写出这种映射。合法形状（根在项目外）→ exit 0 / `match`，已作为正式用例；重叠形状 → `mapping_unreadable` / exit 3，另立 `test_wr02_an_overlapping_root_is_refused_below`。原夹具的断言（exit 0）无法被任何遵守该不变量的实现满足。
2. **PA-01 的注入点已不存在**：原件 monkeypatch `inspect_library` 并只在“路径 == 原备份文件”时写盘，而 A1 之后健康检查跑在隔离副本上、不再对原目录做任何读取。按开发指导允许的方式把注入点迁到“复制刚完成”的等价阶段，断言不变且更强（`changed_during_check` + `evidence.scope == "isolated_copy"` + 第二次核验必败）。

两处都在 `tests/test_u1a_post_tidy_boundaries.py` 里就地写明，审计原件 `docs/audits/2026-11-14-post-tidy/evidence/test_post_tidy.py` 一字未改。

## 6. 只读独立复审与处理

对照契约 v6 与修订 A1–A6 逐文件审 `git diff 5f19553..HEAD` 的只读复审结论：**P1 = 0**，这份树可以替换日常入口；剩余 1 项 P2 属“未证明的修复”、1 项 P2 与 6 项 P3 属文档/口径，逐条处理如下。

| 编号 | 内容 | 处理 |
| --- | --- | --- |
| P2-1 | Tk 拆除竞态只有权宜修复（`VerificationPanel.dispose()` 里一次主线程 `gc.collect()`），根因大概率在 `gui/collection_view.py` | **未关闭，登记为下一轮第一项**；本轮只声称“全量 + 三个 Tk 模块各自独立进程跑通”，不声称根因已修。加固稳定性结论需要同一顺序重复 N 次，本轮未做 |
| P2-2 | `IMPLEMENTATION.md:28` 仍写“WR-01..07 尚未关闭，本轮只整理目录和文档” | 已改（同一收尾提交） |
| P3-1 | `_scan_outcome` 成死代码，且内部“续跑 + empty → 0”正是 WR-03 的洞 | **删除该函数与其 4 条单测**（它们钉住的正是那段错误语义），并在调用点注释说明删除原因；等价行为由 `test_u1a_scan_full_walk.py::test_allow_unknown_releases_only_legacy_rows` 等承接 |
| P3-2 | `verify_backup` docstring 把保证写小了（`_recheck_members` 实际会抓“复制之后、最后一次 stat 之前”的写） | 已改成“复制到最后一次 stat 之间 → `changed`；stat 之后（含返回后）不在保证内” |
| P3-3 | `archive_schema.py:794`、`library_repository.py:905` 仍用 `NOT LIKE 'sqlite_%'` | 行为无害（只用来读名字再与已声明集合相交，从不判“多余对象是内部的”），已在两处补注释说明 |
| P3-4 | `scan` 文本的“需要你知道的 N 行”用的是截断后清单 | 改为打印 `stats.findings` 的精确计数，并注明下面最多列 20 行 |
| P3-5 | `restore-check` 对不存在的库打印 `None` | 已改为 `-`（状态与退出码不变） |
| P3-6 | `TASK_PLAN.json` 的 `active_workstream.status` 仍是旧审计态 | 已随收尾同步 |

复审自己跑出来的数字（它不是引用我的）：150 项（边界 10 + 生成 26 + 恢复 6 + scan CLI 24 + 身份 7 + 面板 77）全绿 25.82 s；187 项（scan walk 41 + 库维护 82 + 巡检 48 + 输出位置 16）全绿 81.76 s；审计原件 9 项仍 **7 通过 / 2 失败**（即 §5 的两处偏差）。它还独立确认了 `test_a_write_during_the_copy_is_refused_as_changed` 的注入点、`restore_check` 的 `library_missing`、以及“复制失败不判 ok”分支。

## 7. 明确未做与边界

- **真实库恢复未执行**；本轮只做只读预检、已有备份核验与合成演练。恢复操作卡仍需用户明确指令并另批。
- 归档采集器的 `apply()` **只在合成批次上跑通**，真实批次未演练；跨卷移动（身份必然变化）本机单卷无法演练。
- **600,447 个文件的搬迁前身份基线补不回来**：PA-03 只能关闭为“旧证明已更正 + 新采集器已验证”。另有 40 批没有搬迁前内容摘要。
- `verify` 的隔离副本是显式代价：一次完整复制（峰值磁盘占用不超过备份目录大小），进程被强杀会留下 `<备份名>.verify-<hex>` 目录。
- 清单摘要只证明“相对清单未被改”，**不是**抗同时篡改库与清单的签名。
- `read_revision` 取 `MAX(event_seq)`，是“这次读的版本”，不是每行变更计数器；没有追加事件的变更可能保持同值。
- 仓库层 `recover_verification_page(expected_delivery_id=None)` 仍保留旧调用者语义，只有桌面端口强制必填。
- 核验面板仍是单页 50 行，只有上一页/下一页，没有跳页；`repair_queued` 不计入已验证。
- **Tk 拆除竞态（P2，未关闭）**：未改动的 `5f19553` 在全量主体里出现过原生异常 0x80000003；本轮在 `VerificationPanel.dispose()` 里加了一次主线程 `gc.collect()`，缓解但没有证明根因。现场证据：同一文件在一个进程里连开多个 Tk 根会随机失败（`_tkinter.TclError: Can't find a usable tk.tcl`，前一个解释器被 finalize 之后无法重新初始化）——整合树连跑 3 次得 6 绿 3 红，**基线 `4ba891e` 同样 1 绿 2 红**，所以这是环境级竞态，不是本轮引入的回归。因此 Tk 展示类按基线方法逐条独立进程验收（9/9），根因排查留给下一轮。
- 未合并 main、未部署、未推送业务数据、未触碰真实图库与账号。

## 8. 回退

代码：`git checkout daily/u1a-w5-w6-20261114`（回到 `4ba891e`），或退到上一检查点标签 `checkpoint-2026-11-14-u1a-w5w6-proof`（`e4eb6ce`）。日常目录本轮不改产品代码，除非另行切换。
