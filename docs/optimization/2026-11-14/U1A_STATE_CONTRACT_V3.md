# U1-A（RFA 轮）状态契约 v3

日期：2026-11-14。前序：[v2](../2026-11-13/U1A_STATE_CONTRACT_V2.md) 仍然有效；本文件只**修订**其中三处，并冻结本轮三份接口事实。审计：[2026-11-13-u1a-rf-recheck](../../audits/2026-11-13-u1a-rf-recheck/FINDINGS.md)（RFA-01..07）。

## 0. 基线与范围

| 项 | 值 |
| --- | --- |
| 审计基线提交 | `26afa48`（产品 `b07c2ed`，S0 契约 `b1a4f1d`） |
| 树 | `45f2115ec7ce1ccbcaf3f4837dfa2d23c321470b` |
| 工作树 | `.runtime/jpgpng_u1a_rfa_fix_20261114_01/integration_worktree` |
| 分支 | `fix/a1-u1a-rfa-20261114` |
| 解释器 | `D:\Miniconda3\envs\JuiLiu\python.exe`（CPython 3.12.14），`PYTHONNOUSERSITE=1`，`-s -B -X utf8` |
| 红基线证据 | `.runtime/jpgpng_u1a_rfa_fix_20261114_01/red01/`（10 红 / 2 绿） |

本轮**不新增表、不改库 schema、不改 `_SCHEMA_VERSION = "1"`**，不接 UI，不动真实图库/账号/网络。全部验收用合成 SQLite 与合成 PNG。

三份冻结接口：

* **R-A 可续观察**（RFA-01）：替代"整页硬预算"，修订 v2 §3.4 的预算含义。
* **R-B 在场证据与清理**（RFA-02/03/06/07）：修订 v2 §1.2、§1.3、§4。
* **R-C 后继解析**（RFA-04/05）：收敛 v2 §2.1 的权威顺序。

## 1. R-A：可续观察（RFA-01）

### 1.1 预算语义

`PageBudget(max_files, max_bytes, max_seconds, max_slices)` 是**一次观察切片**的预算，不是来源页的永久上限。默认 `PageBudget()`：`max_files=32`、`max_slices=8`。

* 切片内最多开 `max_files` 个文件；缓存命中不计入。
* 一次 `accept_source_page` 最多推进 `max_slices` 片；用尽时先持久化进度，再抛 `PreflightCancelled("page_slice_limit")`。
* 暂停/取消在任何两片之间生效，先持久化进度，再抛 `PreflightCancelled("cancelled")`。
* `page_file_budget` / `page_byte_budget` / `page_time_budget` 只保留给"单个 page_spec 自身超出预算"，即无任何合法推进的情况。

### 1.2 进度事实

键 `ObservationKey`：`job_id + source_id + pass_id + entity_id + page_identity + page_digest`。**不含 `batch_id`**：resume 会新建 batch，进度必须跨批存活。

事件：`kind = "page_observation_progress"`，载荷：

```json
{
  "schema_version": 1,
  "page_key": {"job_id": "...", "source_id": "...", "pass_id": "...", "entity_id": "...", "page_identity": "...", "page_digest": "..."},
  "records": [{"artifact_id": "...", "output_key": "...", "quality_key": "...", "root_revision": 1,
               "health": {"...": "ArtifactHealth payload"}}],
  "observed_files": 32, "observed_bytes": 0, "slice_seq": 1,
  "root_revisions": {"project": 1}, "policy_version": 1
}
```

* 只追加**本次新增**的记录；同一 `artifact_id` 以最新事件为准。
* 读取按 `event_seq DESC` 取该 `job_id` 最近 200 条同 kind 事件，Python 侧按 `page_key` 过滤，最多重放 `_MAX_PROGRESS_RECORDS = 4096` 条，超限丢弃（fail closed）。
* 写入走新命令 `record_page_observation`（`LibraryRepository.record_page_observation(job_id, key, records, resources)` → writer → `_insert_event`）。写入失败不会让受理失败：无进度只是变慢，不是错误。

