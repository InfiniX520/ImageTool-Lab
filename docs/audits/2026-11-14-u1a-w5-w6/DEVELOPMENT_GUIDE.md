# 下一轮开发指导：修复 W5/W6 的证明与作用域缺口

输入基线固定 `52e69365d78aac3acda77cbb093ba9e07b74bece`，产品 `01b91c3`。先读本轮 [发现](FINDINGS.md) 与 [结果](evidence/results.json)，再读 v4 和 W5/W6 范围稿。旧 RFB 结论已用原始反例复核，不重新推倒已关闭的观察/清理/后继契约。

## S0：主线程先冻结四个接口问题

本轮审计已完成代码阅读与风险对齐，但没有授权某个尚未明确的修复方案。执行者先把以下决定写入一个版本化补充契约，向用户说明具体方案；遵守工作区要求，结束对齐轮，得到确认后才 contextual fork 并行开发。不要在同一轮刚看完代码就派无上下文智能体。

1. **备份证明的对象是什么**：区分 sealed-copy（相对生成时清单未改变）、current-source-equivalent（相对当前源的逻辑内容）、healthy（SQLite 内部完整性）、compatible（当前版本可接受）。每一项应有 `verified / mismatch / unknown / not_checked` 等明确状态；没有执行的检查不能默认 true。
2. **scan 的结论和退出码**：保留真实 reasons；约定 `coverage_complete`、匹配/排除/未知数量、确定异常及 stopped/truncated 的组合。现有 0/2/3/4 必须有兼容说明；可增加严格模式或不完整退出码，禁止悄悄换含义。文本/JSON/退出码必须表达同一事实。
3. **核验页面的作用域**：选择批次、读取 epoch、恢复操作 epoch、单页 delivery 身份、UI 最终结果如何绑定。明确已提交恢复遇到选择改变时继续完成原命令，但不污染新选择；未提交队列如何处理。
4. **行级坏数据与已有页统计**：坏记录哪些字段可报告 null/raw-type、哪些导致结构化停止；独立 existing_pages 如何与 total/linked/counts 对账。不得为了 UI 数字写入假的 delivery 或授予原图健康状态。

把本审计 `evidence/test_w5w6_recheck.py` 原样引入新分支的 `tests/`（只允许模块头/ROOT 路径适配），保留红基线 **9 红/3 绿**。其中 WC-06 是设计缺口测试；如选择别的等价字段/表现，先在契约记录变更，保留原测试，并新增对等的用户行为断言，不能静默删除。

## S1：备份与 CLI，首要修复 WC-01

独占 `core/library_maintenance.py`、`maintain.py`/`maintain.cmd` 的必要接线及相应测试。WC-02 的 CLI 汇总同属此包，不能另派一个人同时改这个文件。

### 1. 先定义并实现真正的校验材料

- 生成时使用已有 SQLite 在线备份 API，完成后在独立只读事务中取得副本完整逻辑视图。不要改成直接复制活动主文件，也不要对源执行 checkpoint。
- 清单记录格式版本、生成模式、对象定义指纹、逻辑内容指纹/表计数、最终备份文件的 SHA-256 和字节数、健康检查与兼容检查的独立事实。清单最后原子封存。晚期异常、磁盘满、fsync/rename 失败必须让目录保持明确“未完成”，不能留下成功标志；清理只能处理本操作实际创建、核验为非链接且在目标范围内的文件。
- `verify` 接受清单后必须实际核对清单内的每个成员；拒绝绝对成员路径、`..`、重复/大小写冲突、重解析点和越界；拒绝缺失成员、错误摘要或未知清单版本。不要只验证 library_name 或主文件大小。
- 清单摘要证明的是相对清单的完整性，不是对敌手同时篡改库和清单的真实性保证。不要把普通 SHA 清单称成数字签名；本批无需引入密码包/依赖。

### 2. 逻辑比较与封存验证分开

现有 `verify_library_copy(source, copy)` 文档承诺同对象、同行内容，应实际比较，或版本化拆成两个明确入口。不能仅改文案把 P1 降级，同时继续让恢复 runbook 把它当充分证据。

