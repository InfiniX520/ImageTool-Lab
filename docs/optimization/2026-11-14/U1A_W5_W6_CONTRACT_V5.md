# U1-A W5/W6 证明与作用域契约 v5（S0 冻结稿）

基线 `52e6936`（产品 `01b91c3`）。输入：[本轮审计](../audits/2026-11-14-u1a-w5-w6/README.md)（`WC-01..06`）。本稿只回答审计要求的四个接口问题，**不授权任何“只改文案就算修完”的做法**。

## 0. 一句话

v5 把“这个副本/这次巡检/这次核验能不能算数”从一个**布尔猜测**改成**四类独立事实 + 明确的未检查状态**；并把界面的“用户现在在看哪一批”从一个隐含变量改成**显式 epoch + 批次身份**。

## 1. 决定一：备份证明到底证明什么（WC-01）

### 1.1 四类事实分开报，缺省一律 not_checked

| 事实 | 问题 | 取值 |
| --- | --- | --- |
| `sealed` | 备份目录里的成员文件，是否仍与**生成时清单**逐字节一致 | `ok / mismatch / missing / not_checked` |
| `logical` | 副本的**逻辑内容**（对象定义 + 每表逐行内容）是否与清单里封存的指纹一致 | `ok / mismatch / not_checked` |
| `healthy` | 副本自身 SQLite 完整性 | `ok / failed` |
| `compatible` | 本版本能否打开它 | `ok / mismatch` |
| `source_equivalent` | 副本逻辑内容是否等于**当前源库**（另一个问题，默认 `not_checked`） | `ok / mismatch / not_checked` |

`ok` 只在**四项都执行且都通过**时为 `True`；任何一项 `not_checked` 或缺失，`ok=False`。**没有执行的检查不得默认为真。**

### 1.2 两个入口，名字就是语义

- `verify_backup(directory)` → 封存校验（清单模式）。这是 runbook 的恢复门槛：**备份坏了、被改过、缺成员、版本不支持都能当场发现**。
- `compare_library(source, copy)` → 当前源对照（“这两个库现在内容一样吗”）。给运维和演练用，**不**作为“备份是否可信”的门槛。

旧 `verify_library_copy(source, copy)` 保留为薄别名指向 `compare_library`，docstring 明确它不回答“备份是否可信”。CLI 对应：`maintain.py verify <备份目录>`、`maintain.py compare <源> <副本>`；旧的 `verify <源> <副本>` 两个位置参数写法保留为 `compare` 的兼容别名并在帮助里标注 deprecated。

### 1.3 逻辑指纹的冻结算法（必须逐字节可复现）

`logical_fingerprint.version = 1`：

1. **对象指纹**：`SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name`，每行按 `(tag,len,payload)` 编码后喂给同一个 SHA-256。**不使用** `rootpage`（物理位置与内容无关）。**包含** trigger/view 等额外对象：两库只要有一个对象定义不同，指纹就不同。
2. **每表指纹**：对 `type='table'` 的非内部表，取 `PRAGMA table_info(t)` 的列顺序，逐行流式读取：
   - 行序：有 rowid 的表 `ORDER BY rowid`；`WITHOUT ROWID` 表按主键列 `ORDER BY`；无主键按全部列 `ORDER BY`（重复行相同，顺序确定）。
   - 值编码：类型标签 1 字节（`0=NULL,1=INTEGER,2=REAL,3=TEXT,4=BLOB`）+ 8 字节大端无符号长度 + 载荷（INTEGER 用 `to_bytes(8,'big',signed=True)`，REAL 用 `struct.pack('>d')`，TEXT 用 UTF-8，BLOB 原字节）。
   - 记录 `rows` 计数。
3. **汇总**：`sha256("fp1" || 每表(名字, rows, 表指纹) 按名字排序 || 对象指纹)`。
4. **有界内存**：游标分批读取，**禁止 `fetchall` 整库**；批次 `1000` 行。
5. **只读**：`mode=ro` + `query_only=ON`，不 checkpoint、不写源。
6. **禁止**用 Python `hash()`、无序 SELECT、`str(value)` 统一化、无边界字符串拼接。

