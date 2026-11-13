# 下一轮开发指导：先闭合 U1-A 状态与证据，再接界面

适用产品基线：`d3c5cf8`；完整开发提交：`8b1c751`。问题编号见 [FINDINGS.md](FINDINGS.md)。此页是执行计划，不表示修复已完成。

## 0. 开工前必须固定的事实

1. 从本轮审计分支建修复分支，或从明确的 8b1c751 加入本报告和反例。不要在日常主目录的 5465e0c 上误以为已有 W5；不要自动合并未审计功能到 main。
2. 保存本轮 14 项反例：基线应为 12 失败、2 通过。移入 `tests/` 时只适配导入和 fixture，保留行为断言；不得 xfail、skip、扩大超时或删掉失败。
3. 现有全量曾为 1,589 通过/1 失败/4 跳过（含单跑 Tk 合计）。排行榜单测复跑通过没有关闭 UA-01。先让确定性交错测试转绿，再谈稳定性重复运行。
4. 历史 artifact、完成 delivery、已接受来源收据不因修复被改写；当前健康、修复授权、用户意图和批次展示不能混成一个 completed 布尔值。
5. 不扫描真实图库，不登录、不上传、不删除、不释放；全部测试从空的合成目录/数据库开始。测试环境继续用共用 Python，无新增包和 Node 前端环境。

## 1. 先定一份共同契约

开始改 library_repository 前，先写状态表及事务输入，不让不同修复各自猜测。建议最小命令结构（名称可调整）：

| 输入 | 必须绑定的事实 |
| --- | --- |
| 页面目标 | job、batch、illust、page、output_key、quality |
| expected-old | 预检时 delivery ID/代次/状态、page link、候选集合版本 |
| 根 | 稳定 root ID 与持久映射 revision；路径仍用 PathRef |
| 观察 | 实际选定 artifact ID、期望摘要/大小、观察状态、时间、策略、文件身份 |
| 用户意图 | 当前意图键、意图版本、调用者请求的动作；recheck 不等于 repair |
| 命令身份 | command ID、命令种类、完整目标和语义摘要；同 ID 不同内容拒绝 |

同一观察记录只能用于它绑定的页面/路径/代次。hash_match、missing、changed 都是有时效的观察；missing 不是长期下载授权。

决定表至少包括：

| 条件 | 持久结果 | 派发 |
| --- | --- | --- |
| 有效 hash_match，符合复用质量 | 链接正确 artifact；退休当前 hold，保留历史 | 无 |
| missing + 未排除 + 明确修复动作 + 额度可用 | 新 pending 代次 | 允许 |
| root_unavailable / unreadable / unknown / changed_during_check | 真正的 needs_verification/blocked；不能留下 pending | 禁止 |
| changed，无可用已记录版本 | 保留字节并 hold | 禁止 |
| 当前 EXCLUDED | 不安排修复；阻止尚未授权发布的交付 | 禁止 |
| expected-old 或候选代次改变 | 返回需重新预检；不提交错误页面状态 | 禁止本次旧命令 |
| 意图/观察损坏 | 明确损坏结果，保守阻断 | 禁止 |
| 其他任务占有输出槽 | 表达阻塞所有者或受控接管流程 | 不抢写 |

## 2. D0：版本与反例保全

**改动范围：** 测试、文档、运行回执，先不改产品。

- 将 `evidence/test_independent_u1a.py` 的用例按行为分为并发接受、意图、恢复、效率四组；避免复制大量重复 fixture。
- 同步带入原 J-01、AR4 接受事务、传输/发布/崩溃守卫，保留两项本轮通过的字节和历史保护测试。
- 固定源码提交和环境，再跑红基线。最终修复回执必须区分“测试代码提交”和“产品代码提交”，不能引用另一棵工作树的绿结果。
- 把本轮 `ranking_failure.json` 与确定性交错用例一起交给执行者。**禁止只把 20 秒改成 60 秒。**

交付：红基线 XML、文件摘要、下一阶段使用的固定提交号。

