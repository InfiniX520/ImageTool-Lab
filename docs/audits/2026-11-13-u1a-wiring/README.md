# U1-A / J-01 生产接线评审

日期：2026-11-13。产品基线：`4023fca`（产品代码 `dda6fc1`，schema 5）。评审批次：`JPGPNG-U1A-WIRING-20261113-01`。

**结论：J-01 仍在当前产品中成立，U1-A 尚不能接生产。** 本轮用真实 `LibraryRepository` / `PageDeliveryExecutor` 和合成临时库复现：删除文件、等长非图字节替换、同尺寸不同摘要 PNG 替换后，同页新任务仍按旧 artifact 计为完成，`pages.completed=1 / existing=1 / pending=0`，没有派发修复；批次还能以 `coverage_complete=True` 结算为 completed。独立测试文件在基线上 **7 红**，另有 2 个明确 skip（等待实现）。本轮只做评审与红测试，不改产品、不接真实图库/生产库/账号/网络。

| 资料 | 用途 |
| --- | --- |
| [问题明细](FINDINGS.md) | J-01 当前行为、根因、两条复用路径、调用面、优先级 |
| [下一轮开发指导](DEVELOPMENT_GUIDE.md) | W0–W6 接口、状态机、事务/IO 边界、用户意图、验收矩阵 |
| [独立红测试](https://github.com/InfiniX520/ImageTool-Lab/blob/audit/u1a-j01-tests-20261113/tests/test_u1a_j01_health.py) | 基线 7 红 + 2 skip；分支 `audit/u1a-j01-tests-20261113`，提交 `4606dd4` |
| [基线 XML](../../../../.runtime/jpgpng_u1a_wiring_20261113_01/tests_baseline.xml) | 本文件单独运行：17 项、7 失败、2 跳过、0 错误 |
| [全量 XML](../../../../.runtime/jpgpng_u1a_wiring_20261113_01/tests_full_suite.xml) | 基线+红测试：1,515 项、7 失败（全部为本文件预期红）、5 跳过、0 错误 |
| [本批索引](../../../../.runtime/jpgpng_u1a_wiring_20261113_01/README.md) | 工作树、角色、证据边界 |

## 1. 范围

评审“历史交付收据 / artifact 记录是否可以代表当前磁盘健康”，以及所有会因数据库记录跳过下载的生产路径。重点是：

- 跨任务 trusted 复用；
- 同任务 completed delivery 复用；
- 关注/名单规划中的 `_candidate_requires_write`；
- 用户主动删除、转存、归档、清理后的意图；
- 文件 I/O 与 SQLite 写事务的边界；
- 修复/补齐时旧收据、旧 artifact、旧文件的保全。

不包含：真实图库扫描、真实账号/网络、上传/下载/删除/释放、封包、云端回读、UI 改版、生产迁移。

## 2. 为什么 J-01 仍是生产接线阻断

`_find_reusable_artifact` 只查 `artifacts` / `deliveries` 表，`_artifact_is_valid` 只验证记录字段；返回前没有打开记录指向的磁盘文件。另有一条同任务 completed existing 路径同样只凭数据库复用。调用点都在 `_accept_source_page` 的写事务内，不能简单在该函数里加文件哈希——那会把全文件读盘放进 `BEGIN IMMEDIATE`。

用户明确说明本地删除是上传夸克后主动腾空间，因此修复不能退化为“本地没有就自动重下”。必须把**历史交付事实**、**当前文件观察**和**用户意图**分开。

## 3. 本轮证据

- 删除文件：新任务 `pages={total:1, completed:1, new_completed:0, existing:1, pending:0, needs_verification:0}`，`dispatchable=()`，磁盘不存在。
- 等长非图字节（70 B `X`）：同样 `completed=1/existing=1/pending=0`，磁盘仍是替换字节。
- 同尺寸真实 PNG（70 B，摘要不同）：同样复用，磁盘仍是替换 PNG。
- 三种场景 `settle_batch` 均可在 `coverage_complete=True` 时返回 completed，`works.completed=1`。
- 独立测试 7 红；未改动文件仍复用、显式 `redownload` 仍可补齐、旧记录逐字段不变的 8 个守卫为绿。
- 只加 `os.path.exists()` 的浅修仍有 4 项红（等长非图与同尺寸换摘要），不能作为修复。

## 4. 未做与边界

- 未修改任何产品代码、TASK_PLAN、LOG 或历史审计。
- 未合并红测试到主树；红测试保留在独立分支，作为下一轮 W0 的失败证据。
- 未扫描真实图库，不能推断实际受影响文件数量。
- 未实现 artifact 健康表/观察投影/UI；DDL 与迁移须在 W1 定稿后单独评估。
