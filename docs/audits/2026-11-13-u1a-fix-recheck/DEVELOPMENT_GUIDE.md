# 下一轮开发执行指导

本指导针对 `3ee638f`，不是要求推倒已经有效的修复。先读[结论](README.md)、[问题清单](FINDINGS.md)和[冻结契约](../../optimization/2026-11-13/U1A_STATE_CONTRACT.md)。当前 9 个问题优先于 UI 接线和新功能。不要因旧 14 条反例通过，就删除本次新增红测试。

## S0：冻结基线与澄清契约

1. 从本审计分支建立独立修复分支；确认产品树与 `3ee638f` 一致，当前审计提交仅包含文档和 evidence。不要切换日常目录到未验收版本。
2. 将本目录 evidence 中的 15 项检查接入正常 tests，调整导入方式即可。冻结红基线应为 **13 红/2 绿**；旧 14 项仍应全绿。不能改断言、xfail、跳过或扩超时来消除红基线。
3. 主代理先完成接口阅读与下面三组契约的对齐，把方案给用户并结束该轮。遵循根 AGENTS 的跨轮 fork 要求，不在同一轮刚读完文件就派发复杂业务子任务。
4. 定义事实：历史交付/当前页引用/输出槽所有者/后继关系/意图版本/发布阶段/清理待办/受理票据分别是什么，哪些不可变。不要再把这些状态合成一个 cancelled 或一个 bool。

需要在编码前决定的三个点：

- **发布与排除**：沿用 gate-wins 时，允许发布判断先于最终文件出现；界面不能承诺“点排除返回后所有尚未落盘页都不会出现”。若要更强保证，设计与写者串行的发布预留和 durable 决议，以及重启时如何恢复。只加第二次查询仍有竞争窗口。
- **暂停**：正式受理且登记的在途页允许有界收尾；暂停边界后的新 admission 拒绝。已读 running 但尚未登记不应算正式受理。整页取消时不推进 cursor，不凭不完整核验生成健康结论。
- **取代**：superseded 表示后继处理，不等于用户放弃；历史链接保留，新的活动批次应解析后继并重新核验可用字节。不能把 EXCLUDED/user_cancel 与 superseded 混用。

契约现有文字也需统一：removal_scope 模块头仍称 claim 为线性化点；状态契约既允许受控 prepared→cancelled，又在“禁止捷径”笼统禁止；coordinator 注释仍称控制在 caller thread。逐条改成最终实际协议，并保留变更理由。

## S1：先让排除、崩溃恢复和清理闭环（RF-01、RF-09）

主要文件：`core/page_delivery.py`、`core/library_repository.py`、`core/delivery_repository.py`。这些函数同属一个交付事务边界，由同一开发 owner 串行修改。

实施步骤：

1. 列出 prepared/recovery_needed 的 prepared evidence、owner batch、generation 与临时/最终文件组合。`recovery_needed` 不能直接等同“未发布”。
2. 本地观察放写事务外；最终文件属于自己且已发布，走确认；最终文件不存在且有合法未发布取消资格，走取消；外来最终文件/证据不全/不可读，保留字节并 hold。
3. 写者执行 CAS 时绑定 expected-old 和当前意图/决议，拒绝不同 owner、代次或已变更阶段。明确 `_StorageFault` 是库故障还是可表达的单项结果；不要把全部异常吞掉，也不要让合法的 recovery_needed 状态被误报成存储故障。
4. 取消后保留可查询、可重试的清理事实。若新增表/列，必须另开迁移契约和回退验收；若复用既有事件，明确检索/幂等/生命周期，不靠扫描任意文件发现所有权。
5. 清理只处理有精确证据的本项目临时文件。unlink 失败保留待办；重试成功关闭待办；不改变历史 artifact、不发布取消内容。处理崩溃在“状态提交后、unlink 前”和“unlink 后、清理回执前”两处。

验收矩阵至少包括：

| 维度 | 必测值 |
| --- | --- |
| 状态 | prepared、recovery_needed、cancelled、completed |
| final | 不存在、自己已 link、外来同尺寸文件、路径类型改变 |
| 意图 | 无、EXCLUDED、随后撤销、坏/未知投影 |
| temp | 正确归属、内容改变、已不存在、临时锁定 |
| 重复/中断 | 同命令重放、重复 recover_local、上述两个崩溃点、启动恢复 |