- 对象比较读取每边实际定义，包括已接受的额外对象；额外 trigger/view 的接受规则也要明确。不能用“各自相对于声明的异常名称一样”代替“彼此一样”。SQL 字符串含引号时不可用简单空白压缩改变字面量；无法安全等价归一时保守报告差异。
- 逻辑摘要需要稳定列顺序、确定行顺序、SQLite 类型标记与长度编码，区分 NULL/TEXT/BLOB/整数/实数；考虑无主键表、重复行、WITHOUT ROWID 和额外表。不要使用 Python hash()、无序 SELECT、把值全部 str()、或无边界字符串拼接。
- 使用流式游标/分批读取，不 `fetchall` 整个库；长读时间、忙等待与取消/预算应有真实终止策略。持有只读快照期间会影响 WAL checkpoint，避免界面线程执行，也不要假称对写者完全无影响。
- 对当前源比较必须清楚标识观察时点；源在备份后新增/修改并不证明封存副本坏了。清单封存校验和“与现在源不同”应能同时成立。
- 文件组模式保留整组语义；SHM 是可重建的簿记，是否作为封存校验成员必须明示。不要删备份中的有效 WAL，也不能把目标遗留 WAL 与另一个主文件任意组合。先有合成回退演练，再调整 runbook。

### 3. 本包验收矩阵

同计数改单字段、交换两条值、NULL/TEXT/BLOB 差异、同名异定义额外对象、正常 WAL 快照、备份后源继续写、清单缺失/未知版本/错误成员、复制或封存时失败、反复验证不改封存主文件、只读源/目标拒绝和恢复后逻辑结果一致。文件组恢复沿用本轮通过对照，别为修一个问题破坏它。

不得通过“main SHA 不同即失败”解决本项；正常 WAL 正向对照必须保持通过。真正还原和删除真实库仍不属于合成测试的授权范围。

## S2：scan 的坏数据诊断与准确结论

独占 `core/library_audit.py` 和其测试，CLI 输出交 S1 的唯一文件所有者接入。S0 已冻结的返回类型作为两包边界。

1. 在 `_row_fields` 的危险类型转换前做严格字段校验。合法缺摘要旧行、格式异常、路径拒绝、用户已排除、预算不足须保留不同 reason。
2. 一行异常不应吞掉整页。身份/排序键可用时返回 unknown/record_invalid 并继续下一行；身份或游标本身不可判定时，返回结构化账本错误和最后安全游标，不跳过未知范围。
3. 不把坏数值强转为 0，不写库“修复”，不触发网络；正常 matched 的完整 SHA 规则不变。
4. CLI 总结只有在明确达到相应覆盖范围且所有应核验行均 match 时才写“一致”；有 unknown 或被预算打断时写实际已匹配数和未核验原因。excluded 说明用户意图，不当作已哈希成功。
5. 明确默认 limit=200 只是一页，不宣传成一次完成整库。没有游标推进的时间/字节预算中断不能鼓励无限原参数重试；显示需要增加预算或调整条件。

验收：两行中一好一坏仍看到好行；TEXT/BLOB/NULL/REAL 数值、非法 path JSON、坏意图、单文件超预算、页中途超预算、0 行、全部 excluded、缺摘要混合、完整全部 match；同时断言核心结果、文本最终行、JSON 与退出码。原有只读、游标、预算、取消测试保持。

## S3：UI 作用域与最终结果

独占 `gui/collection_controller.py`、`gui/verification_panel.py`、必要的 `gui/collection_view.py`、`core/delivery_repository.py` 的摘要读取增补及其测试。禁止分两个 agent 同改 controller。

### 1. 先修选择作用域 WC-04

- 给每轮核验读取生成单调 epoch，并捕获 batch_id。summary/pages 回调同时校验 epoch 与当前批次；A 的迟到结果不得写入 B 的缓存。
- 在 `_select_task`、同 job 新 batch 的 `_snapshot`、submit/resume 的批次更新路径统一作废读取状态。审查所有 `_current_batch` 赋值点，不能只补一个按钮路径。
- 请求开始到新列表被接受前，旧行不能作为新选择的可操作行。取消 Future 只是减少工作，不是完成顺序保障。
- 恢复入口也验证 row 批次/epoch，形成第二道 UI 作用域闸门。后端仍按现有意图、根版本、CAS 检查，不用 UI 状态替代它们。
- 运行中的恢复使用冻结的操作上下文，不能在切换任务后用当前批次改写命令；新选择的页面不显示旧操作结果。未发出的旧队列按 S0 约定停止或明确显示原批次任务，绝不悄悄混入新选择。

### 2. 再修结果聚合 WC-03