### 1.3 复用规则（唯一判据）

一条进度记录可以当作"本次观察结果"复用，当且仅当全部成立：

1. `artifact_id` 仍存在于本次快照的候选行里，且 `sha256/size/output_key` 与行一致；
2. `quality_key` 等于本次请求的 quality key；
3. `root_revision` 等于当前 `PathResolver.root_revision(ref)`；
4. `health.policy` 与当前 `_ARTIFACT_HEALTH_POLICY` 一致，且 `_health_is_current(record)` 为真（`max_age_seconds=900`）；
5. 记录不是 `unobserved_health`（`UNKNOWN/UNREADABLE/ROOT_UNAVAILABLE` 一律重新观察）。

不满足任一条 → 丢弃该记录并重新观察。**禁止**把"未观察"写成健康，也禁止把过期观察当成当前证据。

### 1.4 切面算法

```text
plan = 读快照（DB 只读，一次）
remaining = 按 page_spec 顺序的候选
slices = 0
while remaining and slices < budget.max_slices:
    cached, fresh = 用 1.3 规则切出本片（fresh 数量 <= max_files）
    if fresh 为空 and cached 覆盖 remaining: 提交整页
    if fresh 为空 and 仍有未覆盖: 失败（不可推进，明确 reason）
    观察 fresh（每片内检查 cancel，先于打开下一个文件）
    持久化 cached+观察结果为本片进度
    slices += 1
提交整页：一次性 writer 事务写 deliveries/links/receipt/checkpoint/event
```

* 整页提交前**不前移 cursor、不写 receipt**，因此"部分工作"永远不会被误认为已提交。
* 已提交的页再次到达时，走既有 `_accepted_page_receipt` 零哈希重放，不读进度。

### 1.5 控制结果映射（不变）

`PreflightCancelled("cancelled")` → `_Stopped`；`page_*` → `_Budget` → 源以 `source_budget` 停止。不得变成 `source_runner_failed`。

### 1.6 不变量

1. cursor 只随整页提交前进；部分切片不留"已提交"痕迹。
2. 每条被复用的健康结论都有 `artifact_id` 级别的证据绑定与时效。
3. 同一页的重复受理在切片数上限内必然前进；不会无限重读同一批文件。
4. 文件 I/O 永远在写事务之外；写者只做 CAS 与行内校验。

## 2. R-B：在场证据与清理（RFA-02/03/06/07）

### 2.1 在场四态

新模块 `core/file_presence.py`：

```python
class Presence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNREADABLE = "unreadable"
    ROOT_UNAVAILABLE = "root_unavailable"

@dataclass(frozen=True, slots=True)
class PresenceResult:
    state: Presence
    ref: PathRef
    detail: str      # "ok" / "missing_bound" / "unbound" / "permission_denied" / "io_error" / "stat_failed"

def probe_ref(resolver: PathResolver, ref: PathRef) -> PresenceResult: ...
```

实现：先 `resolver.root_state(ref)`；`UNBOUND/MISSING_BOUND` → `ROOT_UNAVAILABLE`（detail 区分）；`AVAILABLE` 才 `os.stat`，`ENOENT` → `ABSENT`，其他 `OSError` → `UNREADABLE`，成功 → `PRESENT`。**绝不 `mkdir`**，绝不把异常吞成"不存在"。

### 2.2 取消矩阵（`_abort_unpublished`）

| final | temp | 结果 |
| --- | --- | --- |
| PRESENT | — | `recovery_needed/published_before_abort`（绝不取消，绝不删） |
| ABSENT | PRESENT（身份匹配） | CAS 取消；CAS 之后**重新核验身份**再删 |
| ABSENT | ABSENT | CAS 取消，事件记 `temp_absent` |
| 任一 `UNREADABLE`/`ROOT_UNAVAILABLE` | | `recovery_needed`（`root_unavailable`/`temp_unreadable`），不取消、不记 `already_absent` |