验收断言：不覆盖外来文件、不误删已发布原图、不重复生成 artifact；被取消页不联网；可恢复本地情况不让启动抛未分类异常；清理失败有可操作诊断且之后可安全收敛。

## S2：关闭共享页引用与恢复证据缺口（RF-02、RF-04、RF-05）

主要文件：`core/delivery_recovery.py`、`core/delivery_repository.py`、`core/library_repository.py`，必要时 follow/query carry 的调用契约。

### 取代关系

1. 先画 A 历史完成、B hold、A 续跑接管、B 再续跑这四步的数据行关系；加入 C 也链接同一 hold 的情况。
2. 新活动批次遇到 `superseded_by_recovery_*`，追踪明确后继或重新选择同页面合法候选并核验。防循环、悬空引用、后继仍在传输、后继再次被取代。
3. 只改变当前活动批次的引用/结果；过去批次保留当时收据。不能直接“把全部旧 batch_item_pages 改为新 artifact”。
4. 保持唯一输出槽和 work budget；接管不抢 transferring/validating/prepared。质量、路径、页索引必须匹配；不同质量策略的外国 hold 不能继承为本任务成功。
5. 新增 A/B/C 轮换续跑的端到端反例；接管方与被接管方都要验收，而非只看接管返回 reused。

### 证据绑定

1. payload 无论 verdict 正负，都保留 `observed_artifact_id`；与“本次是否选中可复用 artifact”分开。
2. reuse/repair/park 均校验：行存在且有效、expectation.matches_row、页面/质量/输出路径、root revision、策略、时效、expected-old delivery/state/generation。
3. 对无历史 artifact 的真正新页单独表示，不伪造一个 artifact ID 或 missing 观察来共用路径。
4. 拒绝时不退休 hold、不新增 pending、不改 cursor、不消耗预算；诊断事件可以写，但 requested/applied action 必须区分。
5. 逐字段负测：ID、摘要、大小、output_ref、root revision、policy、checked_at、旧状态/代次；验证真实表/文件变化，不只断言 returned dict。

### held 的调度意义

所有 `held` 的危险/未知情况都需检查 list_dispatchable。无 resolver、观察未产生、过期证据、根不可用、读失败分别有理由；普通尚未下载且无历史 artifact 的 pending 可以保留 repair_queued。若没有充分事实允许 PARK，返回 refused 并明确保留原状态，不能声称已经挂起。

## S3：统一控制协议，补齐生命周期和取消（RF-03、RF-06、RF-07、RF-08）

主要文件：`core/task_coordinator.py`、`core/collection_runtime.py`、`core/library_repository.py`，以及 follow/query 的等待协议。这组必须由一个 owner 处理；不要按“一个人 observer、一个人 control”拆同一文件。

1. 用统一分类把全库坏投影故障交给控制 owner；accept/claim/recovery/publish 都应关闭本轮 activation 并保留具体错误。单图 HTTP 失败继续按单图策略处理。
2. admission 使用每批票据/代次。登记 outstanding 与 put、关闭检查在同一协议中，暂停拥有明确边界；发生排队失败或 Future 取消也要归还票据。
3. `settle_stop` 看正式登记的 outstanding，不以 `qsize()` 充当事务事实。测试 dequeue 已成功而 worker 尚未递增计数的窗口。
4. `_shutdown`、`_closing`、fatal owner 的关系写清楚。所有 service loops 都能退出；所有已接受 Future 有唯一终态。close 检查新增线程自身调用、重复关闭和超时重试；writer 最后关闭。
5. 将现有 `ObservationRequest.cancellation` 接通，并在两张图/两个候选之间检查。添加整页总字节/时长/数量预算；单文件预算保留，不能通过禁用摘要校验“提速”。
6. 取消和预算耗尽使用专门控制结果，禁止转换为 MISSING/CHANGED。未完成源页的 cursor 不前进；通过 receipt/命令标识确认已经提交的页。
7. 改造 `.result(20)` 的协议：等待超时不等于工作已撤销。保存命令身份、查询最终结果，区分仍处理/取消已确认/已提交/被拒绝。不得超时后无条件再投同一批新页或只扩大 20 秒。