- 保存每页 outcome/reason 的有界结果集合，分开 processed、verified、repair_queued、held、refused、unknown、error；最多20页，无需新依赖或数据库。
- 单页最后保留真实原因和合适颜色。批量按最严重结果呈现，并保留异常明细，不让最后一个成功覆盖前面的异常。
- 不认识的 outcome 和格式错误保留诊断；只有 exception 才是失败的旧假设需要修改。是否继续下一页由冻结策略决定，不用文本内容猜测。
- close 不再调 UI，不再发队列；测试 Future 异步完成而非只测立即 resolved 的快乐路径。

### 3. WC-06 可同包处理

保留 `counts` 的 delivery 状态含义，新增 artifact-only 已有页计数和真正未挂接页计数，摘要用“已有”解释。用一个短读快照取得各数，不新增每页 SQL；全库与单批两种 scope 都要对账。不把 artifact-only 的历史成功当作本次完整哈希健康证明。

验收须检查 **最后可见结果**、可操作行的 batch 与提交命令，而不是仅断言某个正确文本曾经在调用日志里出现。最少覆盖：A→B 慢查询、同 job 新 batch、summary/pages 逆序、刷新与选择交错、恢复途中切换、暂停批次、未知返回、混合20页、close、复用成功对照。Tk 用现有样式；本轮不重新设计风格、不加预览图。

## 分工、合流和复审顺序

| 包 | 独占关键文件 | 依赖 |
| --- | --- | --- |
| 主线程 S0 | 一份补充契约、红基线、任务记录 | 先完成并让用户确认；之后才能 fork |
| S1 备份/CLI | library_maintenance、maintain 入口与对应测试 | S0；接收 S2 新字段，自己完成 CLI 接线 |
| S2 scan | library_audit、对应测试 | S0；不改 maintenance |
| S3 UI/摘要 | controller/panel/view、delivery_repository 摘要、对应测试 | S0；不改核心状态转移 |
| 主线程合流 | 文档、统一验收与冲突裁决 | S2 → S1 → S3；不同时编辑同文件/同事务 |

满足工作区 fork 条件后，每个 agent 独立 worktree，继承完整上下文与最高推理设置，提示词保留工作区要求的原句。若要改公共返回结构，先退回主线程更新契约，不能各路自己猜。完成后由主线程跑编译/适用类型检查/全量，再派一个只读 contextual fork 对照契约审最终 diff；问题退回原开发 agent。

修复工作从 **52e6936 或包含它的审计文档分支** 建新分支。不要从仍引用旧 baseline 的导航选错代码。不得修改原来审计标签、改写旧报告数字、删红基线、清理其他 lane 的 basetemp。日常目录在当前版本，切换另作明确动作；本轮修复先保持独立。

## 验证命令与落盘

在新 worktree 使用工作区已有解释器，PowerShell：

```powershell
$env:PYTHONNOUSERSITE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:TCL_LIBRARY = 'D:\Miniconda3\envs\JuiLiu\Library\lib\tcl8.6'
$env:TK_LIBRARY = 'D:\Miniconda3\envs\JuiLiu\Library\lib\tk8.6'
$env:PIXIVKEEP_AUDIT_REPO = (Get-Location).Path
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest `
  docs/audits/2026-11-14-u1a-w5-w6/evidence/test_w5w6_recheck.py `
  -q -s -p no:cacheprovider --basetemp=../probe_fix01 --junitxml=../probe_fix01.xml
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest tests `
  --ignore=tests/test_collection_presentation.py --ignore=tests/test_source_presentation.py `
  -q -s -p no:cacheprovider --basetemp=../full_fix01 --junitxml=../full_fix01.xml
```

首次红基线、修复专项、最终全量各用新目录；`--basetemp` 会清理指定目录，必须检查它不存在且不属于其他运行批次。两个旧 Tk 文件的9个 test node 各起一个进程，保留每次日志/XML。新核验面板45项已在全量中，不重复计入总数。所有校验用合成库，不借“验收”读取或迁移真实图库。

通过门槛：既有 2,077 项零失败（4 个已登记跳过明确保留）；本轮反例按冻结契约转绿且3个正向对照不倒退；新增矩阵通过；最终只读审查无未处置 P1/P2。测试通过后再更新 IMPLEMENTATION、TASK_PLAN、LOG 和一份维护记录。仅关闭实际复现并修复的编号，不把长期遗留笼统写成全部完成。

## 本批之后

修复完成再考虑 finish_source 最终结果查询、意图索引/投影与迁移门槛、carry 分窗和 claim 成本。真实外置卷、重解析点与断电恢复须独立演练；归档 A2–A6 继续按原计划。不要为本批 UI/备份修复扩大成新数据库架构、新 Node 前端、默认自动补下载或真实资料释放。
