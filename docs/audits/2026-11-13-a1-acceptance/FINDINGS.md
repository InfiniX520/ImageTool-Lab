# A1 接受事务复审：问题明细

日期：2026-11-13；产品 `2ccabcad4904349e045c4453eadb55de18c2b581`。P1 是生产接线前优先消除的错误接受/状态覆盖；P2 是可靠性、边界一致性、性能或集成交付缺口。以下均不声称生产数据已经出错。

证据根为工作区 `.runtime/jpgpng_a1_acceptance_audit_20261112_01/`；[索引](../../../../.runtime/jpgpng_a1_acceptance_audit_20261112_01/README.md)。`probes02/results.json` 为修正后的探针结果；首次 `probes01` 的两个 TypeError 是审计脚本漏传构造参数，不是产品问题，原件保留。

## AR4-01 · P1 · expected-old 没有覆盖接受事件的版本

位置：[ContentResolution](../../../core/archive_scope.py#L783)、[apply_content_resolution](../../../core/archive_schema.py#L1480)、[bind_archive_path](../../../core/archive_schema.py#L1282)。

**自然操作序列，无伪造 resolution、无修改数据库：**

1. 路径上的物理文件 A，字节 AAAA，接受为 revision 1。
2. A 原地变成 BBBB，真实观察并解析为 revision 2，但先不提交该决定。
3. 用物理文件 B 替换同名路径，B 的字节为 AAAA。正常解析要求确认；确认并接受后，内容仍为 revision 1，但接受事件与物理身份已更新为 B。
4. 应用第 2 步的旧决定。写入成功，把当前绑定推进到 BBBB/revision 2，并把物理身份改回已被替换的 A；实际路径仍是 B/AAAA。
5. 维护审计仍通过，因为每条记录各自能关联到其旧观察，缺少接受链上的版本约束。

证据：`probes02/results.json → stale_accepted_event`。`delayed_reuse_resurrects_identity` 是更小的同内容变体：延迟 REUSE 能将当前接受的物理 B 改回 A，内容键始终没变。

根因：`ContentBinding` 已有 `acceptance_id/root_revision`，解析输出却只保留 `previous_content/expected_binding_absent`。写时只比较内容键；同内容替换、根变更或再次认可不会改变内容键。因此“当前值等于 expected 内容”并不说明绑定未变化。SQLite 单 writer 只能串行执行，不能自动识别串行到达的陈旧决定。

**通过条件：**解析输入携带确切绑定代次/接受事件；写事务在任何插入前比较它。旧接受变化后即拒绝并重新观察/解析，不悄悄采用当前值。真正重放已成功的同操作可以返回原结果，但不得把当前指针倒回旧事件。检查缺席→存在、同内容不同物理对象、根版本改变、两个不同内容竞争与操作重放。

## AR4-02 · P1 · 计划保存仍是另一条可推进绑定的写路径

位置：[save_prepared_plan_in_transaction](../../../core/archive_plan_store.py#L1009)，尤其 1097–1121；[仓库两种命令](../../../core/archive_repository.py#L93)。

证据：`probes02/results.json → stale_plan_bypass`。先由真实观察构造 BBBB/revision 2 的计划，随后正常确认同名 B/AAAA/revision 1，再保存旧计划。**保存成功，当前绑定变成 BBBB/revision 2，acceptance_id 变 NULL**，实际文件仍为 AAAA；维护审计通过。

计划保存没有原决定的 expected 令牌，允许“相同资产且 revision 更大”就推进，并把刚读出的当前键当 expected。它调用不带 acceptance_id 的 `bind_archive_path`，新接受证据被清空。`apply_archive_resolutions` 本身的安全检查不能覆盖这条路径。

这部分已经被 N1/N2 设计文档诚实列为 N3 未完成，并非本轮声称修复后又凭空发现的全部新问题；但对 A1 的生产入口是实际阻断。`prepare_observed_plan` 的 snapshot/mtime 检查确实修好了，不能因此推断接下来的保存也有原子接受保护。

**通过条件：**建立受控“接受 + 保存观察计划”应用命令，或让纯计划保存明确只追加索引而不改变当前接受绑定。实际物理 I/O 在长事务外；数据库内 recheck expected、内容/观察/接受/计划持久化在同一工作单元。失败全部回滚，旧计划仍可读，重放既有计划不回退当前绑定。不要把未观察的 `user_selected` 索引改称已验证事实。

## AR4-03 · P2 · 幂等快速路径同时有误拒绝和漏拒绝

位置：[apply_content_resolution](../../../core/archive_schema.py#L1521)、[_accepted_binding_acceptance](../../../core/archive_schema.py#L1588)、[record_path_acceptance](../../../core/archive_schema.py#L1201)。

两个反例：

- `reuse_replay`：已有基线后再真实观察同一文件，得到 REUSE，应用成功；用同一 resolution/ObservedFile 重试，只改变调用时 `bound_at`，报 `ArchiveSchemaError: Acceptance identity already has different facts`。该分支 expected==current，因此绕过只在 expected 不匹配分支执行的幂等返回；确定性 acceptance_id 相同，后来接收时间却不同。
- `changed_replay_facts`：首次 BASELINE 已成功后，用原候选真实读取相同字节、同 check_id、不同 run_id；配原 resolution 调用，直接返回旧 acceptance。未比较新输入的完整观察事实，`record_local_observation` 的同 ID 冲突检查被跳过。这个例子专门验证错误复用操作 ID 的输入，不能说成自动产生了假摘要或新的数据损坏。

已有“NEW_REVISION 重放”测试通过；它没有覆盖 REUSE 路径，所以全绿不能关闭幂等问题。

**通过条件：**先规范化并比对操作/观察事实，再统一幂等判定；将首次提交时间与请求事实分开。BASELINE、REUSE、NEW_REVISION、两种 REPLACEMENT 的同请求重放返回首次结果，不新增行；同 ID 任意事实改变明确冲突。操作已成功但当前绑定后来推进时，重放可以返回历史结果，不能改写当前指针。

## AR4-04 · P2 · 多路径历史仍会分配已占用的内容代次

位置：[首次绑定与同对象变化解析](../../../core/archive_scope.py#L923)、[find_known_content](../../../core/archive_schema.py#L1385)。

`hardlink_lag`：A/B 是两个硬链接名，均接受同资产 revision 1/AAAA。文件原地变 BBBB，只复扫 A，A 到 revision 2；再次原地变 CCCC，只复扫 B。B 从自己的 revision 1 推导 revision 2，撞上已保存的 BBBB，抛 `Content identity already has different facts`。重扫 B 仍使用滞后的绑定，不能自行恢复。旧行未被覆盖，这是安全拒绝，但正常分批工作无法推进。

`rename_after_edit`：接受 A/AAAA 后，原地改 BBBB 并改名 C。文件 ID 未变，新路径无绑定、该摘要未知，首次分支再次分配 revision 1，与旧内容冲突。

根因：枚举器以物理对象生成 scope asset ID，代次却按单路径旧绑定分配；摘要查询只返回全库排序后的第一行，不能代表该资产自己的完整历史。对于已有同资产同字节候选，还需避免其他资产的先命中遮蔽正确代次。

**通过条件：**先确定逻辑资产、物理对象、路径的关系以及硬链接分叉/改名策略，再设计资产级代次分配。不得仅增加 MAX+1 便声称身份语义已完成。需要测试滞后路径连续变化、先改名后扫描、硬链接被独立替换、恢复旧内容、其他资产相同字节干扰。原资产和计划历史不覆盖；无法自动判定时提供结构化决定，不持续抛不可变键冲突。

## AR4-05 · P2 · 别名修复未贯穿计划保存，混合字符键仍不一致

位置：[ensure_archive_asset](../../../core/archive_schema.py#L1152)、[计划保存的精确 path_json 查询](../../../core/archive_plan_store.py#L1070)、[计划重载检查](../../../core/archive_plan_store.py#L835)、[PathRef](../../../core/task_contracts.py)。

- `alias_plan`：先选择 `gallery/A.bin` 接受，再单独选择同一文件 `gallery/a.bin`。解析和接受都成功 REUSE；`prepare_observed_plan` 成功；保存失败 `ArchiveContentConflictError: Existing asset has different immutable facts`。计划先按精确显示拼写查资产，查不到后以 `user_selected` 再插；既有行为是 `observed_existing`，于是来源冲突。即使只改这个分支，重载 `_check_assets` 还要求原 path_json 完全相等。
- 独立测试 `test_ar3_05_mixed_script_alias_selections_share_one_identity` 失败：`画A.bin/画a.bin` 同一 Windows 目录项，两次选择产生不同 identity_key。联合选择的物理去重通过，不代表跨批次一致。

**通过条件：**明确规范目录项、别名记录及恢复拼写，枚举/绑定/观察/receipt/计划保存/重载使用同一语义；不改写旧计划原文。ASCII、混合字符、根 ID 大小写、NFC/NFD、straße/strasse、硬链接分别检查。不能简单 Unicode 全量 casefold，也不能为了绕过来源冲突而改变已保存的 source_kind。

## AR4-06 · P2 · 公开低层写接口能制造维护审计随后拒绝的状态

位置：[record_path_acceptance](../../../core/archive_schema.py#L1201)、[bind_archive_path](../../../core/archive_schema.py#L1282)、[load_content_binding](../../../core/archive_schema.py#L1341)、[维护接受校验](../../../core/archive_schema.py#L799)。

这是输入边界测试，**不是正常高层命令已知会传错参数的证据**；但低层接口已经公开且可独立提交，应在写入前遵守自身不变量。

`write_audit_boundary01/results.json`：

1. 各自建立正常 A/B 接受记录。调用 `bind_archive_path` 给 B 的同内容绑定传入 A 的 acceptance_id。接口仅检查该 ID 存在，写成功，SQLite 完整性/外键都通过；`load_content_binding(B)` 实际返回 A 的物理身份。随后全库审计才报 `Path binding does not match its acceptance`。
2. 正常存储一个新的诊断检查，再调用 `record_path_acceptance` 把当前内容键作为自己的 expected_content_key。接口写成功；维护审计报 `Path acceptance expects a generation that cannot precede it`。全库审计的新约束没有同步到写入口。

**通过条件：**抽出接受事实校验，写/读/维护共享。绑定必须匹配 acceptance 的 path/content/asset/revision/root 关系；expected 若按现设计表示被替代的内容，必须确为同资产旧代次；若 R1 改为绑定事件令牌，则按新语义统一重写，别保留两套解释。拒绝发生在任何写入前；加入同内容跨路径、同路径跨代次、其他资产、未来代次、空/未知接受 ID 的测试。

## AR4-07 · P2 · 摘要与路径所有者查询仍随历史全扫

位置：[find_known_content](../../../core/archive_schema.py#L1398)、[ensure_archive_asset 所有者检查](../../../core/archive_schema.py#L1122)、[索引 DDL](../../../core/archive_schema.py#L535)。

这轮增加接受事件索引，且原“取最新诊断物理身份”的查询已被删除；不能继续用旧诊断查询作当前瓶颈证据。实际仍有：

- `sha256 + size_bytes ORDER BY asset_id, revision`：`SCAN archive_contents`、临时排序树。
- `path_key → DISTINCT asset_id`：扫描以 asset/revision 为前缀的索引，非按 path_key 定位。

| 历史行数 / 本轮查询数 | 摘要 VM 指令 | 所有者 VM 指令 | 固定 10 次摘要 | 固定 10 次所有者 |
| --- | ---: | ---: | ---: | ---: |
| 100 / 100 | 32,704 | 31,200 | 3,270 | 3,120 |
| 200 / 200 | 125,404 | 122,400 | 6,270 | 6,120 |
| 400 / 400 | 490,804 | 484,800 | 12,270 | 12,120 |

证据 `sql_growth01/results.json`：真实 schema 4 临时库、合法索引事实，SQLite progress handler 每指令计数；固定 10 次查询也随历史增加。不是 500 GB 运行时测量，小样绝对耗时未构成当前卡死证据。

**通过条件：**先完成 R1/R2 查询语义，再设计覆盖摘要、资产历史、路径所有者与接受令牌的索引；保持 DDL 版本可辨识。用 EXPLAIN、固定批量/增长历史和写入成本证明收益。不要删历史、跳过摘要或放宽校验去获得更快数字。

## AR4-08 · P2 · 独立测试、合并版本和当前文档尚未闭合

事实：

- 主树本轮 3 个提交改了产品与专项测试，但 `TASK_PLAN.json`、`LOG.json`、联合入口等仍指向 `705a990`，最后日志仍为上一轮审计；不能由一个专项设计的“已实现”代替当前总体状态。
- B 分支 `6cad61a0be67abbabddcd62e976a011607f2fae2` 独有 `tests/test_ar3_lifecycle.py`。它未进入 `2ccabca` 的现有全量测试，本轮逐字复制后独立跑是 22 过/1 失败。主树全量也恰为 1,415 项，B 旧报告总数同为 1,415，但测试组成和基线不同；按数字判断“已合并验证”会误判。
- 本轮确有独立 worktree 和 A/B/C 所有权表；不能说没有并发隔离。缺口是 B 交付未纳入合并退出条件和当前进度。
- schema 已升 4，仓库/计划层文档字符串与错误提示仍写 schema 3；新专项设计有一处错误相对链接。这些文案不会改变实际版本检查，但会误导接续。

本审计同步当前入口、修正专项链接并留存 worktree 索引，**不擅自合入失败测试或执行修复**。独立测试纳入版本和产品提示修订仍是下一轮工作，所以本项不标全部关闭。

**通过条件：**主集成者接收具体 SHA，独立断言在新树复验并进入正式测试或以明确理由合并等价覆盖；保留旧版失败证据，不删掉红项制造全绿。最后一个产品提交后冻结摘要、跑专项/全量、同步唯一当前入口/LOG，再提交。历史报告不改成新结果；未提交 worktree 不删除。
