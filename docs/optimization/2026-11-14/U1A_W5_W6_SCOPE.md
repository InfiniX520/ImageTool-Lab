# U1-A / W5-W6 交付批次范围（2026-11-14 夜）

状态：本轮开工前的对齐记录。基线 `482eb03`（分支 `fix/a1-u1a-rfb-20261114`）。本页先于产品代码落库，作为 W5/W6 批次的实现约束。

## 1. 本轮目标

把 U1-A 从"离线状态机已关闭"推进到"用户可见、可操作、可安全切换"。三个交付面：

1. **库预检与备份（W6）**：一条命令回答"新代码能不能安全打开这个库"，并在任何切换前产出可验证备份与回退说明。
2. **待核验页面列表与恢复（W5）**：仓库层只读列表 + 桌面端口 + 白蓝界面面板，把已合入的 `recover_verification_page` 真正接到用户手上。
3. **全库巡检（W5 第三项）**：只读观察既有 artifact 与磁盘当前字节，报告漂移；**只报告，不改状态**（"核验"不等于"授权补下载"）。

## 2. 不变量（全批次）

- 真实库 `JpgPng/download_logs/library.sqlite3`、图库 `downl_Pixiv`、账号、Cookie、网络一律不动；所有演练在 `.runtime` 下的副本上进行。
- 库 schema 保持 `"1"`；不新增表/列/索引；不新增依赖。
- 不改日常启动入口（`启动.bat`/`main.py` 行为不变），不改现有页面语义。
- 写入路径一律走既有短事务与 CAS；界面线程不做 SQLite、不做文件 I/O、不做哈希。
- 既有契约 v4 的状态机不变：本批只加读取端口和用户入口，不新增状态转移。

## 3. 已确认事实（本轮实测，只读）

| 事实 | 证据 |
| --- | --- |
| 真实库与新代码 DDL **逐条相同** | 49 个对象，缺失 0 / 多余 0 / SQL 文本差异 0；`_REQUIRED_TABLES` 全在 |
| 真实库当前健康 | `quick_check=ok`、`integrity_check=ok`、`foreign_key_check` 0 条；WAL 0 字节 |
| 真实库内容 | 1 个 job（`paused`，2026-10-24，user_pause）、1 个 batch（`paused`）、540 batch_items、908 deliveries（897 `suspended` + 11 `completed`）、908 batch_item_pages、11 artifacts、700 events |
| 待核验页现状 | **0 页**处于 `needs_verification`/`recovery_needed`（所以面板需要"停住/待续"也可见，否则空表） |
| 结论 | **不需要 schema 迁移**；W6 的交付物是"预检 + 备份 + 演练 + 回退说明"，不是改写库 |

## 4. 工作包与文件归属（同一文件不并行）

| 包 | 内容 | 独占文件 |
| --- | --- | --- |
| WP1 | 库预检 / 备份 / 副本校验 / 切换计划 | 新 `core/library_maintenance.py`、新 `maintain.py`、新 `tests/test_u1a_library_maintenance.py` |
| WP2 | 待核验页面只读列表与摘要 | `core/delivery_repository.py`、新 `tests/test_u1a_verification_list.py` |
| WP3 | 全库巡检（只读报告） | 新 `core/library_audit.py`、新 `tests/test_u1a_library_audit.py` |
| WP4 | W5 桌面端口 + 白蓝界面面板 | `core/collection_desktop.py`、`gui/collection_view.py`、`gui/collection_controller.py`、`gui/collection_app.py`、新 `gui/verification_panel.py`、新 `tests/test_collection_verification_panel.py` |
| WP5 | 合流验收、文档、标签、推送、晨间报告 | `docs/**`、`TASK_PLAN.json`、`LOG.json` |

WP3 只交付模块与测试；CLI 子命令由主线程在 WP1 的 `maintain.py` 上接线，避免与 WP1 抢同一文件。

## 5. 冻结接口

### 5.1 WP1 `core/library_maintenance.py`

```python
inspect_library(path) -> dict
# status: missing | empty | compatible | incompatible | unreadable
# format, schema_version, objects_declared, objects_actual,
# missing_objects[], extra_objects[], mismatched_objects[],
# quick_check, integrity_check, foreign_key_violations, counts{},
# size_bytes, wal_bytes, shm_bytes, mtime
backup_library(path, destination_dir, *, label=None) -> dict
# 复制 main+wal+shm 为一组，不写源库；目标目录唯一，已存在则拒绝覆盖；
# 复制后只读复验（对象集合 + 计数 + quick_check），返回 manifest（含每文件 sha256）
verify_library_copy(path, copy_path) -> dict
# 对比声明与实际对象、逐表计数、quick_check；返回 {"ok": bool, "reasons": [...]}
switch_plan(path) -> dict
# 只读：{"compatible": bool, "steps": [str], "blockers": [str]}
```

