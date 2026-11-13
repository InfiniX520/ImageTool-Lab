# A1 AR4 修复与 schema 5 维护记录

日期：2026-11-13（Asia/Shanghai）。批次：`JPGPNG-A1-AR4-20261113-01`。
基线：`57a2532`（产品代码与 `2ccabca` 相同）；分支：`fix/a1-ar3-20261112`。
范围：只处理 [AR4 复审](../audits/2026-11-13-a1-acceptance/README.md) 的 AR4-01..08；离线合成文件与临时库，不接生产、不碰真实图库/账号/上传/删除/释放，不联网。

## 1. 结论

AR4-01..08 已全部关闭。A1 的离线接受事务、计划应用、多路径身份、别名、写读不变量、查询索引和独立测试集达到可进入生产接线评审的状态；**不表示生产接线、真实图库迁移、云端回读或释放已完成**。schema 为 **5**，计划协议仍为 **3**，范围回执仍为 **2**。

## 2. 提交

| 工作包 | 提交 | 内容 |
| --- | --- | --- |
| R1 | `55ee067`、`e7d8123` | 完整绑定令牌、幂等事实、写读不变量；no-op 重绑 |
| R2 | `f84f734`、`354853c`、`3024b7c`、`a06b733` | 资产级多路径历史、真实目录项拼写、计划别名 |
| R3 | `860dc0e` | 唯一原子 `apply_observed_plan`；计划保存不再推进绑定 |
| R4 | `c446766`、`766504c` | schema 5、两条覆盖索引、v4 草案显式拒绝 |
| 独立测试 | `2d73ce1` | 合入 B 的 AR4 23 项 + 原子命令 6 项 |

最终产品代码包含上述全部源码提交；本记录单独提交，不改变产品字节。

## 3. 逐项结果

| 编号 | 结果 | 关键证据 |
| --- | --- | --- |
| AR4-01 | 关闭 | `ContentResolution` 冻结 `expected_acceptance_id/root_revision`；旧 BBBB 决议、延迟 REUSE 在 B/AAAA 接受后均被 `ArchiveBindingConflictError` 拒绝，全表不变 |
| AR4-02 | 关闭 | `apply_observed_plan` 在单个 `BEGIN IMMEDIATE` 内接受整批并保存计划；失败全部回滚；重放不重新接受；计划保存不再推进已有绑定 |
| AR4-03 | 关闭 | REUSE/BASELINE/NEW_REVISION/REPLACEMENT 同事实重放返回首次接受、不新增行；同 check ID 的 run/time/hash/size/identity/root 变化写前拒绝 |
| AR4-04 | 关闭 | 资产级 `MAX(revision)` 作为代次下界；硬链接滞后到 rev3、改名续 rev2、旧代次完整；apply 内再检并结构化冲突 |
| AR4-05 | 关闭 | 显式选择归一为真实目录项拼写，跨请求 `A/a`、`画A/画a` 同一 identity；`straße/strasse`、NFC/NFD 不折叠；旧 path_json/source_kind 不改写 |
| AR4-06 | 关闭 | 绑定必须精确匹配 acceptance 的 path/content/asset/revision/root；跨路径、同路径跨代次、expected self/future/foreign 写前拒绝；no-op 保留 |
| AR4-07 | 关闭 | schema 5 新增 `idx_archive_contents_digest(sha256,size_bytes,asset_id,revision)`、`idx_archive_assets_path(path_key,asset_id)`；EXPLAIN 无 SCAN/TEMP TREE |
| AR4-08 | 关闭 | B 的独立 AR4 23 项与原子命令 6 项进入主树；本记录、TASK_PLAN/LOG/入口同步；旧审计正文与证据保留 |

## 4. 测试与验收

### 4.1 主树全量

- 基线（审计）：1,415 项，1,412 通过，3 跳过。
- R1 后（`e7d8123`，单个 Tk 测试 deselect）：1,435 项，1,432 通过，3 跳过，0 失败/错误。
- R2 后（`a06b733`，单个 Tk 测试 deselect）：1,444 项，1,441 通过，3 跳过，0 失败/错误。
- R4 后（`dda6fc1`，`--ignore=tests/test_collection_presentation.py`）：**1,494 项，1,491 通过，3 跳过，0 失败/错误**（`full_r4_main.xml`，218.137 s）。
- 单独的 presentation 文件：4/4 通过（`presentation_tk.xml`）；该文件曾在其他运行时遇到 Tk/Tcl 初始化失败（环境问题，不是产品断言失败），计入时需单列。
- 组合口径（Tk 可用时）：1,498 项，1,495 通过，3 跳过，0 失败/错误。

