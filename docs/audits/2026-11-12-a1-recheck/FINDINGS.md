# A1 修复后复审问题

日期：2026-11-12。所有产品问题本轮未修；AR2-08 的导航、定位和当前状态由本轮整理，仍需后续批次遵守交接门槛。范围与测试见[总览](README.md)，执行步骤见[开发指导](DEVELOPMENT_GUIDE.md)。下面的行号对应本轮冻结源码，后续以函数名及快照核对。

## AR2-01 / P1：强制凭据只在写入侧成立

位置：[archive_plan_store.py](../../../core/archive_plan_store.py) 的 `_decode_scope` 614–673 行、`_load_in_transaction`；[archive_schema.py](../../../core/archive_schema.py) 的 `validate_archive_facts`。复现：`missing_persisted_receipt_is_accepted`。

正常带 ScopeReceipt 保存计划后，只将 scope_json.scope_receipt 改为 JSON null，保留所有成员、计数和摘要。load_archive_plan 成功返回 scope_receipt=None，inventory.scope_complete=True；显式 validate_archive_facts 也接受。

原因是 `_decode_scope` 允许 null，只有非 null 才验证；全库审计仅检查计划头为合法 JSON，并没有调用计划协议校验。当前计划版本仍是同一 version 2，写入口却宣称全部强制凭据，形成读写语义不一致。

这不是证明数据库遭攻击；它是损坏、中断升级或后续写入口出错时，读取端没有守住不变量。不能以“正常 save 不会写 null”替代损坏数据的拒绝行为。当前版本必须要求完整凭据；若确需接纳历史无凭据计划，应单独协议版本、明确降级状态，不允许伪装成当前完整计划。普通读取、仓库读取、重放、维护审计均须一致。

## AR2-02 / P1：当前批次校验漏掉它实际引用的内容行

位置：[archive_plan_store.py](../../../core/archive_plan_store.py) 的 `_check_assets` 704–744 行、`_load_in_transaction`、`save_prepared_plan_in_transaction` 已有批次重放分支；[archive_schema.py](../../../core/archive_schema.py) 的 `_require_content` 和 `validate_archive_facts`。复现：`referenced_content_corruption_bypasses_hot_validation`。

正常保存一个已知摘要的计划后，在临时库把 archive_contents.sha256 改为 NULL。asset 行和计划清单仍保留原摘要。普通重载成功、相同计划重放也成功；显式全库审计则正确拒绝 `Archive asset identity does not match its facts`。

将全库扫描从每次操作移走是合理优化，但“只校验当前批次”必须包括它的引用闭包。现在只拿资产行与 JSON 相比，未证明资产所引用的 archive_contents 同一 asset/revision/size/hash/content_key 一致。SQL 外键只约束身份存在，不能替代这些事实。

修复应在当前批次上做 JOIN/定向关联检查，或复用严格公共谓词；正常保存、重载、重放不能各写不同程度的判断。不要恢复每次全库扫描来掩盖缺口，也不要删除全库审计。补内容摘要/大小/键、关联收据、当前路径绑定的针对性篡改矩阵，并明确哪些历史计划不依赖当前绑定。

## AR2-03 / P1：快照与计划之间只比路径和大小

位置：[archive_scope.py](../../../core/archive_scope.py) 的 `_entries_digest` 455–461 行、`ScopeReceipt.matches_entries`、`consume_scope_pages` 1800 行附近；[archive_plan_store.py](../../../core/archive_plan_store.py) 的 `_scope_entries`、`_receipt_json`。复现：`scope_receipt_does_not_bind_identity_or_version`、`source_verification_ignores_physical_identity`。

两个独立反例：

1. 真正通过枚举器验证的凭据保持不变，将同路径同大小条目的物理身份改为另一 device/file_index，并改成另一 asset/revision/hash，prepare_archive_plan 仍接受。
2. 在交给 consume_scope_pages 的页中只换 physical_identity，仍提供原 source；返回 snapshot_verified=True。原因是这里只比较 path/content，而 ArchiveEntry.physical_identity 被声明 compare=False，且没有额外物理检查。