必测交错：pause 先于 admission、admission 先于 pause、锁外读后暂停、取队列后计数前暂停、首张读中暂停、排队 observation 与 close、满队列 close、启动恢复抛错、owner 运行中抛错、Future 预先取消、关闭超时后再关闭。使用 Event/Barrier/显式钩子；时间断言仅用于响应上限，不用于制造特定顺序。

至少用 CollectionRuntime + 假 source 做一次坏意图集成测试，断言后续任务既不激活也不发元数据请求；本审计反例已经证明激活漏洞，尚未做真实网络。

## S4：性能与待办接回总计划

RF 全部关闭后，再处理已登记的意图索引/迁移和 carry 长事务。测量包括：每页 SQL 扫描行数、读盘次数/字节、事务最长持锁、控制 Future 延迟、最终 paused/closed 延迟；不能只量按钮返回速度。

规模分层：小样验证因果 → 200 作品×10 页 → 单作品 2,000 页 → 少量真实大小的合成文件。不要一次生成数百 GB 或读取用户图库。意图投影要与 append-only 事件原子维护，处理坏事件、重建、任务删除生命周期与 root revision；不能用内存缓存无限期绕过意图。

carry 拆成可续窗口时，cursor/预算/页链接/事件必须同一提交；失败恢复不漏页、不重计作品。原来的 10.8 秒是旧版证据，必须测新值，不能沿用为本轮性能数据。

W5 UI/CLI 后置接线：用户主动删图不自动视为事故；明确区分“核验”和“授权补下载”。completed 重新核验独立 API、旧 record_manager 健康门、真实卷/迁移/备份回退、归档回读/释放各自保留门禁。继续原图优先、性能其次、无缩略图、相对路径、少依赖、认可的白蓝界面。

## 并行、提交与复审

本轮问题大量共享 library_repository 和同一状态机，不适合再次把产品修复按问题编号强行五路并行。若用户确认下一轮并行，推荐：A 负责产品串行集成，B 只写独立反例/整链测试，C 只做规模测量与文档；文件所有权列明，各自固定基线。不允许两个子代理同时修改同一文件或事务边界。

所有业务子任务继承上下文，提示词原样带根 AGENTS 指定句子，不降级推理。主代理先完成对齐并结束该轮，用户确认后再 fork。每包集成后先语法/类型检查（仅使用已有工具）和相关测试，最终跑全量，再只读 fork 审契约与 git diff；问题退回原上下文修复。

每个有意义包提交：源码、针对性测试、当前进度、准确验证统计。只更新一个 authoritative current 指针；历史报告标注版本保留。测试失败不得清空工作树或移动旧标签；保留失败输出，修复后另开结果编号。

GitNexus 可辅助但不能替代检查：本次 `_cancel_unpublished_delivery` impact 只看出 dispatch/writer，动态 `_db/getattr` 的 executor→startup 链必须手动补读。修改前按项目约定做 impact，提交前 detect_changes，并用 `git diff --name-status` 复核实际范围。

## 复现本次反例

在**固定审计版本的仓库根目录**执行，证据和临时库使用仓库之外的新目录：

```powershell
$env:PYTHONNOUSERSITE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PIXIVKEEP_AUDIT_REPO = (Get-Location).Path
$auditRun = Join-Path (Split-Path (Get-Location).Path -Parent) 'reaudit-repro-new'
if (Test-Path -LiteralPath $auditRun) { throw '选择新的证据目录，不能覆盖已有结果' }
New-Item -ItemType Directory -Path $auditRun | Out-Null
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest `
  docs/audits/2026-11-13-u1a-fix-recheck/evidence/test_fix_boundaries.py `
  -q -s -p no:cacheprovider --basetemp="$auditRun/tmp" --junitxml="$auditRun/results.xml"
```

基线期望退出码 1、13 失败/2 通过。最后验收必须所有预期行为反例通过，并保留已通过的原14项、原图/不覆盖/恢复/预算测试；UI 环境的首轮错误与复跑分开记录，不能合成“从未失败”。
