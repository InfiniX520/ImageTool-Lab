# U1-A 维护闭环契约 v6 修订 A1–A6（开发前冻结）

基线：`5f19553`（日常 `4ba891e` + 审计包 `audit/post-tidy-20261114`）。[契约 v6](U1A_MAINTENANCE_CLOSURE_CONTRACT_V6.md) 与[上一轮 WR 开发指导](../../audits/2026-11-14-u1a-w5w6-proof-independent/DEVELOPMENT_GUIDE.md) 继续有效；本文件只补 PA-01/PA-02/PA-03 与 WR-01..07 的强制验收条件，不新开产品路线，不重开已关闭编号，不授权真实库恢复、释放、联网或合 main。

## 0. 一句话

九项产品问题归并为三个根因加四个独立缺口：备份核验没有共同版本、对象集合定义错用 LIKE、清单语义不严；另加恢复步骤、扫描全程、外部根、面板分页与恢复身份。判据不是“测试更多”，而是每条编号都有行为证据。

## A1 单一稳定版本（PA-01）

**定义**：一次 `verify` 的四项事实（sealed / logical / health / compatible）必须来自**同一份文件组、同一代次**。三个独立只读连接不构成共同版本。

**取得方法**（唯一允许的实现）：

1. 先按 A3 严格校验清单，再解析成员路径，拒绝链接、junction 与越界目标；
2. 在备份目录**同级**建唯一私有临时目录 `<备份名>.verify-<uuid4hex>`（同卷）；建不出来即 `verify_workspace_unavailable`、`ok=False`、退出 3；
3. 逐成员**边复制边哈希**（1 MiB 分块）；打开后 `fstat`、复制后再 `fstat`、再对路径 `stat`，比对 `(st_dev, st_ino, st_size, st_mtime_ns)` 与路径身份；任一变化 → `changed_during_check`、`ok=False`、退出 2；
4. `file_group` 模式的主库/WAL/SHM 必须**成组复制、成组判定**；
5. logical 与 health 在副本上**共用一个只读连接、一个读事务**（`inspect_library(..., connection=)` 关键字扩展，旧签名行为不变）；
6. 副本用完即删，只删本次创建的文件；删不掉记 `verify_cleanup_failed` 警告并写路径，绝不删陌生文件；
7. 报告新增 `evidence`：`scope="isolated_copy"`、成员状态、`source_state_before/after`、`copy_bytes`、`seconds`、`checked_at`、`snapshot_id`。

**证明作用域（必须写进报告与帮助文字）**：本次核验描述的是那份隔离副本；返回之后源目录再被改写，不在本次保证内。要回答“源还能不能对上封存内容”，用 `source_equivalent`，它是一个独立问题，永不影响 `ok`。

**代价**：`verify` 现在需要一次完整复制（峰值磁盘占用 ≤ 备份目录大小，结束时释放）。这是换取共同版本的显式代价，不许用“文件没变就跳过复制”的启发式绕开。

**反例矩阵**（全部断言 `ok=False` 或 typed 错误，且断言被核验目录与源文件组未被写入）：复制中改行 → `changed_during_check`；复制后改行 → 本次仍描述副本、第二次核验 `mismatch`；同路径替换；等长改写；sealed 字节来自 A、logical 来自 B 的清单 → 必须在无注入时也 `ok=False`；WAL-only 提交；成员删/增；busy/取消/预算。

## A2 对象集合唯一定义（PA-02）

- 唯一判定 `_is_internal_object(name)`：字面量前缀 `sqlite_`，大小写不敏感（SQLite 的保留判定就是如此），**不用 LIKE**。
- `_object_rows`、`_objects_digest`、`_object_definitions`、`_normalized_objects` 与指纹遍历共用它，遍历表/视图/触发器/索引一致。
- `FINGERPRINT_VERSION` 保持 1：同一库 digest 不变，旧清单不会被无端判坏。
- 含 `sqlite_` 形近合法表的旧清单会得 `logical_mismatch`；正确处理是重新生成备份，**不得**回退旧漏检算法、删除该表或忽略 digest 差异求绿。
- 必须覆盖：`sqliteXuser`、`sqlite9user`、大小写别名、合法带引号名称、view/trigger/index、真实内部对象。

## A3 清单严格校验（WR-04）