从固定工作树根目录复现（报告目录必须是本轮新目录，不能复用已完成的 basetemp）：

```powershell
$env:PYTHONNOUSERSITE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PIXIVKEEP_AUDIT_REPO = (Get-Location).Path
$auditOut = Join-Path (Get-Location).Path '.runtime/u1a-red-baseline-new01'
if (Test-Path -LiteralPath $auditOut) { throw 'Use a fresh report directory' }
New-Item -ItemType Directory -Path $auditOut | Out-Null
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest `
  docs/audits/2026-11-13-u1a-independent/evidence/test_independent_u1a.py `
  -s -q -p no:cacheprovider --basetemp="$auditOut/tmp" --junitxml="$auditOut/red.xml" `
  *> "$auditOut/red.log"
```

基线退出码应为 1（12 个行为断言失败）。若出现导入错误、数据库建库错误或版本不同，先修运行现场，不能当作复现成功。修复后的同一命令应退出 0。

## 3. D1：关闭三个优先阻断

### D1a — UA-01：处理预检/完成交错

1. 预检快照携带候选 artifact/交付代次，而不只是一个可能为空的 health 列表。
2. 写者重新读取实际行。若预检后出现新的完成代次，回滚本次接受并返回专门的 retry-preflight 结果；不得新建 artifact_unknown 覆盖本批已完成链接。
3. 重试由事务外执行器负责，有次数/时间上限。保持 cursor、source receipt、预算、批次链接一次提交；不能先推进 cursor 再请求核验。
4. 同批已完成事实可作为重试定位依据，不能作为跳过当前内容检查的万能豁免。
5. 没有候选且仍没有历史完成时按正常新下载；根未绑定/观察失败时不把“无证据”解释为“无历史”。

验收：本轮 completion-race 测试绿；排行榜原用例绿；新增完成发生在候选读取前/后、写者前、另一任务完成四个闩锁交错；没有第二次网络下载、没有虚假 NV、没有重复预算或 cursor 跳跃。测试用 Event/Barrier，不用随机 sleep 证明无竞争。

### D1b — UA-02：统一移除意图作用域

1. 明确意图属于内容/路径还是任务；当前接受语义跨任务，必须保留对同一目标的全局排除能力。
2. 将 intent lookup 抽成唯一契约，接受、claim、recovery repair、重试与发布门都查询相同键和最新版本。
3. 记录 EXCLUDED 后，对其范围内尚未进行物理发布的排队交付实施取消/阻断；不要只按记录入口的 job_id 更新。
4. 撤销 NONE 是显式新事件。恢复按钮不能暗中撤销排除；command ID 重放也不能覆盖后续意图。
5. 已开始传输的发布交错见 D3；先保证旧任务入口对另一任务 pending/NV 的抑制。

验收：两个 cross-job 反例绿；同任务/跨批旧守卫绿；覆盖从历史下载记录、当前任务两种入口记录与撤销；验证旧完成收据不变。

### D1c — UA-03：held 必须真实停止派发

1. pending 遇根不可用等不确定观察，必须转换为不可派发 hold；定义 reason，而非返回 held 文案后 action=none。
2. 在 recovery result 与实际 delivery/batch 计数之间建立统一不变量：held ⇒ 不在 dispatchable；repair_queued ⇒ pending 且符合修复授权。
3. executor 初始化输出子目录与“已绑定根暂不可用”分开。不要把根不存在都当作可以 mkdir；相反也不能禁止合法新输出目录首次创建。
4. 重定位根必须用户明确操作、持久 revision 变化并重新核验，不默默沿用旧证据。

验收：离线根反例不再重建目录、不触发假 GET；增加 unreadable/unknown/changed_during_check 的 pending 状态矩阵；已存在外来文件保持原字节。

## 4. D2：恢复协议闭合（UA-04～06）

建议先在纯决定层实现，再接写者。

- **多代候选：** 返回明确的 selected observation；只要合法已记录版本匹配，决定、artifact ID、展示摘要和事件使用同一记录。没有匹配时另选择稳定的失败原因，不能混用第一个 changed 与后面的 matched ID。
- **所有权：** 不直接链接另一 job 的活动 hold 作为自己可操作的交付。选择“阻塞引用+提示所有者”或带 expected-owner 的显式交接；任何方案都需保持唯一输出槽、历史引用和幂等性。
- **证据：** repair/park/reuse 都传完整冻结观察；所有变更动作检查 age、policy、root revision、artifact/路径绑定和 expected-old。只有满足这些条件后才退休旧 hold。
- **回执：** 动作被拒绝时 event 中要同时有 requested_action 与 applied_action（或明确 applied=false），避免记录 action=repair 却实际未修的含混结果。
- **同 ID 重放：** 返回已接受结果，不重新读盘；不同动作/目标复用同 ID 必须冲突，不只比较三个页面字段。

验收矩阵：旧版匹配、新版匹配、都不匹配、候选超上限、观察过期、根版本改变、观察与页面不符、预检后状态变化、别任务抢到输出槽、重复命令。拒绝后全库相关行逐字段不变，不能靠最终文件未变掩盖数据库被提前退休。

## 5. D3：意图持久化与发布竞争（UA-10、UA-11）

### 损坏与持久化

- 读取错误保守阻断并给可诊断错误码，保留坏事件。不要 except/continue 后当作 NONE。
- 当前每次接受扫描全量 removal_intent 事件；完整 events 表又包含大量交付事件。设计带索引的最新意图投影，与不可变事件同事务更新，且能审计来源/重建。
- 长期意图不能随任务删除级联消失。先明确生命周期，再评审 DDL/迁移；**不要偷改既有 schema 号，也不要把下载库 schema 与 archive schema 5 当成同一个版本。**
- root_revision 恒 1 的现有实现只能表示当前进程的不可变映射，不能用作跨会话重新定位后的版本证明。其持久化方案须与意图键一起设计。

### 传输与发布

定义明确的发布授权线性化点，覆盖：排除先于 claim、claim 后、读流中、validation、prepare 前后、rename 前后、confirm 前后。

- 排除先被接受：worker 协作停止，最终发布不得发生；在磁盘/DB协议允许的点落 suspended/cancelled/held。
- 文件已经发布：返回“本次已完成，仅约束后续”，记录事实，不谎称阻止；不能为满足断言删除已发布图片。
- prepared/rename/confirm 的崩溃协议仍须恢复一致，不能把 prepared 行直接 UPDATE cancelled 了事。
- 进程退出后先核对发布证据与最新意图，再决定恢复，不自行联网。

验收：corrupt-intent 与 transfer-exclusion 反例绿；各发布交错子进程退出；旧 artifact/收据完整；临时文件保留与清理有明确归属。

## 6. D4：性能与可取消性（UA-07～09）

**先改结构，再测速度，不先增加下载并发。**

1. coordinator owner 只调度短命令。文件哈希转入有界 observation executor；传递停止 Event，整批限制文件数、总字节、总时长和结果 payload 大小。
2. observer 完成后再提交短写命令，命令保留 job/batch/run 绑定；暂停、关闭、切换任务后到达的旧结果不得推进 cursor 或启动下载。
3. 明确 Future 超时不等于事务取消。超时后可查询命令状态、等待回执或拒绝重入；不能重复提交直到偶然成功。
4. `_health_map` 每条 carry 命令只解析一次，follow/query 同时修改；映射按 artifact/页索引。必要的有效期/代次使用点检查保留。
5. 对已接受的精确同页重放先查 receipt，避免全文件读盘。一般核验缓存限定单批、文件代次和根/策略；跨会话默认不沿用。
6. 8 MiB 命令限制应在可预见时先预估/分片，不能读完大量文件才发现装不进队列。
7. 大 carry 拆可接续窗口，写事务预算独立于读盘预算。同步减少 intent 全表扫描与 `_refresh_batch_item_result` 对大多页作品的重复全量计算。

验收应保留两个层次：

- 确定性：哈希闩锁期间 snapshot/暂停可响应；关闭能取消观察且不留非守护线程；同页重放哈希次数不增加；carry 解析总量与 H 成线性。
- 规模：沿用 200×10 页与单作品 2,000 页两种形状，再加入较大合成文件、多代候选、第二批变化、进程退出。记录读盘字节、实际哈希次数、查询扫描量、写事务 p50/p95/max、暂停/关闭延迟、峰值 payload/内存；不要只报总秒数。

既有 10.8 秒 carry 写事务及 30 秒单文件预算超过控制响应需求。下一轮在实现前约定有界阈值并记录环境，不把硬件差异等同于功能已修；不强制新增 profiler 依赖，先用现有计时和 SQL/调用计数。

## 7. D5：界面、迁移与真正的验收

基础反例通过后再接 W5 UI/CLI，保持用户认可的白蓝界面、相对路径、无缩略图和少依赖。

- 分开“重新核验”和“核验后补齐缺失”。目前 recover 接口在 missing 时会排下载，不能直接接到一个看似只检查的按钮。
- 展示观察时间/严格或快速/当前状态/原因；批量操作有范围、预算、进度、取消与结果名单。
- 正常启动空闲；用户没有选择修复就不自动联网。已排除与已转存但未核验区别显示，OFFLOADED 未实现前继续拒绝。
- 原数据库副本备份/升级/崩溃恢复/回退演练通过后，才评审真实小样；不用本轮健康核验通过来宣称归档封包、上传、回读、释放已经完成。
- 真实重解析点与外置卷行为另做受控端到端测试。当前 3 项 skip 不改成假通过；保持平台边界可见。

最终验收必须同时满足：原套件全绿（解释 skip）、本轮 14 项与新增交错全绿、规模/取消/退出/迁移证据对应同一产品提交、文档状态一致。任何新增修复改变产品，之前绿结果都不能直接复用为最终验收。

## 8. 文件归属与协作方式

若后续需要三个角色并行，按下表分工；不要三个人同时改 library_repository 后互相覆盖。

| 角色 | 唯一写入范围 | 交付 |
| --- | --- | --- |
| A 集成/产品 | core 及最终契约、迁移 | 小步提交；每步固定接口和状态表 |
| B 独立回归 | tests 的独立文件 | 红基线、交错和字节/历史不变断言；不修改产品来“配合测试” |
| C 规模/文档 | 自己的 runtime 批次与报告草稿 | 对固定产品 SHA 运行，读写仅合成库；不修改 A/B 工作树 |

每人独立 worktree；由 A 顺序合并，报告不能引用“某代理正在收尾”当完成证据。需要共同文件变更时先交接所有权。可以串行完成，不为并发而拆成相互猜接口的补丁。

GitNexus 已在本次冻结树重建索引；query/context 用于定位，impact 对 accept_source_page 报 0 个上游，但实际 coordinator 通过 `getattr` 动态调用它。**工具的 0 不等于无人调用。** 修改前仍须按 AGENTS 运行影响分析，再人工核对动态分派、worker 和 source pump。

## 9. 留档与目录规则

- 当前总入口只维护 `docs/optimization/IMPLEMENTATION.md` 和 `TASK_PLAN.json.current_plan.active_workstream`；LOG 追加本次结果，旧审计原文不覆盖。
- 每批一个 `.runtime/<batch-id>/`：固定源码、探针、XML、结果、README。不要继续生成散落的 dbg1…dbg39 而没有索引。
- 新报告只复制可公开的小型证据；Cookie/Token、生产 DB、真实作品名单与图库不入 Git。
- 临时目录只有确认被哪些报告引用、状态已结束、可恢复归档且摘要核对后才可移动。现有保全基线、final worktree、原始失败 XML 不因“太多”就删除。
- 最终提交回执包含：base、product、tests、docs、final commit；未关闭问题；实际命令/退出码/数量；后续动作。不要把报告提交本身当产品新版本。
