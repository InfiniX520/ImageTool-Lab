# JpgPng A1 修复批次维护记录

> 本文记录前序修复过程。最新独立复审见[复审总览](../audits/2026-11-12-a1-recheck/README.md)：现有测试通过，但 8 项问题/接线缺口仍开放；下面“已关闭”仅表示该批次当时判断，独立结论以复审为准。本轮纠正定位/计数/当前下一步，原修改前文档已保全，旧 XML/manifest 不改。

批次：JPGPNG-A1-REPAIR-20261112-01
日期：2026-11-12
状态：离线事实/范围/计划/写入适配修复已联测；**不是 A1 生产发布，不是真实图库迁移，不是删除授权**。

## 授权与边界

- 只在 `E:\Zero9\JuiLiu\JpgPng` 及 `.runtime/` 合成副本内工作。
- 未读取真实图库或生产库，未使用账号、Cookie、Token、密码，未上传、回读、封包、删除或执行释放。
- 未安装依赖；未使用 `pip install --user`；测试使用共享解释器 `D:\Miniconda3\envs\JuiLiu\python.exe`。
- 未提交 git；未重置工作树；历史失败 XML 和旧报告保留。

## 本批实际修改

| 文件 | 修改 |
| --- | --- |
| `core/archive_schema.py` | schema 3 事实模型：内容/资产/路径绑定分离；同路径同资产可换代、不同资产不可抢占；物理观察字段；释放事件 prepared→终态；转移意图、release operation 交叉校验；1 MiB 证据/JSON 上限；触发器防止路径换主；全库事实校验。 |
| `core/archive_plan_store.py` | 8 MiB 有界分段清单（`archive_plan_segments`）；准备与事务内保存拆分；旧计划可重载；同资产新代次条件更新绑定；当前批次校验；显式全库审计入口保留；异常/尺寸边界。 |
| `core/archive_repository.py` | schema 3 门；继承唯一写入者事务；移除事务外 precheck；读连接显式关闭；ContractError 统一包装；命令式重载有事务边界。 |
| `core/archive_scope.py` | 三个入口统一资格策略与配置输出根；物理身份+raw 路径；NFC/NFD 与硬链接别名保留；大小写同目录项排除；O(n) 身份索引/deque；ScopeReceipt 完整消费凭据；祖先身份与目录成员快照分离。 |
| `core/library_repository.py` | 仅 `_enqueue` 增加可选的声明 `payload_bytes` 计入命令/排队预算；该符号影响为 HIGH（9 直接调用者/3 流程），已记录并回归。 |
| `tests/test_archive_schema.py` | 22 项；多代次、NULL、释放、转移意图、收据、Row、物理观察等反例。 |
| `tests/test_archive_plan_store.py` | 24 项；2k 分段、10k 预算、跨批复用、旧计划重载、收据持久化/篡改/默认强制、全库审计入口等。 |
| `tests/test_archive_repository.py` | 6 项；唯一写入者、并发首存、损坏/冲突、关闭、继承 job。 |
| `tests/test_archive_scope.py` + `test_archive_scope_boundaries.py` | 26 + 68 项；统一资格、Unicode、硬链接、收据、祖先/根变化、预算/取消。 |

## 实际验证

1. 归档相关专项：`.runtime/jpgpng_a1_repair_20261112_01/archive_focused18.xml`
   - 212 tests，212 passed，0 failed，0 errors，0 skipped，21.1 s。
2. 独立范围复跑：`.runtime/jpgpng_a1_repair_20261112_01/scope_independent04.xml`
   - 94 tests，94 passed，0 failed，0 errors，0 skipped。
3. 全量干净复跑：`.runtime/jpgpng_a1_repair_20261112_01/full03.xml`
   - 1301 tests，1298 passed，0 failed，0 errors，3 skipped，170.8 s。
4. 全量最新复跑：`.runtime/jpgpng_a1_repair_20261112_01/full21.xml`
   - 1313 tests，1310 passed，0 failures，0 errors，3 skipped。
   - 部分更早报告曾有 Tk/Tcl 初始化 error；full21 为 0 error，历史失败保留，不宣称 GUI 永久稳定。
