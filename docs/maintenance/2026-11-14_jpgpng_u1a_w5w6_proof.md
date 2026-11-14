# JPGPNG-U1A-W5W6-PROOF-20261114-01 维护记录

本批修复 [W5/W6 审计](../audits/2026-11-14-u1a-w5-w6/README.md) 提出的 1 项 P1、4 项 P2、1 项 P3。契约见 [W5/W6 契约 v5](../optimization/2026-11-14/U1A_W5_W6_CONTRACT_V5.md)（含第 9 节 A1..A8 修订）；切换与回退见[runbook](../optimization/2026-11-14/U1A_SWITCH_RUNBOOK.md)。

## 1. 起点与基线

- 基线：`52e6936`（产品 `01b91c3`），审计分支 `audit/u1a-w5-w6-20261114` @ `0c11b1a`。
- 红基线：把审计的 `evidence/test_w5w6_recheck.py` 原样搬进 `tests/test_u1a_w5w6_boundaries.py`（只改 ROOT 推导 `parents[4]→parents[1]`），复跑得 **9 失败 / 3 通过**，与审计报告一致。
- 关键事实：上一轮我在 `verify` 的 docstring 里写「同样的行」，实现却只比对象名集合、表计数与完整性——**证明的含义是空的**。这是本轮唯一 P1 的来源，也是我先承认的错误。

## 2. 六项逐条

| 编号 | 根因 | 修法 | 判据 |
| --- | --- | --- | --- |
| **WC-01** P1 | `verify_library_copy` 从未比较行内容，也不读清单摘要；两台库各自「相对声明」偏离的方式不同也能通过 | 备份改为**封存 + 逻辑指纹**：清单 v2 记录对象定义指纹与每表逐行内容指纹（类型标签 + 长度前缀 + 流式哈希），`verify_backup` 逐项校验 `sealed`/`logical`/`healthy`/`compatible`，未执行的一项一律 `not_checked`、`ok=False`；`compare_library` 回答另一个问题 | 同计数改单值 → `sealed=mismatch` 且 `logical=mismatch`、`ok=False`、退出 2；正常 WAL 快照仍通过（正向对照）|
| **WC-02** P2 | `_scan_lines` 的 `else` 把「没有确定异常」升级成「每一行都与记录一致」，且把预算原因说成旧行或没摘要 | 结论改由引擎的 `coverage`/`unknown_reasons`/`ledger` 驱动；新增退出码 **5**（未覆盖/无法核验）；`--allow-unknown` 只放行两类旧行；unknown 原因逐条 `code: count`；续跑页到末尾不再说「库里没有成品」（复审 R1，见 §6） | `--max-bytes 1` → 文本不再出现「一致」、退出 5、原因 `file_exceeds_budget: 11` |
| **WC-03** P2 | `_verify_finished` 显示正确后，`_finish_verification` 无条件覆盖成绿色「完成」 | 保存每页 outcome 的有界桶（verified/repair_queued/held/refused/unknown/error），按最严重结果收尾并保留最后原因；`repair_queued` 不计入 verified | held/refused/未知三种返回后，最后可见结果不是 success；复用成功仍是 success |
| **WC-04** P2 | 核验查询用固定 pending key，切任务不清空；A 的迟到结果写进 B 的缓存并可驱动 A 的恢复 | 新增 `_verification_epoch` 与批次身份，四个作废点（选任务、同 job 新 batch、提交/恢复后、销毁）；请求捕获 `(epoch,batch)` 双校验；行带 epoch/batch，恢复前二次闸门；在飞命令跑完但标注「上一批」，剩余队列停派 | A→B 迟到 summary/pages 不进入 B；提交的命令 batch 与当前选择一致 |
| **WC-05** P2 | `_audit_row` 在验证前裸调 `int(row['size'])`，一条坏尺寸就让整次巡检抛 traceback | `_row_fields` 先验后转：坏 size → `unknown` + `artifact_record_invalid`（细码 `reason_detail`）+ `size_recorded_raw_type`；坏 identity → 结构化 `ledger` 错误 + 最后安全游标，不跳过不明范围 | TEXT/BLOB/REAL/NULL 尺寸与空 artifact_id 都只影响该行；无 traceback、不写库 |
| **WC-06** P3 | 摘要只统计 delivery，artifact-only 页被显示成「已登记 0 页」 | 新增 `existing_pages` 与 `unstarted_pages`，面板显示「已有 M 页」并保留恒等式 `total = linked + existing + unstarted`；删掉「已登记 N 页」与那句错 docstring | 复用成功后摘要出现「已有 1 页」，不为 artifact 伪造 delivery |

## 3. 契约修订（A1..A8）

实现期确认的八条，全部写进契约第 9 节：compare 与 verify 的 `ok` 语义分开（A1）；`-shm` 不封存（A2）；`coverage` 增加 `error`（A3）；坏行 `reason` 词表不变、细码进 `reason_detail`（A4）；`library_rows` 只在首屏、`empty` 不等于库空（A5）；scan 原因逐条打印（A6）；WC-06 的展示口径（A7）；作用域作废只摘 watcher 不 cancel（A8）。

## 4. 验证

### 4.1 判据与专项

| 文件 | 项数 | 结果 |
| --- | --- | --- |
| `tests/test_u1a_w5w6_boundaries.py`（审计 12 条反例） | 12 | **12 通过**（修复前 9 红 3 绿） |
| `tests/test_u1a_library_maintenance.py` | 82 | 全绿（原 60 项一项未删，新增 22） |
| `tests/test_maintain_scan_cli.py` | 23 | 全绿（原 10 项未删，新增 13） |
| `tests/test_u1a_library_audit.py` | 48 | 全绿（原 30 项未删，新增 18） |
| `tests/test_collection_verification_panel.py` | 60 | 全绿（原 45 项未删，新增 15） |

