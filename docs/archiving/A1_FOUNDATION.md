# A1 离线基础与接续契约

日期：2026-11-11。批次：JPGPNG-A1-FOUNDATION-20261111-01。

## 范围

本批由两个独立子代理并行起草，主代理冻结并复核：local_observation.py、archive_inventory.py、archive_schema.py 及各自测试。三个模块均未导入现有下载启动链；没有改生产数据库、读取真实图库或凭据、封包、上传、回读、删除或安装。

**这是可离线验证的 A1 基础，不是生产归档发布，也不表示 J-01 已修复。**

## 模块职责

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| core/local_observation.py | 显式根下普通文件的身份、大小/mtime 前后一致性、流式 SHA-256、逐块取消与时间/字节预算 | 原子防对抗替换、解除文件锁、跨平台实测、上传或删除 |
| core/archive_inventory.py | 显式作品范围、不可核验问题、确定性整文件分包、参数化额外空间估算 | 自行扫描、封包、压缩/纠错工具保证、实际磁盘分配或远端限制 |
| core/archive_schema.py | 临时库 v1→v2 迁移、内容/路径/观察/包/对象/位置/检查/释放事实表 | 自动发现或打开生产库、证明云端持有、执行释放或改变现有生产版本检查 |
| core/preservation_policy.py（既有） | 消费本地与归档证据给出下一步 | 文件 I/O、证明云端、补下载或删除 |

## 本地观察 API

LocalObserver(root, root_id=..., kind=..., root_revision=...).observe(ObservationRequest(content, path, run_id, check_id, budget=..., cancellation=...))。

ObservationRequest 是不可变对象；调用方提供 ContentVersion 与 PathRef，不允许任意绝对目标。根/祖先/目标重解析点、UNC/设备根、非常规文件与身份缺失均拒绝完整核验。ObservationBudget 是逐文件限制，不是无限后台任务授权。

完整摘要必须与打开句柄及路径前后身份、大小和 mtime 一致。相同大小不同摘要是 CHANGED；读取中变化、消失或身份不可用不得成为 HASH_MATCH。取消/耗尽不返回完整摘要。没有历史摘要时，BASELINE_OBSERVED 只代表当前文件完整读取；ContentVersion 仍保持 sha256=None，不自动提升为历史 HASH_MATCH。

Windows 本机使用 st_dev/st_ino 身份；这不是对抗性写者的原子锁，也不能中断一次正在阻塞的系统 read。逐块检查必须靠工作线程/I/O 所有者调用；界面不得同步调用。

## 清单和容量 API

调用方传 ArtworkGroup(work_id, entries)，每项为 ArchiveEntry(content, path, page_index=None)。build_archive_inventory 接收 batch_id 与 InventoryPolicy；plan_archive_capacity 接收清单、显式可用额外空间和 CapacityPolicy。

无内容身份/大小/摘要、非法路径/ADS、路径碰撞、内容代次或作品页冲突、空作品均列出 issue，不将部分合法项冒充完整范围。相同观察去重；同内容的不同路径仍分别保留。分包不拆文件，作品优先同包；超目标单件和作品拆包均明确标记。

容量按源文件之外的新增空间计算，区分全批/逐包冻结及阶段并存。2 GiB、20 GiB、15%、64 MiB 控制资料都只是参数化起点；没有实测工具开销、PAR2 修复能力或夸克容量。不足时只报告 shortfall，不自动清理或释放任何资料。

## 临时库 schema 与迁移

schema 2 将 archive_contents（内容代次）与 archive_assets（路径/来源）分开，避免多个路径误用同一复合外键；archive_objects 独立表示真实包/恢复/控制对象，位置绑定具体对象代次。本地与归档 evidence_json 是带完整类型信息的不可变快照，校验器检查索引列、JSON、内容/路径、包成员与位置关联。

只可通过 migrate_archive_copy(path, backup, temporary_copy_confirmed=True) 显式迁移已关闭的临时副本。使用现有 .writer.lock、逐项旧 DDL 预检、只读完整性检查、独立不覆盖备份、BEGIN IMMEDIATE 和回滚。未知/未来版本或不兼容对象拒绝。

生产 LibraryRepository 未改，仍拒绝 v2；迁移测试副本不得作为正常生产库继续使用。未来生产接入必须单独设计版本升级与回退、在唯一写入者内完成并运行相关测试。

释放表只是事实结构，不赋予删除能力。除现有 hash/位置门槛外，A5 仍需批次计划/命令、当前根映射、具体文件身份与显式范围；本批不实现释放。

## 性能与运行边界

哈希有界分块；schema 校验使用 SQL 连接/游标及聚合，不把全部观察载入内存。清单首版为纯内存显式范围，不是图库扫描/SQLite 分页器；超大规模分页与 UI 留待后续接入。无缩略图、无新依赖、无新入口。

测试和报告在工作区 .runtime/jpgpng_a1_foundation_20261111_01/；checkpoint/ 有六份本批源码/测试完整副本与修改前共享文档摘要，CONTRACT.md 冻结并行边界。原始测试失败及最终报告保留，不用旧报告冒充当前版本。

## 下一批

1. 选择一个小而有界的数据库查询/适配入口，将计划 JSON 与现有唯一写入者显式适配；不改第二套完成账本。
2. 在合成范围接入文件清单枚举，保留排除规则、分页、取消、根变化及大目录预算。
3. 独立审计生产 v1→v2 接入和库兼容；只有明确发布批次才允许迁移生产库。
4. 准备 A2 官方工具来源、版本、体积、安装/便携位置及安全密码通道实验；不先索要真实密码。
5. 保留 U1-B/U1-C/U2/U3/U4 与 E1 原有独立门槛；不把本批基础通过当作其他功能已完成。
