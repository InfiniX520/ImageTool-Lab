# 下一轮开发指导：先冻结六个缺口，再接 W5

基线：包含本审计的 `audit/u1a-rfa-recheck-20261114`；产品固定 `48a6968`，审计输入 `f7c389b`。先阅读 [结论](README.md)、[RFB 问题](FINDINGS.md)、[v3 契约](../../optimization/2026-11-14/U1A_STATE_CONTRACT_V3.md)。本轮只审计和记录，没有开始产品修复。

## S0 · 主执行者串行对齐与保全

1. 检查日常 checkout、修复 worktree、审计分支与各自 git status；不要拿日常 `5465e0c` 覆盖新候选，不重置用户修改。
2. 从审计分支新建唯一集成 worktree。先原样接入本轮 12 项反例（仅允许修改根定位），复现 10 红/2 绿；旧 RFA 12/RF 15/UA 14 与所有测试保留。按新编号 RFB 接续。
3. 冻结下面四份返回协议，并把 v3 的矛盾与错误解释写成新版本契约，不改写历史 v3 文件来抹去原承诺。
4. 对齐输出必须结束一轮；用户确认后才按 AGENTS 使用继承完整上下文的 fork。禁止刚读代码就在同一轮并发，也不要为了凑“三并发”共享文件/事务。子代理提示词按工作区原文质量声明执行。

需要冻结的四份协议：

| 协议 | 必须明确的字段/结果 |
| --- | --- |
| 观察与进度 | evidence_kind、当前 attempt/命令身份、选中 artifact、row/root/policy、freshness；有效净进度、支持容量、不可接续原因、提交条件 |
| 清理扫描 | 每 tier 的 next_cursor、EOF、budget_exhausted、examined、offered；空结果不得兼任 EOF |
| 后继解析 | 权威边、节点集合、artifact/质量校验、none/unusable/budget/cycle；读取与 writer 同一协议 |
| 恢复错误 | CAS 输家、可读但补偿写失败、账本不可读、文件不可观察；每类对调度/持久化的具体影响 |

## S1 · 将严格质量与观察推进一起修（RFB-01/02/06）

唯一负责人持有 `library_repository.py` 的观察/受理区、`observation_snapshot.py`；若改 `local_observation.py` 或 source/coordinator 接线，同一工作包串行完成。不要三个人分别改缓存、预算、受理，它们共用同一个提交前提。

### 先解决设计冲突

“原文件可任意修改”“跨 resume 不重哈希”“仍是严格当前内容证明”“固定记录容量”“任意大页原子提交”无法同时无条件保证。优先级沿用用户要求：原图质量 > 性能 > 其他。禁止用 stat-only、增加 TTL 或改成功统计来换速度。

推荐分清三层：历史观察是候选提示；一次当前观察尝试产出严格证据；已接受 receipt 是历史提交事实。只有第三层允许零哈希幂等重放。缓存过期、身份/版本变化或无法证明内容未变时，第一层不能直接升级为第二层。

最小安全修法可以对明确支持范围重新验证正面缓存，但必须同时处理切片正好用完的最后一项和容量不支持的明确结果，不能把 RFB-01 修成 RFA-01 的无限重试。对大页的长期方案，优先考虑由 observer 持有一个可查询命令，在内部有界切片间让出控制、最终完整提交；暂停/重启后重新建立新尝试的信任边界。若需要持久化 continuation/DDL，先给迁移方案和门禁，不偷偷借 events 无限增长来代替正式状态。

请先提交设计判定表，不直接照抄上述建议为代码。S0 必须写清：支持的 page 数/候选版本数/快照字节；超限是明确拒绝还是可续命令；什么时候重哈希、什么时候可不重哈希；尝试完成的线性化点和无法消除的短竞态。不要承诺任何时刻外部修改都能被 SQLite 事务防住。

### 代码步骤