5. 规模：
   - 2,000 条：`payload_bytes=1,790,044`，9 段；`tracemalloc` 峰值 `11,865,024` B（约 6.63×），保存/重载通过。
   - 10,000 长名：超过 8 MiB，在写入前以 `ArchivePlanTooLargeError` 拒绝，零写入；本轮未重测精确长名字节。
   - 10,000 短名：加入物理身份序列化后为 `8,938,386` 字节，也被 8 MiB 预算提前拒绝；独立输入条目上限为 9,000，9,001 条在收据/序列化前拒绝。

## 已关闭的审计点

- A1-01：单一继承写入者事务；无嵌套 BEGIN/COMMIT；无事务外重放 precheck；读连接显式关闭。
- A1-02/03/11：统一资格、物理身份/原始路径、硬链接别名、大小写同目录项、祖先与成员快照；归档 identity_key 对非 ASCII 保留精确拼写，NFC/NFD 同内容两路径测试通过；硬链接源容量按物理身份只计一次。
- A1-05/06/07：分段有界计划、2k 保存/重载、跨批复用、同资产换代与旧计划重载。
- A1-08/09：NULL 不升格、资产/内容/成员/位置绑定、转移意图与释放 operation 交叉校验、物理观察持久化。
- A1-10：O(n) 身份索引；常规计划只校验当前批次；全库审计保留显式入口。
- A1-12：测试前提错误（排队任务 stop、集合不可哈希、范围大小写）修正并记录，不改正常暂停/业务逻辑。

## 仍未关闭 / 不可误读

- **A1-04 计划入口已关闭**：`ArchiveLibraryRepository.save_archive_plan`、`prepare_archive_plan`、`save_archive_plan` 都要求完整 `ScopeReceipt`；无公开 `require_receipt` 旁路。无 receipt 在准备/入队前拒绝，带 receipt 的保存、重放、并发、关闭重开和重载通过。
- 生产 `PathRef.target_key` 仍 casefold（下载主线未改）；归档使用 identity_key，ASCII 大小写折叠、非 ASCII 精确，NFC/NFD 不再在 inventory 碰撞。生产路径语义是否统一另行评估。
- 当前同时有 9,000 输入条目与 8 MiB 序列化限制；10k 已超过条目上限。此前短名/长名序列化测量属于较早实现阶段，不作为当前可通过声明。
- 计划声明 `payload_bytes` 是规范化文档字节，不是 Python 堆内存。本文采用所列 2k 测量 11,865,024 / 1,790,044 ≈ 6.63×；不同中间态的比例不作为当前保证，未做 RSS 断言。
- 未验证多进程 writer 锁竞争、崩溃/WAL 半写恢复、真实 symlink/大目录、真实封包/回读/密码/上传/删除/释放。
- Tk/Tcl 在历史 full06/full10 出现环境 setup error；full21 和本次独立全量均 0 error，旧失败保留。
- `core/library_repository.py` 的 `_enqueue` 为 HIGH 影响面；本批只增加可选声明字节计费，仍需生产接入前独立复核。

## 检查点

- 源码/测试副本与 SHA-256：`.runtime/jpgpng_a1_repair_20261112_01/checkpoint_review_fixes_20261112/manifest.json`
- 验收摘要：`.runtime/jpgpng_a1_repair_20261112_01/validation.json`
- 修复前快照：`.runtime/jpgpng_a1_repair_20261112_01/before/`
- 历史失败：`probe_r1.xml`、`schema01.xml`、`plan01.xml`、`full01.xml`、`full02.xml` 等保留，不覆盖。

## 下一步（独立复审后更新）

按[复审指导 F0–F5](../audits/2026-11-12-a1-recheck/DEVELOPMENT_GUIDE.md)执行：先计划读取与引用闭包，再候选/观察/代次交接与实际 I/O，之后重新评审生产。旧“决定是否删除 require_receipt=False”已过时；当前无该公开参数，但持久化读取仍允许 null 凭据，问题性质不同。

实际原始 XML、validation 与源码检查点在项目内 `.runtime/jpgpng_a1_repair_20261112_01/`；工作区上级同名目录仅保存另一部分合成临时目录。以下检查点段的路径按项目根解析，不按工作区根解析。

## 结论

本批把 A1 的离线事实、范围、计划与单写入者适配推进到可重复验证状态，但 **A1 不是生产可用**；A1-04 的所有计划入口已强制 receipt，真实边界和其余阶段仍需继续按门槛推进。
