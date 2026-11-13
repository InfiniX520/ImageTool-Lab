# A1 绑定令牌、幂等事实与写读不变量（R1 / AR4-01、AR4-03、AR4-06）

日期：2026-11-13。工作包 R1，基线 `57a2532`（产品 `2ccabca` 加文档提交），分支
`fix/a1-ar4-core-20261113`。对应[当前审计](../audits/2026-11-13-a1-acceptance/FINDINGS.md)
的 AR4-01（CAS 只比较内容键）、AR4-03（幂等快速路径误拒绝与漏拒绝）、AR4-06（低层写接口
能制造维护审计随后拒绝的状态）与[开发指导](../audits/2026-11-13-a1-acceptance/DEVELOPMENT_GUIDE.md)
的 R1。这不是生产接线授权，不接触真实图库、账号或生产库。

## 1. 三个版本各自回答什么

| 版本 | 回答的问题 | 由谁产生 | 不能代替 |
| --- | --- | --- | --- |
| 内容 revision `(asset_id, revision)` | 这份**逻辑资产**的哪一代字节事实 | 解析器分配、写入时建行 | 哪个物理对象、哪个根映射刚被认可；也不能说明当前路径还指着它 |
| 绑定/接受令牌 `(content_key, acceptance_id, root_revision)` | 当前路径状态是**哪一次成功接受**的结果 | `record_path_acceptance` 追加，绑定外键引用 | 内容摘要真伪、云端回读；更不能当作"这次请求没被处理过"的证据 |
| 操作/观察 ID `(path_key, content_key, check_id)` + 请求事实 | 这是不是**同一个请求**的重放 | 观察者给 `check_id`，接受行 ID 由三者确定 | 无条件再次修改当前绑定的权限；也不能证明重放请求的事实与首次相同 |

三者互相不能替代：只比较内容键会把"同内容、不同物理对象"或"根重标"的变化当成没变化
（AR4-01）；只用接受 ID 会把"同一 check 但换了字节/身份"当成重放（AR4-03）；只靠观察 ID
无法说明当前指针是否还指向该接受（本文件的第 5 节）。

## 2. 期望令牌（expected token）的形态

`ContentResolution` 由解析器填充三件事，合起来是不可变的期望令牌：

| 字段 | 含义 | 允许取值 |
| --- | --- | --- |
| `expected_binding_absent` | 解析时该路径**确实没有绑定** | `True` / `False` |
| `previous_content` | 解析时读到的绑定内容（content_key 的来源） | 绑定的 `ContentVersion`；缺席时为 `None` |
| `expected_acceptance_id` | 解析时绑定**引用**的接受事件 | 64 位十六进制；索引绑定为 `None` |
| `expected_root_revision` | 该接受事件记录的根映射代次 | 非负整数；索引绑定为 `None` |

规则：

1. `expected_binding_absent=True` 时，另两个字段必须为 `None`。**没有绑定**由这个布尔量
   表达，不再靠 `None` 兼作"不检查"。
2. `expected_binding_absent=False` 时，`expected_acceptance_id` 与
   `expected_root_revision` 必须同时有值或同时为 `None`：同时为 `None` 恰好表示
   "旧式无接受的索引绑定"（计划保存建立、物理对象未知）。
3. 写入口从**解析输入**读取令牌；绝不用刚读到的当前值重新生成 expected。

旧的无 acceptance 索引绑定因此不需要新列：`expected_binding_absent=False`、
`expected_acceptance_id=None`、`expected_root_revision=None` 就是它的完整令牌。本包
R1 **不改 DDL、不升 schema**（当时为 4），避免与 R4 的索引迁移叠加；R4 之后 schema 为
**5**（只多两条热查询覆盖索引，没有新的绑定列），本文其余协议与 4 时点相同，v4 草案被显式拒绝。

## 3. 写入次序（全部在任何 INSERT/UPDATE 之前）

1. `_require_write_transaction`：autocommit 立即拒绝；managed isolation 下补一条
   `BEGIN IMMEDIATE`，仍由调用者提交/回滚。
2. 输入类型与字段校验；未确认/不完整的决议直接 `ArchiveIdentityConfirmationRequired`。
3. 由观察派生 rebind 证据，与已存 `archive_local_checks` 的**同一 check ID 全部事实**
   比对（path、content 链接、root、run、checked_at、state、observed_size/sha256、
   status/reason/digest_complete/bytes_read/physical size/mtime/device/index）。不同即
   `ArchiveSchemaError`，不允许被后面的幂等返回吞掉（AR4-03 的 `changed_replay_facts`）。
4. 真重放判定（第 5 节）。命中即刻返回历史接受，不写任何行。
5. 期望令牌与当前绑定逐项比较：`content_key`、`acceptance_id`、`root_revision`、
   `(size_bytes, sha256)`、revision 单调性、观察根不得早于当前接受根。任一不符即
   `ArchiveBindingConflictError`，调用者必须重新观察/重新解析。