### 1.4 清单 v2

`manifest_version: 2`，在 v1 基础上新增：

```json
"logical_fingerprint": {"version": 1, "digest": "…", "rows": {"表名": 行数},
                        "objects_digest": "…", "objects_count": 12, "computed_at": "…"},
"members": [{"name": "library.sqlite3", "bytes": 4435968, "sha256": "…"}],
"finalizer": {"format": "manifest.json", "written_at": "…", "sealed_by": "backup_library"}
```

规则：

- 清单**最后**原子写（临时名 + fsync + rename），存在清单即代表封存完成。
- 读取清单时拒绝：绝对成员路径、`..`、重复名、大小写冲突名、重解析点、越界、未知 `manifest_version`、缺失成员、摘要不符。
- `manifest_version: 1` 的旧备份：`logical.state="not_checked"`、`ok=False`、`reasons=["manifest_too_old"]`，CLI 提示“重新生成备份”。**不静默放行**。
- 清单摘要证明“相对清单未被改”，**不是**抗同时篡改库与清单的真实性保证；文档不得称其为签名。

### 1.5 明确不做的

- 不用“源主文件 SHA 与副本主文件 SHA 相同”当通过条件（正常 WAL 快照就不相同，正向对照必须保持通过）。
- 不把“源在备份之后又写了”解释成“备份坏了”；这属于 `source_equivalent=mismatch`，与 `sealed/logical` 无关。
- 不引入密码学依赖、不做签名。

## 2. 决定二：巡检的结论与退出码（WC-02 / WC-05）

### 2.1 结果新增字段（S2 产出，S1 消费）

```python
result["coverage"]  # "complete" | "page" | "budget" | "cancelled" | "empty"
result["stats"]["unknown_reasons"]  # {reason: count}
result["stats"]["considered"]       # 本页取出的候选行数（含被停住的行）
result["stats"]["stopped_at"]       # None | "page_limit" | "max_bytes" | "max_seconds" | "cancel"
result["ledger"] = {"state": "ok" | "error", "code": None | "ledger_unreadable" | "cursor_unavailable",
                    "last_safe_cursor": [...]}
```

- `coverage="empty"`：库里**没有** artifact 行（单独一条 count 查询得出），不是“全部一致”。
- `coverage="complete"`：本页取完且没有更多行、没有预算/取消停止、没有账本错误。
- `coverage="page"`：被 page limit 截断（还有更多行）。
- `coverage="budget"` / `"cancelled"`：预算或取消。
- `ledger.state="error"` 时**不得**给出任何 verdict 汇总当结论。

### 2.2 行级坏数据（WC-05）

- 在 `_row_fields` 做**严格校验**，先验后转：
  - `artifact_id`：非空 str、长度 ≤128 → 否则 `ledger.state="error", code="cursor_unavailable"`，返回**最后安全游标**并结构化停止（不能跳过不明范围）。
  - `page_index`：int 且 `0..9999` → 否则该行 verdict=`unknown`，reason=`record_page_invalid`，继续下一行。
  - `size`：`int>0` 合法；`NULL`→`no_recorded_size`；其它类型（TEXT/BLOB/REAL/负数）→`record_size_invalid` + 该行 `size_recorded=None`、新增 `size_recorded_raw_type`（`"text"/"blob"/"real"/"null"/"negative"`）。**绝不强转 0，绝不当成 missing。**
  - `sha256`：`NULL`→`no_recorded_digest`；非 64 位小写十六进制 → `record_digest_invalid`。
- 一行坏数据不影响同页其它行；正常行的完整 SHA-256 规则不变。

### 2.3 CLI 结论与退出码

