# 独立发现：固定于 9ee6206

6 类动态证实的问题：1 P1 + 5 P2。WR-04、WR-06、PA-03 沿用旧编号；CL-01..03 为本轮编号。12 个失败用例不等于 12 个独立缺陷。每项均列明触发前提，未将合成故障说成真实数据事故。

## CL-01 / P1：临时目录清理无成员所有权，且会穿越 junction

位置：[library_maintenance.py](../../../core/library_maintenance.py) 497–504 行 `_discard`；调用点 793、866、1775 行。前两个用于备份失败清理，后者是本轮新增隔离核验的 finally 路径。

`_discard` 的注释说“只删除本次创建的内容，不沿链接”，实际却枚举目录中当前存在的所有成员，遇目录递归，其他一律 unlink。它没有记录本次创建了哪些成员，也没有验证目录/成员仍是原对象。Windows junction 在 `DirEntry.is_dir(follow_symlinks=False)` 下仍能进入目录分支；这个参数并不替代重解析点检查。

动态证据：

1. 在 `_verification_workspace` 建好私有目录后插入一个非核验器创建的普通文件。`verify_backup` 返回 `ok=True`、`cleanup=ok`，文件却消失。
2. 同阶段放入指向本批另一合成目录的 junction；目标目录中的哨兵文件被删除，核验仍返回成功。两端路径均验证位于本用例临时根内，未指向用户资料。

影响是条件性的：正常没有陌生成员时不等于会丢图；但一旦目录中出现陌生文件、重解析点或对象替换，清理就超出本操作的所有权。核验通常被认为是安全检查，这使问题达到 P1。备份入口的旧调用也要一起修，不能只对 verify 包一层。

修复应持有操作所有权与成员身份清单；清理只处理仍匹配的自有成员。发现未知项、链接、目录身份变化或不可读就保留并报告；不递归删除未知目录。新临时目录、路径前缀判断、`resolve()` 一次检查或换成通用 `rmtree()` 都不是完整所有权证明。原子打开/身份复核与 Windows 重解析点边界必须纳入设计；无法证明时宁可留下有说明的残留。

证据：`test_cl01_verify_cleanup_preserves_an_unowned_file`、`test_cl01_verify_cleanup_does_not_follow_a_junction`，2 失败。

## WR-04 / P2：清单校验改善，但整个操作的失败协议未收口

位置：同文件 787–803 行复制阶段 try、804–934 行其后阶段、1168–1187 行 `_safe_member`、1709–1714 行暂存区错误、CLI `verify` 分支。

四个互补反例均为真实边界注入：

| 故障位置 | 实际结果 | 要求 |
| --- | --- | --- |
| 已完成复制后，`_write_manifest` 抛磁盘满 OSError | 裸 OSError 逃逸，已建目标残留且没有成功清单 | typed 主错误；明确残留/清理事实，不能误认为已生成可恢复备份 |
| 复制先失败，清理再抛 PermissionError | 清理异常覆盖复制主因 | 主因保留，清理失败单独附加；控制中断语义也要保留 |
| JSON 成员名称含 `\u0000` | Path.resolve/stat 抛 ValueError，CLI 没有输出约定的 JSON 错误 | 在文件操作前规范化为 `manifest_*`、退出 3 |
| 私有核验暂存区无法建立 | `ok=False` 正确，但 CLI 退出 2 | v6 AMD1 第 2 节指定不可建立为退出 3，不能归为已观察到内容不符 |

这些问题不会把所有失败变成成功；主要损害可诊断性、CLI 协议与安全残留处理，故为 P2。第一条没有要求“失败就必须删光”：在 CL-01 原则下，保留并准确记录未完成目录也是合格修法。不能为了让失败目录为空而扩大删除权限。

清单大小/密封集合等已修部分继续承认；本轮不能把“严格 JSON 校验”当作整个备份生命周期异常处理已关闭。

证据：4 个 `test_wr04_*`，4 失败。测试不捕获并吞掉原异常；故障 hook 已确认实际到达，避免因前置路径规则先拒绝而产生假通过。

## CL-02 / P2：restore-check 对损坏、空白和不兼容库仍给成功

位置：[library_maintenance.py](../../../core/library_maintenance.py) 616–654、2692–2695 行。

`restore_check` 拿到 `inspect_library` 的完整状态后，只把 missing 和“非 WAL 主库旁存在非空 WAL”当作失败。其余一律 `state=ok`，CLI 只按这个字段返回 0。因此 `status=unreadable`、`empty`、`incompatible` 与 `state=ok` 同时出现。

三个真实合成输入分别是非 SQLite 字节、零字节文件、只含 unrelated 表的 SQLite。三个调用均返回退出码 0，独立测试均失败。

完整恢复协议还有 inspect/compare 等步骤，本审计没有执行真实恢复，也没有证明这三种输入会绕过所有步骤；不能夸大为“已经恢复坏库”。但独立辅助命令的成功含义与“可打开的一致状态”描述不符，会误导人工与后续自动化门禁。另有静态邻接边界：WAL stat 失败被当作 0 字节，应在修复时一并覆盖，尚不单列为已动态证实的新问题。

应分开内容健康、当前 schema 兼容、文件组一致与未能观察。成功条件明确组合；不可读不能推断“没有 WAL”，未知不能当 ok。即使主库健康且处于 WAL 模式，仅凭 header/大小也不能证明任意 WAL 属于它，不要扩大成完整来源证明。