`maintain.py` 子命令：`inspect`、`backup`、`verify`、`plan`；全部支持 `--json`；退出码 0 正常、2 不兼容、3 不可读；不联网、不建目录（除 backup 的显式目标）。

### 5.2 WP2 `core/delivery_repository.py`

```python
list_verification_pages(self, *, batch_id=None, states=None, after=None, limit=50) -> dict
# {"rows": [{"batch_id","job_id","illust_id","page_index","delivery_id",
#            "state","error_code","generation","updated_at","output_key",
#            "recoverable": bool, "actions": ["verify"]|[]}],
#  "next_cursor": tuple|None, "truncated": bool, "stats": {...}}
# states 默认 ("needs_verification","recovery_needed")；
# 允许的集合固定为 needs_verification / recovery_needed / suspended / pending / retry_wait
# 只读；不读盘、不算哈希；limit<=200；cursor 稳定（(batch_id, illust_id, page_index, delivery_id)）
verification_summary(self, *, batch_id=None) -> dict
# {"batch_id":..., "phase":..., "counts": {state: n}, "recoverable": n, "total_pages": n}
```

`recoverable` 由既有纯函数 `core.delivery_recovery` 的转移表推导（`needs_verification`/`pending` 可恢复，其余只显示）。

### 5.3 WP4 桌面端口（`core/collection_desktop.py`）

```python
DesktopRuntime.verification_pages(*, batch_id=None, states=None, after=None, limit=50)  # 查询线程
DesktopRuntime.verification_summary(*, batch_id=None)                                    # 查询线程
DesktopSession.recover_verification_page(batch_id, illust_id, page_index, *, command_id=None)  # 唯一 IO 线程
```

`gui/collection_view.py` 只增加：`verification_slot` 属性与 `set_verification_rows(rows, *, page_text='')`、`set_verification_result(text, tone='muted')`。
`gui/collection_controller.py` 只增加 action：`verification.list`、`verification.recover`。
面板文件 `gui/verification_panel.py` 自带按钮与表格，只通过 `on_action` 回传。

## 6. 验收标准

1. 新测试全绿：WP1/WP2/WP3/WP4 各自专项；每条新增行为至少一条反例（失败路径）。
2. 非 Tk 全量 `pytest`（排除 `test_collection_presentation.py`）0 失败 0 错误；Tk 文件逐文件/逐条隔离运行。
3. 真实库副本演练：`inspect`（compatible）→ `backup`（含 WAL 组与 sha256）→ `verify`（ok）→ 新代码打开副本 → 列表/摘要/巡检读数与只读 SQL 一致 → 回退说明可用。
4. 只读审计：另一只读 fork 对照本页逐条核对 `git diff`，0 P1、P2 修完或登记为边界。
5. 证据与记录：`.runtime/jpgpng_u1a_w5_w6_20261114_01/` 保留原始 XML/JSON；维护记录 + IMPLEMENTATION §4L + README/TASK_PLAN/LOG 更新；tag 与推送。

## 7. 不做（本批边界，写进记录）

- 不改真实库、不迁移 schema、不切日常入口；切换由用户一条命令决定。
- 不做"授权补下载"的批量入口；不把 `suspended`（批次停住）当成事故。
- 不做 quick_match 快速核验；仍只用完整 SHA-256。
- 不做跨会话观察缓存；每次核验重新观察。
- `config/collection_v2.json` 不新增字段（旧版本读取会因 `settings_unknown_fields` 拒绝）；每页字节上限仍是模块常量，配置入口留待 settings v2 迁移。

## 8. 并发与回退

- 四条 lane 并行，文件归属见第 4 节；任何跨包需求走主线程改冻结接口，不私自改对方的文件。
- 每个 lane 在自己 worktree 分支上提交；主线程按 WP1→WP2→WP3→WP4 顺序合流，每合一次跑一遍相关专项。
- 回退：任一 lane 的提交都可单独 revert；`maintain.py backup` 产出的备份目录不随 git 变化，回退库用副本替换，不动原件。