| 情形 | 文本最后一行 | 退出码 |
| --- | --- | --- |
| 库无 artifact | 「库里没有已登记的成品可核验」 | 0 |
| complete 且全部 match/excluded | 「本次核验覆盖全部 N 行，全部与记录一致」 | 0 |
| 有 missing/changed/unreadable/root_unavailable/changed_during_check | 列出明细 | **2** |
| coverage != complete，或 unknown>0 | 「本次只核验了 k/N 行，M 行无法核验（原因…）」 | **5** |
| 只有 legacy 类 unknown（`no_recorded_size`/`no_recorded_digest`）、无 findings、且 `--allow-unknown` | 明示「跳过 M 行无摘要旧行」 | 0 |
| `ledger.state="error"` | 结构化错误 + 最后安全游标 | **3** |
| 参数被拒 | 参数错误 | 4 |

- 0/2/3/4 的原含义不变，**新增 5** 表示“没核验完/无法核验”。
- **禁止** “本次核验的每一行都与记录一致” 出现在 unknown>0 或 coverage!=complete 的输出里。
- `--json` 与文本必须表达同一事实；JSON 里附 `exit_code`。
- `stats.budget_exhausted_reason` 语义不变；新增 `stopped_at` 区分“分页截断”与“预算停止”。

## 3. 决定三：核验页面的作用域（WC-04）

- 新增 `self._verification_epoch: int`（单调递增）与 `self._verification_batch: str | None`。
- **作废点**（全部要加）：`_select_task`（job 或 batch 变化）、`_snapshot` 里发现同 job 新 batch、`_submitted`/`_verified` 后批次更新、`dispose`/`request_close`。
- 每次请求捕获 `(epoch, batch_id)`；回调校验两者都等于当前值，否则**丢弃**（不写缓存、不渲染、不弹框）。
- `_verify_rows` 的每行携带 `epoch` 与 `batch_id`；恢复动作前二次校验 `row.batch_id == self._current_batch and row.epoch == self._verification_epoch`。
- 未提交队列：`_recover_verification` 只接收当前 epoch 的 key；已在飞的命令**继续跑完**（后端权威），但其结果标注为“上一批（batch …）”且不写入新选择的列表。
- 批处理中途选择变化：停止派发剩余页，结果区显示「上一批还剩 k 页未处理，已停止」。
- 取消 Future 只是省工作量，**不作为**顺序保证。
- 后端仍按现有意图/根版本/CAS 检查，UI 状态不能替代它们。

## 4. 决定四：已有页统计（WC-06）

`verification_summary` 保留 `counts`（delivery 状态）含义，**新增**：

```python
"existing_pages": int,    # batch_item_pages.artifact_id IS NOT NULL AND delivery_id IS NULL
"unstarted_pages": int,   # total_pages - linked_pages - existing_pages
"linked_pages": int,      # 有 delivery 的页（含义不变）
"total_pages": int
```

- 对账恒等式（测试必须断言）：`total = linked + existing + unstarted`。
- docstring 里“total-linked 恰好是尚未开始的页”这句**删掉**（它是错的）。
- 面板摘要改为「本批 908 页 · 已有 1 页 · 待核验 0 页 · 停住 897 页 · 已完成 11 页」，为零的段可省略，但 `existing` 非零时必须显示。
- **不**为已存在的 artifact 伪造 delivery，不把历史 artifact 当作当前磁盘健康。

## 5. 红基线（S0 交付物之一）

把审计的 `evidence/test_w5w6_recheck.py` **原样**引入新分支 `tests/test_u1a_w5w6_boundaries.py`，只允许改模块头与 ROOT 路径推导（审计树相对路径 → `parents[1]`）。保留 **9 红 / 3 绿** 作为修复起点；WC-06 若改用等价字段，先在 `docs/audits/2026-11-14-u1a-w5-w6/CONTRACT_DELTA.md` 记录变更并补一条等价用户行为断言，**不删原测试**。

## 6. 分包与文件归属（同文件不并行）