### 4.2 独立测试

- B 的 `tests/test_ar4_acceptance.py`：23/23（a06b733）。
- B 的 `tests/test_ar4_atomic_plan_command.py`：6/6（860dc0e）；基线 57a2532 上 6/6 红，因命令不存在。
- A 的新增测试：`test_ar4_binding_tokens.py` 21、`test_ar4_asset_history_alias.py` 9、`test_ar4_observed_plan_apply.py` 10、`test_ar4_schema_five_indexes.py` 13。
- 14 个归档专项文件合计 363 通过（R4 工作树）。

### 4.3 2,000 文件与进程退出

- `scale_atomic02/result.json`：2,000 文件、20 页、1,915 个不同摘要；第一批枚举 0.70 s、观察 12.15 s、prepare 0.69 s、单事务命令 10.69 s；tracemalloc 峰值 22,031,985 B。计数：contents/assets 4,015、bindings 2,000、checks/acceptances 2,020、batches 2；完整性与外键通过，全部路径/摘要/物理身份对得上，旧计划逐字不变。
- 第二批 20 文件（10 原地改、5 替换、5 不变）通过，命令 0.30 s；确认替换后接受。
- `crash_atomic01/results.json`：提交前进程退出 97，六张表全 0；提交后退出 98，20 条绑定/检查/接受与 1 个 batch 保留；两次重开均能再接受 20 条新命令。integrity=ok、foreign_keys=[]。
- 这是合成小文件与手工 API 编排，不是 500 GB 吞吐、真实盘故障或生产服务验收。

### 4.4 SQL（C，schema 5 复测）

- 稳态单次：摘要命中 19、未命中 13、所有者 17、`ensure_archive_asset` 幂等 58，均与历史规模无关（R²=1.0）。
- EXPLAIN：摘要 = `SEARCH archive_contents USING COVERING INDEX idx_archive_contents_digest`；所有者 = `SEARCH archive_assets USING COVERING INDEX idx_archive_assets_path`；无 SCAN/TEMP TREE。
- 20 条批保存实测 10,526 VM（反事实 10,364；+162 来自 R1/R3 每条目一次接受令牌/资产存在性检查，不是扫描），100/200/400/1000 基本常数；n=1000 库 3,125,248 B（基线 2,912,256 B，+7.3%；VACUUM 后与对照组同为 3,035,136 B，差为建索引/插行顺序的页布局）。收益是斜率消失，不宣称当前墙钟大幅下降。

## 5. 版本与迁移

- `ARCHIVE_SCHEMA_VERSION = "5"`；v5 = v4 的 16 表 + 14 索引 + 1 触发器，仅新增两条显式索引。
- `migrate_archive_copy` 仍只接受 v1 基线；v1→v5 自动建两条索引。v2/v3/v4 草案均明确拒绝；v4 文案点名“已被 5 取代、无生产库、从 schema 1 重建”。
- 工作区 1,607 个真实 v4 库全部是 `.runtime` 合成/测试临时库，项目树内 0 个；不迁移、不就地改标。旧计划不可跨版本搬运，临时库从 v1 重建。

## 6. 残余风险与边界

1. 显式选择每个不同父目录多一次 listing（每父一次、线性，不按文件）；前缀枚举不变。
2. `os.path.normcase` 在 POSIX 不折叠；混合字符依赖真实目录项；目录不可读或预算耗尽时保留键入拼写并走原有失败/排除路径。
3. 物理 ID 跨时间复用仍是弱身份：改名后 inode 被复用会续到旧资产；未引入 physical-object 表，留待后续身份决策。
4. 对象门按 SQL 文本逐字比较：手工加索引/改结构会被拒绝（有意的 fail-closed）。
5. v4 草案显式拒绝而非迁移；本机 1,607 个真实 v4 合成/测试临时库（其中 1,534 个 product-made）需重建，项目树内无生产库。
6. Tk/Tcl 环境初始化间歇失败；测试记录单列，不作为产品失败。
7. 计划封存、云端回读、真实工具、上传/删除/释放、生产接线均未执行。

## 7. 下一步

A1 离线接口可进入生产接线评审（U1-A/J-01）：先审查真实调用点、用户主动转存/删除意图、真实图库只读同步和回退，再按联合计划做 A2–A6/E1。不得把本记录的离线通过数当作生产图库健康或发布声明。

证据根：`E:\Zero9\JuiLiu\.runtime\jpgpng_a1_ar4_20261113_01\`（README、full_r4_main.xml、presentation_tk.xml、tests_r*.xml、scale_atomic02/result.json、crash_atomic01/results.json、measure/REPORT.md、measure/results/r4_after/）。