枚举时本来没有完整内容摘要，因此不能简单要求“枚举 hash 与最终 hash 一致”。但必须证明观察的是那次枚举的具体文件，并定义 baseline→内容代次解析的合法变换。现在 `_entries_digest` 仅包含相对路径和大小，计划层还主动丢弃物理身份重建条目，做不到这一点。

需要两个明确交接事实：枚举候选的原始路径/根版本/物理身份/大小/mtime，以及观察结果到内容代次的解析记录。物理文件被替换或 mtime 改变应重新枚举/观察或报冲突；不能默认用同路径同大小套旧凭据。HMAC 可选并不能弥补被签名数据缺字段，不应为了过测自建密钥服务。

## AR2-04 / P1：真实文件变化还没有走到持久化代次

位置：[archive_scope.py](../../../core/archive_scope.py) 的 `_capture_file` 1521–1566 行；[archive_schema.py](../../../core/archive_schema.py) 的 `ensure_archive_asset` 路径 owner 检查与 `archive_asset_path_owner` 触发器；[archive_plan_store.py](../../../core/archive_plan_store.py) 的路径绑定更新。复现：`actual_scope_has_no_content_revision_resolver`、`new_physical_file_at_historical_path_is_blocked`。

同一合成 image.png 首次枚举→摘要→保存成功。随后原地从 AAAA 改为 BBBB（等长），再次走真实枚举：asset 基于 inode 仍相同、revision 仍为 0，第二批因 `Content identity already has different facts` 失败。改成 os.replace 放入新的同名文件，则 asset 因物理身份变化而不同，第二批因 `Path already belongs to a different asset` 失败。旧批次和内容保留，回滚正确。

人工构造 revision=1→2 的测试已经通过，但并没有提供“谁决定新 revision”的实际入口。观察结果持久化也要求内容行完全一致：不能先保存未知摘要代次，再用同代次新摘要替换它。schema 3 支持部分模型不等于真实流程已闭环。

不要删除不可变事实检查或让任何新资产自动抢占旧路径。应先定义逻辑资产、物理文件、路径历史与当前绑定：由集中解析器决定复用、新代次、明确替换/冲突，条件更新绑定，旧归档继续指旧内容。对正常替换仍需记录明确来源/操作意图；不要求所有不同 inode 都自动接受。若本期决定暂不支持，必须返回结构化待处理原因、可重试动作，并明确该范围不能算已收全；不能把它写成 A1-07 全部关闭。

## AR2-05 / P2：重载的资源边界与严格解析缺口

位置：[archive_plan_store.py](../../../core/archive_plan_store.py) 的 `_decode_scope` 641–665 行、`_inventory`；[archive_schema.py](../../../core/archive_schema.py) 的 `validate_archive_facts`。复现：`reload_exceeds_prepare_budget`、`duplicate_json_key_in_plan_is_accepted`。

保存一成员合法计划后，为清单追加 9 MiB 空白，分成 48 个仍小于段上限的段，更新头中的长度/摘要。实际文档为 9,438,687 字节，超过准备入口 8,388,608 字节上限；重载成功，全库审计也接受。当前实现先 fetchall 所有段再 join/encode/hash，没有累计上限。一个小 JSON 空白反例足以说明边界未应用；本轮没有扩大到危险内存规模。

另一个反例在清单中增加重复 batch_id 键，重算摘要，重载接受。原因是 inventory_json 使用普通 json.loads，而头部和其他契约用的 normalize_json 会拒绝重复键；同一“严格协议”的解析路径不一致。

读入口先检查头的版本/条数/累计字节上限，再有界读取段、逐段计费、摘要，最后用共同严格解析器解析。保存、重载、全库计划审计共用约束。不要先读完再检查大小。现有全库事实审计没查 segment 完整性，应明确组合审计入口和循环依赖边界，不能继续把“JSON 合法”叫“计划完整性通过”。

## AR2-06 / P2：实际 I/O 仍二次增长，页内中止未闭合

位置：[archive_scope.py](../../../core/archive_scope.py) 的 selections.sort（约 1340 行）、`_raw_entry_exists`、`page`、`_check_snapshot_root`、`_check_directory_membership`、`_read_names`。