在读取任何成员文件之前完成：`members` 为 dict 列表；`name` 唯一且大小写别名拒绝；`bytes` 为非负 `int`（`bool` 拒绝）；`sha256` 为 64 位小写十六进制；`copy_name` 必须属于 `sealed_members`；`sealed_members` 必须是成员名子集；`-shm` 永不被封存；`mode` 只能是 `online`/`file_group` 且按模式校验 WAL/SHM 规则；`logical_fingerprint` 为 dict、`version==1`、`digest` 合法、`tables` 为 dict。

任何不合规 → `manifest_*` typed 错误、退出 3。`ValueError`/`TypeError` 冒到 CLI 视为缺陷。

## A4 恢复协议（WR-01）

`_restore_steps` 与 runbook 同步改成安全的七步：

1. 关闭主程序，并**验证**没有剩余进程/连接；
2. `verify <备份目录>` 并记录模式与成员；
3. 在暂存目录准备**匹配**的文件组并单独校验；
4. 当前主库 + WAL + SHM **整组移入**同卷保留目录（移动，不删除、不覆盖）；
5. 放入匹配组：`online` 自包含、无 WAL；`file_group` 整组，禁止新主库配旧 WAL；
6. 重开做 `inspect` 与 `compare` 逻辑对照；
7. 失败整组退回保留目录。

合成演练必须覆盖：WAL 残留、两种模式、连接未关被检出、恢复后逻辑指纹相等、保留目录内容精确、非法组合被拒。真实库恢复仍不做。

## A5 扫描全程与外部根（WR-02/WR-03）

- 新建 `core/library_scan.py`：`scan_library_report(source, *, project_root=None, after=None, limit=200, budget=None, cancel=None, kind="full_walk"|"continuation")` → `coverage / scope / pages_examined / library_rows / stats / findings / unknown_reasons / next_cursor / resume / exit_code`。
- 引擎 `scan_library_artifacts` 的 API 与逐页语义一字不改，聚合层只在外层循环。
- 默认 `maintain scan` 走全程；显式 `--page` 才走单页；`--limit` 是页大小，不是全库总量。
- 只有到 EOF、未被预算/取消/错误停止才是 `complete`；中途任一页确定异常必须进入最终退出码，末页不得覆盖。
- 裸 `--after`：`scope=continuation`、`coverage=partial`、退出 5，即使已在 EOF。
- 根映射只读加载 `config/output_roots_v1.json`：不存在 = 无外部根；存在却读不了/JSON 坏/版本未知/根非法 → `mapping_unreadable`、退出 3；不猜根、不 mkdir、未知 id 不回退项目根。`--root` 只指项目根。

## A6 归档采集器身份（PA-03）

- 枚举仍可用 `DirEntry`，身份必须来自可靠 `os.stat` 或打开后 `fstat`（卷身份、文件 ID、链接数）；零值或取不到显式写 `identity: "unknown"`，禁止零等于零判通过。
- 哈希前后校验同一已打开文件，保留路径绑定与重解析点拒绝。
- 只能关闭为“**旧证明更正 + 新采集器已验证**”：不重跑旧搬迁、不重建旧链接、不改旧清单，也不宣称历史身份基线已补回。

## 7. 退出码

| 码 | 含义（沿用 v6 §3.3） |
| --- | --- |
| 0 | 全程完整、确定异常为 0（legacy unknown 仅 `--allow-unknown` 放行） |
| 2 | 任一被访问页有 missing/changed/unreadable/root_unavailable/changed_during_check；备份分项 mismatch |
| 3 | 账本/映射/清单不可读或无法安全继续 |
| 4 | 参数拒绝 |
| 5 | 覆盖未完成、取消、预算停止、存在不可核验行或只完成一页 |

## 8. 验收矩阵

1. 审计 9 条反例（7 红/2 绿基线已复现，见 `.runtime/jpgpng_post_tidy_fix_20261114_01/red_baseline/red.xml`）逐条转绿，原样保留在 `docs/audits/**` 不改；
2. 原 WR/WC/RFB/RFA/RF/UA 回归不下降；
3. 主体全量（排除两个 Tk 展示文件）+ 两个 Tk 文件各自独立进程；
4. 只读 fork 对照 v6 + 本文件逐项审 `git diff`，问题退回原 lane；
5. 文档只保留一个接续入口，机器计划与 daily HEAD 一致，历史记录不改写。

## 9. 明确不改变

五份冻结协议、库 schema `"1"`、无新依赖、面板风格与无缩略图策略、`_VERIFY_BATCH_LIMIT`、bucket/tone 语义、`compare_library.ok` 不把 `sealed`/`source_equivalent` 算进结论（A1 之前的行为显式保留），真实图库与 `record_backups` 内容不动。