变异验证：S1 8 处、S2 7 处、S3 7 处，全部由对应测试捕获（S3 的 1 处判为等价变异并说明理由）。

### 4.2 全量与 Tk

最终一次干净运行（`verify_matrix.ps1`，无其它作业并行，跑在 `3ff949d`）：

| 步骤 | 结果 |
| --- | --- |
| 非 Tk 全量（`--ignore` 两个 Tk 文件） | **2,149 项，2,145 通过，0 失败，0 错误，4 跳过**，680.9 秒 |
| Tk 逐条隔离（presentation 4 + source_presentation 5） | **9/9 通过** |

2,149 = 上一轮 2,068 + 本批新增 81（S1 +35、S2 +18、S3 +15、审计反例 12、续跑回归 1），逐项对得上；4 个已登记跳过保留。

### 4.3 真实库副本演练（只读源 + 副本操作）

`probe/wc_acceptance.py` 八步全过（证据 `acceptance/acceptance.json`）：

| 步骤 | 结果 |
| --- | --- |
| 基线 | 真实库 4,435,968 字节 / sha256 `0cba55b8…34d3d` |
| `backup` | 清单 v2、`verified=True`、源库未变 |
| `verify`（干净） | `sealed=ok`、`logical=ok`、`healthy=ok`、`compatible=ok`、`ok=True` |
| **改一行（行数不变）** | `sealed=mismatch`、`logical=mismatch`、`内容不同的表: jobs`、`ok=False`、退出 2 |
| 还原该成员 | 重新 `ok=True` |
| `compare` 源与副本 | `logical=ok`、`sealed=not_checked`、`question=same_content_as_source`、`ok=True` |
| `scan` CLI | 11 行全部 `match`、退出 0 |
| 真实库收尾 | 字节与 sha256 与基线完全一致 |

另外实测：v1 旧清单（`record_backups/pre-w5w6-switch`）现在明确 `ok=False` + `manifest_too_old` + 退出 2，并提示重新备份；已用新代码重做一份 **封存 v2** 备份 `record_backups/pre-w5w6-proof`（`sealed/logical/healthy/compatible` 全 ok）。

## 5. 边界与未做

- 不做签名/密码学；清单摘要只证明「相对清单未被改」。
- `unstarted_pages` 在当前 CHECK 约束下结构性恒为 0（`batch_item_pages` 不允许第三类页），仍按计算字段实现。
- 面板仍只列前 50 行、不分页；全量巡检走 `maintain.cmd scan`。
- 未做「取消正在飞的恢复」：契约要求它跑完；中途换选择只停派剩余队列并显示上一批剩余页数。
- 真实外置卷/网盘掉线仍只用目录改名模拟；本批没有故障注入的真实盘演练。

## 6. 只读独立审计

对象 `87fdc2a`（基线 `7fec9dc`），报告 `.runtime/jpgpng_u1a_w5w6_fix_20261114_01/audit/REPORT.md`。审计只读、未改仓库、未碰真实库。

- **结论：P1=0、P2=0、P3=1**。六个编号全部 **CLOSED**，每条都由审计自己写的反例复跑（不是引用 lane 自述）。
- 它自己测得的数字：判据 `tests/test_u1a_w5w6_boundaries.py` **12/12**（修复前 9 红 3 绿，判据文件与审计原文只差 2 行 ROOT 推导）；四个相关测试文件 **207/207**；面板 **60/60**；它自写的对抗探针 **33/33**。
- 它独立跑了真实库副本演练：改一行 state（行数不变）→ `ok=False` 且 `sealed`+`logical` 双 mismatch；真实图库 11 行全 match；**原库 4,435,968 字节 / sha256 `0cba55b8…34d3d` 前后完全一致**。

**P3（复审 R1，已修）**：`_scan_is_empty` 在续跑页（`--after` 传入且游标在末尾）把 `coverage=empty` 当成「库里没有成品」，于是对非空库打印「库里没有已登记的成品可核验」。修法：续跑页不再判空库，改说「游标已在末尾，本次没有可核验的行。」；新增回归测试 `test_a_continuation_past_the_end_is_not_an_empty_library`（提交 `3ff949d`）。

**记录为边界的三条（不影响交付）**：

1. **N1**：账本错误在 `--json` 下有两种形状（`LibraryAuditError` → `{"status":"error"}`；结构化 `coverage=error` → 完整报告 + `exit_code: 3`）。事实一致，形状不同；下一批统一。
2. **N2**：`logical_fingerprint` 的行序是 rowid 序，因此 rowid 被重分配但内容相同的两个库会报「内容不同」。对备份无影响（`conn.backup()` 复制 rowid），已在函数 docstring 记明。
3. **S1**：`verify_backup` 未要求 `copy_name` 属于封存成员；要变成假通过必须同时伪造清单摘要，而清单不签名、契约已排除该威胁模型，故未计缺陷。

审计的交付判断原话：**这棵树可以替换日常入口**。

## 7. 证据

`.runtime/jpgpng_u1a_w5w6_fix_20261114_01/`：`red02/`（红基线 9 红 3 绿）、`lanes/*/run*.xml`（三条 lane 过程记录）、`runs/*.xml`（合流后逐步复跑）、`verify/`（最终矩阵）、`acceptance/`（真实库副本演练与 v2 备份产物）、`probe/`（演练脚本）、`audit/`（独立复审）。