1. 给 progress 加“是否完整/可续”语义。检查 truncated、写入确认、事件窗口与过期后剩余队列。持久化失败不是合法净进展；必须显式上报，不只写 `_progress_write_error`。
2. 为每次预算结束记录可证明的进展：本次新观察数量、持久成功数量、仍未覆盖数量及下一步。若相同输入重复退出而无净进展，返回 typed unsupported/unavailable；不得又生成一个 source_budget 让用户盲点继续。
3. 单文件观察接受剩余 byte/time/cancel 预算，或在 S0 明确改成带硬上限的软调度阈值。读取前、读取中、末项后都有一致处理；不得用记录里的已知 size 当实际读字节数。
4. 最终 writer 验证匹配的候选集合/代次/root/policy；文件 I/O 仍在写事务外。候选在两阶段间新增或变化只能重试/拒绝，不能把没观察当坏图。
5. 保持 receipt、cursor、links 的最终事务原子性。不能把“完成一个切片”写成“来源页已接受”；source 结束必须等实际提交结果。

### 验收矩阵

- 同 inode 等长异图：mtime 改变/恢复两种都拒绝严格复用；旧 completed/artifact/旧 batch links 不改写。
- 原文件完全没变：正确复用，额外 I/O 有实测且可解释；不能为了修缺口把所有已有文件都强制重下载。
- 33/64 页、正好 256 页、257 页、支持上限、支持上限外；多版本候选、过期/截断、连续进度写失败。
- 每轮预算结果后有净进展，或明确不可继续的 typed 原因；故障恢复后可重新受理，不移动 cursor。
- 1 字节预算/70 字节文件、最后文件越界、单文件时间限制、暂停在观察中/切片边界/提交前；测试实际读量和取消结果。
- MISSING/UNKNOWN/UNREADABLE 短缓存只能调度/hold；MISSING 之后恢复的正式图不被覆盖、删除或误判成功。

若 S0 调整 RFB-02/RFB-05 的异常协议，先写等价性质断言并解释为何当前红基线仍能暴露原问题，经主审确认后再接线；不能把“任意异常/任意状态”当通过条件。

## S2 · 清理扫描和存储故障闭环（RFB-03/05）

唯一负责人：`page_delivery.py`、`delivery_repository.py` 清理部分、`delivery_cleanup.py`（如需要）。若更改共用异常，S0 先冻结，由主集成者串行改 `library_repository.py`。

1. `_scan_cleanup_tier` 用正式结构返回 EOF 与预算耗尽；两路游标各自传递，不用空列表推断尾部。保留位挤掉的 fresh 行下次必须可达。
2. `_sweep_cleanup` 只有确定 EOF 才回绕；到 cap 就保留 cursor，下次从其后继续。retry tier 的内部回绕同样遵守该规则。
3. 区分 `_db` 封装的 InvalidStateError 与真正不可读故障。CAS 失败后读通则报告赢家，读不通则传递 storage/trust fault。必要时保留异常 cause，不用字符串猜测。
4. 覆盖 recover_local 的最外层与实际启动/控制调用者；不得私有 helper 抛对了、外层又吞回一般 recovery_needed。调度收到 fatal 后停止新派发，未确认 bytes/row 保留；可读但补偿写失败不需要重复盲写。
5. 保持唯一暂存文件删除实现与往返后复核，不因队列修复新建快速 unlink。清理失败不是下载完成，任何不确定均不得销毁正式图或外来 part。

验收：limit=1/2/200；超过两倍 examine_cap 的坏前缀后放合法项；两个 tier 各自坏前缀；持续新行+失败重试；真实 EOF/空库/仅重试；补偿写失败但可读、连续读取失败、publish_authorized 损坏、竞争赢家 completed/cancelled。观察五轮游标和访问次序，而非仅测试 repository 返回列表长度。

## S3 · 有界解析真实边（RFB-04）