6. 写内容/资产行、观察行、接受事件、绑定；条件更新一律使用第 5 步已比较过的令牌。
   失败由调用者事务整体回滚。

## 4. 共享的接受事实校验

写入口与维护审计必须对同一条接受行得出同一结论，但**不把全库审计放进热路径**。共享的是
纯谓词，取已经读到的行：

| 谓词 | 写入口 | 维护审计 |
| --- | --- | --- |
| `_acceptance_values` | `record_path_acceptance` 判定重放不可变事实 | 逐行重建同一元组 |
| `_expected_predecessor_reason` | 接受行的 `expected_content_key` 必须是**同资产、严格更旧**的代次；自己、未来代次、其他资产、未知键在写入前拒绝（AR4-06） | 同一判据 |
| `_acceptance_check_reason` | 接受行必须与它引用的 `archive_local_checks` 在 path/root/size/sha/物理身份上一致；无 check 时不得声称物理身份 | 同一判据 |
| `_acceptance_link_reason` | `bind_archive_path`、`load_content_binding` 校验接受行的 path/content/asset/revision 与绑定值一致；串错 acceptance 抛 `ArchiveSchemaError`，不返回另一文件的物理身份 | 既有绑定↔接受比较 |

`bind_archive_path` 的附加契约：

- 传入 `acceptance_id` 时必须先通过上面的关联校验，再比较期望令牌；
- 更新**已有接受绑定**必须显式给 `expected_acceptance_id`（模块 sentinel `UNSET`
  表示"调用者没给"，与 `None` 表示"当前确实没有接受"区分）；令牌不符则拒绝；
- 已有接受绑定不能被一次普通 bind 改成 `acceptance_id=NULL`（那会悄悄丢失被认可的物理
  证据）；要推进内容必须走 `apply_content_resolution` 并携带新接受；
- 同内容只移动接受指针是允许的（新 check 的合法证据前进），但仍需令牌匹配。

## 5. 重放与失败语义

- **真重放**：仅当
  (a) 本次 `(path_key, content_key, check_id)` 确定的接受行已存在，
  (b) 该行除 `accepted_at` 外的不可变事实与本次请求一致，
  (c) 当前绑定的内容键等于本次新内容键，且当前 `acceptance_id` 正是该行，
  (d) 当前接受根等于本次观察的根
  时，返回**首次存储**的 `accepted_at` 的接受对象，不新增任何行。改调用时间不算改事实。
- **历史成功请求迟到**（接受行存在但当前指针已前进）：不接受为成功写入，也不回退指针；
  返回结构化 `ArchiveBindingConflictError`，无副作用。不得把当前指针倒回旧事件。
- **同内容、不同 check**：不是重放；令牌匹配时追加新接受并把指针前移到新证据。
- **同 check、不同事实**：第 3 步就拒绝，写前失败。
- **接受事件变化 / 根变化 / 同内容不同物理对象 / 旧决议晚于新接受**：令牌不符，
  `ArchiveBindingConflictError`；当前绑定、接受链、旧计划全部不变。

## 6. 异常类型（不新增近似异常树）

| 情形 | 异常 |
| --- | --- |
| 无调用者事务 | `ArchiveTransactionRequiredError` |
| 令牌不符、路径被并发绑定 | `ArchiveBindingConflictError`（带 expected/actual/reason） |
| 需要人工确认替换 | `ArchiveIdentityConfirmationRequired`（带 resolution） |
| 存储事实自相矛盾、同 ID 不同事实、接受关联不符 | `ArchiveSchemaError` |
| 参数本身非法 | `ContractError` |
| 仓库锁竞争 | `ArchiveLibraryBusyError`（原因链保留） |

## 7. 影响分析与保留项

改动符号（GitNexus `a1-ar4-core` 索引，`--include-tests`）：

| 符号 | 风险 | 直接引用 |
| --- | --- | --- |
| `bind_archive_path` | CRITICAL | 4（计划保存、apply、2 处测试），5 条执行流 |
| `validate_archive_facts` | CRITICAL | 47 |
| `load_content_binding` | HIGH | 24（解析器 + 测试） |
| `ContentResolution` / `ContentBinding` | HIGH | 17（模块 + 测试） |
| `apply_content_resolution` | MEDIUM | 11 |
| `record_path_acceptance` | LOW | 2 |
| `resolve_content_version` | LOW | 2 |

必须保留的行为：`expected_binding_absent` 语义、调用者事务归属、`ArchiveLibraryBusyError`
原因链、旧计划逐字可读、schema 4 迁移与拒绝 v2/v3、维护审计能力完整。

不在本包：AR4-02（计划保存旁路，R3）、AR4-04/05（多路径与别名，R2）、AR4-07（索引，R4）。
计划保存仍不携带令牌；本包只保证它**不能**把已有接受绑定改成索引绑定，也不改变它推进
无接受索引绑定的既有行为（由 R3 统一）。