**显式文件选择**：每个选择为判断大小写真实拼写调用 _raw_entry_exists，逐个扫描同一个父目录。80/160/320 文件访问目录项 3,240/12,880/51,360，身份比较仍只有 80/160/320。原测试只看后者，不能证明整体 O(n)。

**分页复核**：40/80/160 个子目录各一文件、page_size=1，首屏之后分别额外 lstat 1,872/6,952/26,712 次。每页遍历所有已选目录，复杂度受目录数×页数控制。正常 page_size=100 会缓解，但不能据此称分页代价与条目数无关。

**页内取消/预算**：在下一页开始时 event 未设置，随后第一个 lstat 将 event 置位，并推进注入时钟；此页仍返回 1 条目且 full=True、无问题。整个 guard 中累计 16 秒模拟执行量，策略 max_seconds=1，未检查。此为文件 I/O 之间缺少检查，不要求程序能强行中断一个阻塞的 OS 调用。

修复应缓存目录原始名称集合（一次扫描与身份/状态绑定）、显式选择按父目录分组；将稳定性复核安排为有界验证会话/批次门槛，清楚标明消费完成不等于刚刚再次验证所有目录。目录变化仍需拒绝，不能简单删掉检查。页内循环/扫描应检查取消与本次工作预算，并在返回前检查；对不能中断的 OS 调用如实定义响应界限。加入真实 scandir 迭代项、lstat、时长、内存指标，保留原字节和完整摘要。

## AR2-07 / P2：新路径键没有一致的大小写规则

位置：[task_contracts.py](../../../core/task_contracts.py) 的 PathRef.identity_key，121–135 行；[archive_scope.py](../../../core/archive_scope.py) 的 selection_relative；[archive_plan_store.py](../../../core/archive_plan_store.py) 的 `_receipt_json`。复现：`mixed_unicode_case_identity_inconsistency`。

画/A.png 与画/a.png 在旧 target_key 中相同，在 identity_key 中不同，因为“整个路径只要含非 ASCII 就原样保留”。external root_id 的 synthetic/Synthetic 也分别成键，而范围选择检查允许 root_id.casefold 相同，后续凭据核对却要求完全相同。相同路径语义在不同接口下不一致。

这里的反例证明键不一致，不声称每种文件系统都把两个路径视为相同，也没有修改 Windows 大小写模式。NFC/NFD 的不同物理文件保护必须继续。应依据实际路径身份/根注册契约处理别名，或在入口统一规范 root ID、对无法确定的路径明确冲突。不要直接恢复全 Unicode casefold/NFC 合并，不要未经影响分析替换整个下载器 target_key。共享契约的新增属性也需对应独立测试，不能只在归档场景间接覆盖。

## AR2-08 / P2：当前接续和证据定位不可靠

可核对的具体问题：

- 上级 `.runtime/jpgpng_a1_repair_20261112_01/` 主要是合成测试/目录；真正的 XML、validation.json 和源码检查点在项目内 `JpgPng/.runtime/jpgpng_a1_repair_20261112_01/`。机器计划把 validation 指到上级不存在的文件。
- 总表说没有公开 require_receipt 旁路，TASK_PLAN 当前 next_step 仍要求决定是否去掉 require_receipt=False；当前章节同时保留旧 250/5/3 和修复后的状态，未明确哪一节应优先。
- scope_independent04.xml 实际为 94，文档为 92；维护记录同时写 10k 短名被拒又写“短名可通过”，内存比例同时出现 6.63× 与 2.9×。这些不能原样交给下一模型做门槛。
- `checkpoint_review_fixes_20261112` 的 archive_plan_store.py 摘要与当前不一致。当前全量复跑通过可以支持当前树，但不能改写旧 manifest 假装一直一致。

本轮已补准确索引、纠正活动状态，并明确历史章节。保留两级现场及原报告，不搬动测试库或覆盖完成基线；每批必须有唯一当前入口与完成源码摘要。实际源码版本与完整回归报告必须绑定；目录名称相似不代表引用正确。此项的整理部分由本审计解决，未来新批次的执行纪律仍须验收。