独立负责人只改 `delivery_lineage.py` 和自己的测试。冻结后由主集成者串行接 `delivery_recovery.py` 与 `delivery_repository.py` carry；后者与 S2 属于同文件，禁止并行写。

1. 把 rowid 窗口看成候选集合，不能当作全部关系。精确 handover 可以指向早于 anchor 的 completed；中间 superseded 节点也可能携带这种边。
2. 在有界集合内批量装载事件/目标，迭代解析，visited 防环。预算计入事件节点与目标查询；预算耗尽是 budget，不是 none。
3. 明确每层权威优先级，严格质量作用域、output/page、artifact 与 completed 绑定；显式坏目标不得静默退化为其他成功。
4. 保留两阶段一致性：写者重读后继，如变更则 typed stale/retry。不能用进程级永久 memo 让变化后的结果仍命中。
5. 用 A→B→早期 C 的结构和公开 recover_verification_page 验收；补三层链、环、坏中间事件、不同质量、目标在预检/提交间改变。三个消费路径都用同一结果，不能各写兜底。

性能验收沿用 8/16/24/32/64 节点，但同时记录目标是否正确、唯一节点/扫描数和 SQL 工作量；不能仅达到“2 条 SQL”却不再解析必须的边。全表 events 扫描的索引/投影仍另立迁移，不把有限语句数当恒定成本。

## 文件所有权、集成和收尾

| 工作包 | 可并行部分 | 必须串行的部分 |
| --- | --- | --- |
| S1 观察 | library_repository、observation_snapshot、专属测试 | 共用异常/命令、source/control 接线 |
| S2 清理与恢复 | page_delivery、delivery_repository 清理区、专属测试 | 与 S3 共享的 carry 接线；共用库原语 |
| S3 后继 | delivery_lineage、专属测试 | delivery_recovery/carry 消费接线由主集成者完成 |
| 独立验收 | 仅新反例与测量，可只读跑各树 | 不改产品、不弱化旧断言、不自行改契约 |

如果 S0 不能隔离文件/事务，就串行 S1→S2→S3，不以“加速”为由交叉修改。每包可审查的小提交，合并后才接共有调用者。

收尾顺序：语法/适用类型检查 → 相关反例 → 非 Tk 全量一次 → Tk 验证并保留首次结果 → 按 AGENTS 用继承上下文的只读 fork 对照契约逐行审 diff。发现问题优先退回原开发代理上下文，不用新代理凭总结宣告关闭。

验收必须同时给出：RFB 各项性质、旧 RFA/RF/UA 回归、历史成功事实摘要不变、资源/推进量、真实测试版本 SHA、未测项、未修项。当前导航/TASK_PLAN/LOG 统一到同一提交与报告；历史 session 不改，失败原件不覆盖。正常提交推送后核对远端 SHA，不能只说“已留档”。

## 本轮复现与后续顺序

在被审 checkout 根，用现有共享 Python；设置 `PYTHONNOUSERSITE=1`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`：

```powershell
python -s -B -X utf8 -m pytest docs/audits/2026-11-14-u1a-rfa-recheck/evidence/test_rfa_recheck.py -q -s -p no:cacheprovider
```

建议为每次运行显式指定工作区 .runtime 下尚不存在的 `--basetemp`、日志与 JUnit 路径。脚本默认从所在 checkout 解析项目根，测试只创建合成库和 PNG、使用 fake HTTP；可用 PIXIVKEEP_AUDIT_REPO 指定另一冻结树。不要把这些预期红的审计文件混入普通绿色基线后再用 skip 隐藏。

优先关闭 RFB-01..06。之后评估 W5 展示/重核验入口与 finish_source 结果协议；事件索引/投影、carry 事务、claim 成本和真实迁移/外置卷另做批次。归档工具/封包/回读/释放依旧按原 A2–A6 门槛推进，本轮不需要用户搬回 500 GB，不触发真实下载或资料删除。
