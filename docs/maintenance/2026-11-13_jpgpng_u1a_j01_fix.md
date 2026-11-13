# JpgPng U1-A / J-01 W0–W4 修复记录

日期：2026-11-13。分支：`fix/a1-u1a-j01-20261113`，从 `bb2cb7f`（产品代码 `dda6fc1`，schema 5）接续。评审批次：`JPGPNG-U1A-WIRING-20261113-01`。本批仍为离线合成验证，不接真实图库、生产库、账号、网络，不执行上传、删除或释放。

## 1. 结论

- J-01 的三条生产复用旁路已改为“当前文件 SHA-256 健康观察才允许复用”：跨任务 trusted、同 job completed、follow/名单 `_candidate_requires_write`。
- 删除、等长非图替换、同尺寸不同摘要 PNG 不再计为 `completed/existing`；缺失进入 pending 修复，变化进入 `needs_verification`；批次不能在这些页面未解决时结算为 completed。
- 未改动文件仍可复用；显式 `redownload` 仍可补齐并保留两代 artifact；旧 artifact、delivery、批次计数逐字段不变。
- W4 用追加式 `events(kind='removal_intent')` 接入 `EXCLUDED`/撤销；`OFFLOADED` 仍明确拒绝，未对外声称支持。
- **W5（UI/CLI/全库巡检）与 W6（2,000 文件、第二批、崩溃恢复）尚未完成；本批不是生产发布声明。**

## 2. 实现

| 工作包 | 落点 | 内容 |
| --- | --- | --- |
| W0 | `docs/audits/2026-11-13-u1a-wiring/W0_DECISIONS.md` | 状态/事务决定、复用判定表、根绑定、I/O 边界 |
| W1 | `core/artifact_health.py` | `ArtifactExpectation` / `ArtifactHealth` / `ArtifactHealthPolicy`；完整 SHA-256 才可复用；缺失/变化/不可读/根不可用/读取中变化/未知均不可复用；payload 严格往返；过期/根 revision 不匹配即失效 |
| W2 | `core/library_paths.py`、`core/library_repository.py` | `PathResolver.root_path/root_revision`；`bind_path_resolver`；预检只读快照先关闭，再做 `LocalObserver` 文件 I/O；check 记录冻结进 command payload，写者短事务复核 |
| W3 | `core/delivery_repository.py`、`core/library_repository.py`、`core/follow_repository.py`、`core/page_delivery.py` | 候选查询与健康门分离；三条复用路径接入；无根绑定/无健康记录 fail-closed；executor 构造时注册根 |
| W4 | `core/library_repository.py` | `record_removal_intent` 追加事件；`EXCLUDED` 抑制缺失自动补齐；撤销 `NONE` 后重新待修；旧记录保留；`OFFLOADED` 拒绝 |

关键语义：

- `hash_match`（完整 SHA-256、稳定身份/大小/mtime）→ `existing=1`，不派发。
- `missing` → 新 pending 交付代次，可修复；不覆盖旧 artifact/delivery。
- `changed` / `unreadable` / `root_unavailable` / `changed_during_check` / `unknown` → `needs_verification`，不自动联网、不覆盖磁盘。
- `EXCLUDED` 且不可复用 → `cancelled`（`intent_excluded`），pending/needs_verification 均为 0；撤销后才重新待修。
- 没有根绑定就**不**回退到旧的“数据库记录即复用”行为；这是 fail-closed。生产在 `PageDeliveryExecutor` 构造时绑定 `PathResolver`。

## 3. 测试证据

命令环境：`D:\Miniconda3\envs\JuiLiu\python.exe`，`PYTHONNOUSERSITE=1`，`-s -B -X utf8`，`-p no:cacheprovider`，独立 basetemp/XML。

| 证据 | 结果 |
| --- | --- |
| [W0 基线](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w0_baseline.xml) | 17 项、7 失败、2 跳过、0 错误（预期红） |
| [W1 健康契约](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w1_contract.xml) | 20 项全过 |
| [W2/W3 复用门](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w2_gate_tests.xml) | 6 项全过 |
| [J-01 独立文件](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w4_j01.xml) | 16 过、1 跳过（W 阶段边界） |
| [W4 全量（不含 Tk）](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w4_full_no_tk.xml) | **1,539 项、0 失败、0 错误、4 跳过**，215.65 s |
| Tk 展示文件单独运行 | 4 项全过（当前环境；全量中 Tk/Tcl 初始化偶发环境错误，单独运行排除） |

新增/激活的 U1-A 测试：

- `tests/test_u1a_j01_health.py`：17 项，16 过、1 跳过；7 个原红全部转绿，8 个守卫保持绿。
- `tests/test_u1a_artifact_health.py`：20 项，状态机、payload、过期、根 revision、快速策略不升级。
- `tests/test_u1a_reuse_gate.py`：6 项，无根绑定 fail-closed、根绑定冲突、过期预检、I/O 不在 writer 线程、预算、同 job resume 修复。
- `tests/test_u1a_removal_intent.py`：2 项，EXCLUDED 抑制/撤销、OFFLOADED 不虚假宣传。

测试调整说明：`tests/test_delivery_repository.py::test_all_pages_flag_is_not_page_byte_quality` 原先只写数据库记录、不写文件；J-01 后这不再是可复用状态。该用例改为写入真实 1,000 字节并绑定同一根，仍只验证 `all_pages` 不是页面字节属性，未放宽断言。

## 4. 边界与残余

- 未接触真实图库/生产库/账号/网络；未上传、删除、释放或回读云端。
- 未做 UI/CLI 展示“核验于某时/状态/原因/重新核验/补齐/标记有意移除”；W5 待做。
- 未做 2,000 文件整链、第二批变化、进程退出/崩溃恢复；W6 待做。
- 快速策略只保留契约，未默认启用；完整 SHA-256 是当前唯一复用策略。
- `root_unavailable` 目前保守转 `needs_verification`，尚无单独观察投影/UI 原因；W5 待补。
- `OFFLOADED` 意图已拒绝写入，避免“记录了但没接线”的虚假能力；待 W5 设计转存核验后再开放。
- 用户意图事件当前挂在 running batch 上；批次/任务删除会级联事件，生产长期意图存储仍需在 W5/W6 评审。
- 未做真实迁移、备份演练或旧库升级；本批不新增数据库 schema 对象。

## 5. 复核入口

- [W0 决定](../audits/2026-11-13-u1a-wiring/W0_DECISIONS.md)
- [评审总览](../audits/2026-11-13-u1a-wiring/README.md)
- [开发指导](../audits/2026-11-13-u1a-wiring/DEVELOPMENT_GUIDE.md)
- [全量 XML](file:///E:/Zero9/JuiLiu/.runtime/jpgpng_u1a_fix_20261113_01/w4_full_no_tk.xml)