`intent_unreadable` 仍按 v2 保持 `recovery_needed`。CAS 必须带 `expected_state`、`expected_prepared_sha256`、`expected_generation`（见 2.7）。

### 2.3 清理事实：生产者 = 消费者

* 写者 `mark_cleanup_completed` 从行内重建 `CleanupFact`，调用 `cleanup_event_payload()` 写全字段：`delivery_id/batch_id/outcome/error_code/temp_sha256/temp_size/cleanup_completed`；并接受调用方传入的 `expected_sha256/expected_size`，与行内 prepared 证据不一致时拒绝（不写终态）。
* 读取端认两种形状：`cleanup_completed == true`，或旧版 `outcome == "cleaned"` 且无该字段。`outcome == "failed"` 不是终态，必须重试。
* 终态事实必须绑定 prepared 身份（`temp_sha256`+`temp_size`），`outcome` 字符串本身不是证据。

### 2.4 候选查询与队列推进

`list_cleanup_candidates(limit=200, *, after_rowid=0)`：

* SQL 侧排除终态清理事实（同时覆盖新布尔与旧 `outcome=cleaned`），`d.state='cancelled'`、`prepared_json IS NOT NULL`；
* `WHERE d.rowid > after_rowid ORDER BY d.rowid LIMIT ?`，单次最多检查 `min(limit*5, 2000)` 行，凑满 `limit` 个可解析事实即返回；
* 坏 `prepared_json` 只跳过该行，不缩小后续窗口；
* `recover_local` 持有内存游标做公平轮转（用完回到 0），重启从 0 开始；不新增列、不加索引。

### 2.5 删除只有一条实现

`_discard_owned_temp(temp, evidence, *, recheck_after_db=False)`：`probe_ref` → `_verify_owned`（size+`file_id`+sha256）→（若发生过 DB 往返）再 `probe_ref` + `_verify_owned` → `unlink`。任何一步不匹配：保留文件，写诊断（`unowned_staged_temp` / `temp_replaced_after_observation`），返回"未删除"。

取消路径、清理路径、确认后清理都走这一条；禁止独立 `unlink` 快捷分支，禁止按扩展名或目录扫描判断所有权。

严格 TOCTOU（另一进程在 `unlink` 瞬间替换）本轮**不宣称**已原子解决；需要文件句柄/独占语义时另立 Windows 实测，未证明前保持"宁可不删"。

### 2.6 恢复补偿

`recover_local` 每行：

```text
try: 主操作
except _StorageFault: 结果 recovery_needed（行仍可恢复），继续下一行
except (_Fault, OSError, ContractError):
    分类 = _settle_local_failure(row)   # 自身绝不抛竞争异常
    结果 = 分类（completed / cancelled / recovery_needed / transferred）
```

`_settle_local_failure`：重读当前行；`completed` → 报告 completed；`cancelled` → cancelled；已是目标状态 → 直接报告；否则 `set_delivery_issue(expected_state=...)`，其 CAS 失败时再重读一次并按赢家状态收敛。真正"数据库不可读"才上抛 storage/trust fault，不得吞掉后谎称"仍可恢复"。

### 2.7 已冻结的 CAS 原语（S0 已落地）

```python
LibraryRepository.cancel_unpublished_delivery(
    delivery_id, batch_id, *, error_code,
    expected_state="prepared",
    expected_prepared_sha256: str | None = None,
    expected_generation: int | None = None,
) -> None
```

* 写者对 `expected_prepared_sha256` 校验行内 `prepared_json.sha256`，对 `expected_generation` 校验行 `generation`；任一不符抛 `InvalidStateError`，行保持原状态。
* 两个参数都可省略（兼容既有调用）；省略即退回 v2 语义。
* 执行者必须在拿到"身份已核验"的结论之后传参；不得在未核验时凭默认值取消。

## 3. R-C：后继解析（RFA-04/05）

### 3.1 权威顺序（唯一）