证据：`test_cl02_restore_check_cannot_approve_an_unusable_database[corrupt/incompatible/empty]`，3 失败。

## CL-03 / P2：预算中断的扫描仍声称 complete_prefix

位置：[library_scan.py](../../../core/library_scan.py) 460–470 行 `_resume`。

实现以 `walk == full_walk and coverage != error` 初始化前缀已完成；预算耗尽但有下一游标时保持 True。冻结契约明确：没有前缀链证明前 `resume.complete_prefix` 恒为 False。

合成发布 2 个 artifact，设置 `ScanBudget(max_files=1)`：结果 `coverage=partial`、只 examined 1、有下一游标、`complete_prefix=True`。退出码为 5 是正确的，本轮不把退出码误报为 0。问题是机器报告发出了它没有证明的前缀证书，后续接续实现可能错误信任它。

本轮最小修法是保持 False，并清楚区分游标提示与可合并证明；不需要趁机新增持久化 resume、DDL 或完整会话系统。若以后实现证明链，应独立设计库身份、版本、筛选/策略、根映射、前缀摘要与断点连续性。

证据：`test_cl03_unproven_scan_prefix_is_not_certified`，1 失败。

## WR-06 / P2 剩余：刷新没有更换查询代次，旧响应仍能显示

位置：[collection_controller.py](../../../gui/collection_controller.py) 804–850 行 `_refresh_verification(reset=True)`，以及 summary/pages 的回调检查。

reset 清空游标和行，但没有增加 `_verification_epoch` 或移除旧 `verification_pages` watcher；由于旧 key 仍在 `_pending`，也没有发出新的查询。若旧响应带相同 batch/cursor，当前校验会接受它。

合成时序：进入 batch-a 核验页 → 第一页 Future 尚未返回 → 点击刷新 → 让刷新前的 Future 返回旧行 → poll。显示中仍出现该旧行，查询请求总数仍只有 1。这里不是后台线程直接更新 Tk，也不是旧作业切换问题；是同批同游标刷新无法划开请求代次。

`expected_delivery_id` 的后端前置比对已修，因此尚未证明此缺陷会恢复到错误文件，保持 P2。用户点击刷新后却得到刷新前读取，且错误回调也可能覆盖新状态，仍需关闭。

建议使用独立查询 generation，刷新立即失效旧 summary/pages watcher，并排入新请求；成功与错误回调都核对 generation。不要只在 callback 中递增，也不要把取消展示等同于取消已经提交的恢复写入；生产者 Future 的取消是否安全须单独核对。

证据：`test_wr06_refresh_drops_the_previous_inflight_page`，1 失败。

## PA-03 / P2 剩余：句柄身份已知、路径身份未知时，仍记录身份 ok

位置：本机 `resume_archive.py` 78–100、113–140 行。固定源 SHA256：`7706883ab8855dd1430b9e6c51685a07d70e7498ffb13d28c2e933f7dd692625`。这是本轮检查到的新版工具，不能拿旧工具实现替代它审计。

`binding_problem` 遇任何身份三元组含 0 时返回 None，意思是无法证明绑定。但调用者最终只依据 `identity_of(after)`（已打开句柄）生成 `identity`，丢掉了路径绑定的 unknown。句柄能够说明哈希读的是哪个对象，不能单独说明“这个路径最后仍指向该对象”。

反例保留真实打开句柄的有效 fstat，仅将读完后的 lstat 身份字段置 0，大小和 mtime 保持真实值。记录仍输出 `identity=ok`。现有 14 条能力测试全绿，表明它们没有覆盖这种不对称能力缺失。

修复要把 opened-handle identity 与 path binding 的状态分别表达，组合证明任一未知则整体未知；若任一已知证据冲突，冲突也不能被另一项 unknown 遮掉。对外 schema 可另定，但不得把 “None = 未发现问题”混同于“身份绑定已证明”。旧 600,447 文件的搬迁前身份仍不能补造，新工具修好也不能追溯证明旧事实。

证据：`test_pa03_missing_path_identity_cannot_be_reported_proven`，1 失败。未执行真实批次 apply，冻结副本只在合成目录使用。

## 其他风险与文档问题（不混入上述计数）

- **既有 P2：Tk 拆除稳定性。** 基线亦曾失败说明不能简单归咎本轮，但不足以证明是环境缺陷。主线程 `gc.collect()` 和逐条独立进程是缓解/验证隔离，不是根因闭环。保留原失败证据，后续按同解释器、同顺序、同进程诊断。
- **P3：当前状态冲突。** README、IMPLEMENTATION 页首、TASK 的最新 overview/commit 仍有旧值；docs README 留有两个 PENDING。本审计更新当前索引并保留历史说明，不能靠重写旧维护记录“更正”当时结论。
- **P3：临时目录回退契约未同步。** AMD1 只写同级建立失败退出 3，实现支持系统临时目录回退。回退本身不自动构成缺陷，但磁盘成本、可恢复残留与错误含义必须在下一修订中写清楚。
- **组织与来源。** 归档工具属于仓库外可变脚本，先版本化再用；3 个 dirty scratch 保留。其存在不代表需要强制清理，更不能把批次名含 scratch 当作删除许可。
- **覆盖边界。** 本轮重点是 `5f19553..9ee6206` 的 10 个产品文件和相邻调用，兼以全套回归；没有对网络、真实恢复、云端封包、所有历史下载器路径做新的端到端验收，不保证“再无其他问题”。