| 包 | 独占文件 | 依赖 |
| --- | --- | --- |
| S1 备份/CLI | `core/library_maintenance.py`、`maintain.py`/`maintain.cmd` 的必要接线、`tests/test_u1a_library_maintenance.py`、`tests/test_maintain_scan_cli.py` | S0；消费 S2 新字段（§2.1） |
| S2 巡检 | `core/library_audit.py`、`tests/test_u1a_library_audit.py` | S0；不改 maintenance |
| S3 UI/摘要 | `gui/collection_controller.py`、`gui/verification_panel.py`、必要的 `gui/collection_view.py`、`core/delivery_repository.py` 摘要增补、`tests/test_collection_verification_panel.py` | S0；不改核心状态转移 |
| 主线程 | 契约、红基线、合流、文档、tag/push、复审 | S2 → S1 → S3 |

## 7. 验收矩阵

1. 审计 12 条反例：9 红转绿、3 绿不倒退（`test_control_online_snapshot_wal_is_valid` 必须仍绿）。
2. 各包新增专项（S1 ≥22、S2 ≥14、S3 ≥12 条，含失败路径与变异验证）。
3. 既有 2,073 项零失败，4 个已登记跳过保留；Tk 9 项逐条隔离。
4. 真实库副本演练：`backup`(v2) → `verify`(manifest) → 改一行 → `verify` 必须 mismatch → 还原 → ok；`compare` 在源继续写时给出 `source_equivalent=mismatch` 且 `sealed/logical=ok`。
5. 只读复审无未处置 P1/P2。

## 8. 本轮不做

新数据库架构、新前端框架、默认自动补下载、真实资料释放、恢复 runbook 的自动化、密码学签名。日常目录保持当前版本，修复只在独立分支；切换是之后一个明确动作。
## 9. 修订记录（实现期确认，均经审计反例验证）

**A1（compare 与 verify 的 ok 语义分开）**：
- `verify_backup(dir)` 的 `ok` = `sealed`+`logical`(对清单)+`healthy`+`compatible` 四者皆执行皆通过；给了 `--source` 才额外算 `source_equivalent`，且**不**影响 `ok`。
- `compare_library(source, copy)` 回答的是它自己的问题，因此两侧的内容比较落在 `logical` 这一项；`sealed` 与 `source_equivalent` 明确 `not_checked`，并返回 `question="same_content_as_source"`。`ok` = 对象定义相同 + 逻辑内容相同 + 健康 + 当前版本可打开。
- 理由：`test_control_online_snapshot_wal_is_valid` 要求 compare 在正常 WAL 快照上 `ok=True`；把 compare 的 `ok` 也绑到 `sealed` 会让它永远 False。

**A2（-shm 不是封存成员）**：`-shm` 每次被任何连接开关都会改内容，纳入封存会让同一备份第二次 verify 必失败。成员项带 `sealed: true/false`；`files` 仍列出它供还原参考。

**A3（coverage 第 6 个值）**：`coverage` 增加 `error`（账本读不出来时），`stopped_at` 增加 `ledger_error` 与 `max_files`。没有 `error` 时「账本读不出来」只能伪装成 page/budget 之一。

**A4（坏行 reason 词表）**：`reason` 仍用既有稳定词 `artifact_record_invalid`，细码放 `reason_detail`（`record_size_invalid` / `record_digest_invalid` / `record_page_invalid`），因为红基线断言的是 `reason`。

**A5（library_rows 与 empty）**：`library_rows` 只在首屏（`after is None`）统计；`coverage=empty` 只表示「本次一页都没核验」（空库或游标已在末尾），只有 `library_rows == 0` 才表示「库里没有 artifact」。

**A6（scan 文本格式）**：unknown 原因逐条打印 `code: count`；「库里没有已登记的成品可核验」只在 `library_rows == 0` 时出现。

**A7（WC-06 的表现）**：新增 `existing_pages` 与 `unstarted_pages`；删除旧文案「已登记 N 页」（它是 WC-06 的误读来源）。`unstarted_pages` 在当前 CHECK 约束下结构性恒为 0，仍按计算字段实现并保留恒等式 `total = linked + existing + unstarted`。

**A8（作废只摘 watcher）**：作用域作废时摘掉 pending 的 watcher，**不** cancel 生产者的 Future（取消不是顺序保证，且 UI 无权取消后端工作）；正确性只依赖 epoch + 批次双校验。