1. **精确 handover**：本交付的 `recovery_owner_transfer` 事件（`event_seq` 新→旧，最多 8 条），取第一条**可用**目标：artifact 存在、`illust/page_index/output_key/quality_key` 全部匹配、其 delivery 为 `completed`。
2. **窗口内 completed**：一次读出 `rowid > anchor` 的同页同质量行（`ORDER BY rowid DESC LIMIT 64`），最新的 `completed`（带 artifact）胜出。
3. **窗口内活动后继**：最新 `ACTIVE_FOLLOWUP`。
4. **有界 superseded 链**：只在本窗口内按 `rowid` 迭代，`visited` 去重，节点上限 `MAX_SUPERSEDE_NODES = 64`。

事件优先是**契约要求**：`recovery_owner_transfer` 指定的 artifact 可能早于被取代行，`rowid` 无法表达它。若事件存在但目标不可用（缺失/损坏/质量不符/非 completed），返回 `reason="event_target_unusable"` 与 `successor=None`：拒绝，**不静默回退**到 rowid 结果。

### 3.2 返回值

```python
@dataclass(frozen=True, slots=True)
class LineageResult:
    successor: Successor | None
    reason: str            # "event" / "rowid_completed" / "rowid_active" / "supersede_chain" /
                           # "none" / "event_target_unusable" / "budget_exhausted" / "cycle"
    diagnostics: tuple[str, ...]
    queries: int
    nodes: int

def resolve_successor(conn, delivery, *, hops: int = 0) -> Successor | None      # 兼容
def resolve_successor_ex(conn, delivery) -> LineageResult                        # 新入口
```

`resolve_successor` 实现为 `resolve_successor_ex(...).successor`，二者共用同一次遍历；三个调用点（`_read_recovery_page`、`_apply_recovery_successor`、`_resolve_carried_successor`）不得各自实现优先级。

### 3.3 预算与缓存

* 单次解析最多 `MAX_LINEAGE_QUERIES = 4` 条 SQL、`MAX_SUPERSEDE_NODES = 64` 个节点；超限返回 `budget_exhausted`（视为无可用后继，不猜）。
* `visited` 按 `delivery_id` 去重；memo 只在**本次调用**内有效，不得跨事务缓存可变结果。
* 因"深度不足"得到的 `None` 不得写进全局 memo。
* 性能验收以**查询数与唯一扫描数**为主，不以本机秒数为跨机器阈值。

## 4. 验收映射

| 项 | 判据 |
| --- | --- |
| RFA-01 | `test_default_page_budget_can_progress_a_33_image_work_after_resume` 绿；32/33/64 页、多作品共页、多版本、暂停在边界、重复受理、切片间替换、过期进度各有反例 |
| RFA-02 | 三条根掉线探针绿；重连后已发布交付回到 `completed`；`cleanup` 掉线返回 False |
| RFA-03 | 真实 executor 写的事实让候选出队；旧 `outcome=cleaned` 也出队；201 条混合窗口推进；坏证据不饿死 |
| RFA-04 | 精确 handover 优先；坏目标拒绝并留诊断 |
| RFA-05 | 同节点只展开一次；8/16/24/32/64 条历史的 SQL 数与耗时；预算耗尽有明确 reason |
| RFA-06 | 往返后替换的文件保留；替换在核验前也保留 |
| RFA-07 | 两次 `recover_local` 交错返回 `completed`；补偿 CAS 失败与补偿写失败都不中止循环 |

全量：非 Tk 1,721 项基线 + 本轮新增，0 失败 0 错误；Tk 分文件 9 项；4 个既有跳过保持；旧 15 + 旧 14 反例保持绿。

## 5. 明确不做

* 不新增表/列、不做迁移；若将来需要持久化更多事实，另立迁移门禁。
* 不做 `finish_source` 的"最终结果查询"协议（已登记遗留）；本轮只保证切片状态与 `Future` 语义不冲突。
* 不接 W5 UI/CLI，不做真实外置卷、备份或云端回读演练。
* 不删除、不重算既有 artifacts/completed/旧 batch links。
