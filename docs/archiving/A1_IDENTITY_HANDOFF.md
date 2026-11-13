# A1 候选→观察→内容代次交接设计

> 本页为前一执行批次的历史说明，保留当时已实现/运行中/未验证记录。当时源码为 705a990；2026-11-13 AR4 后当前产品为 dda6fc1/schema 5，最新记录见[维护记录](../maintenance/2026-11-13_jpgpng_a1_ar4_fix.md)。旧审计保留，不从下文旧“下一步”直接启动。

日期：2026-11-12。批次：`JPGPNG-A1-RECHECK-FIX-20261112-01`。对应复审问题 AR2-03/04/07，按[开发指导](../audits/2026-11-12-a1-recheck/DEVELOPMENT_GUIDE.md) F3 先定接口与状态表，再改代码。本文不是实现完成回执，也不是生产接线授权。

## 1. 结论先行

当前 `ScopeReceipt` 只摘要相对路径和大小，`ArchiveEntry.physical_identity` 又使用 `compare=False`；因此“枚举到的具体文件”与“实际读取并计算摘要的对象”没有可验证的交接。真实文件原地等长改写或 `os.replace` 同名替换时，现有流程也没有明确的代次解析器，只在遇到不可变事实冲突时返回数据库错误。

F3 必须先定义四个独立事实，再把它们串成一个可重放的解析记录：

| 身份 | 用途 | 不能替代什么 |
| --- | --- | --- |
| 原始相对路径 + 根映射版本 | 找到用户选择位置、恢复原拼写 | 不证明位置仍是原文件 |
| device/file_index + size + mtime | 绑定当次候选与实际读取对象 | 不保证跨时间永不复用，也不是来源收据 |
| asset_id/revision + size + SHA-256 | 不可变内容代次，供历史包引用 | 不等于当前路径始终属于该内容 |
| snapshot/run/check/解析记录 | 证明枚举消费、观察与代次解析的交接 | 不自动证明 Pixiv 原图或网盘持有 |

## 2. 候选与观察接口

新增纯数据边界（可放在 `archive_scope` 或独立小模块，不能引入 I/O）：

```python
@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    kind: str                    # project | external
    root_id: str                 # 注册时规范化的根 ID
    root_revision: int
    relative_path: str           # 保留用户原始 Unicode 拼写
    physical_identity: FileIdentity | None
    size_bytes: int
    mtime_ns: int
    snapshot_id: str
    run_id: str

@dataclass(frozen=True, slots=True)
class ObservedFile:
    candidate: CandidateSnapshot
    content: ContentVersion | None  # 读取并计算后的资产/代次/大小/摘要
    digest_complete: bool
    bytes_read: int
    observed_size: int
    observed_mtime_ns: int
    observed_identity: FileIdentity | None

class ResolutionKind(StrEnum):
    REUSE = "reuse"                       # 同路径同内容，复用
    NEW_REVISION = "new_revision"         # 同逻辑资产内容变化，新增代次
    REPLACEMENT = "replacement"           # 同路径不同物理对象，需明确意图
    BASELINE = "baseline"                 # 未知历史摘要，只建立当前基线
    CONFLICT = "conflict"
    NEEDS_CONFIRMATION = "needs_confirmation"
```

`CandidateSnapshot` 是枚举凭据的条目事实；`ObservedFile` 必须显式携带候选，不能只传 `AssetEntry`。观察器返回后要检查：

1. 观察对象的 device/file_index、size、mtime 与候选一致；
2. 读取期间文件未变化；
3. `content` 的 size 与实际读取一致；
4. 摘要完整时才允许与已知历史摘要比较。

不一致时返回 `CONFLICT` 或 `NEEDS_CONFIRMATION`，不得静默套用旧候选。

## 3. ScopeReceipt 升级

当前 receipt v1 的 `_entries_digest` 只包含 `[relative_path, size_bytes]`，即使 HMAC 为可选，也不能证明物理对象。建议升级为 receipt v2：

- 条目摘要字段：`kind`, `root_id`, `root_revision`, `relative_path`, `device`, `file_index`, `size_bytes`, `mtime_ns`。
- `selection_digest` 也纳入根注册版本、选择种类、子批边界和规范化后的选择描述。
- 旧 v1 receipt 在 v2 reader 中明确拒绝；当前版本计划不伪装兼容。
- 计划 `_scope_entries` 不再丢弃 `physical_identity`；`ScopeReceipt.matches_entries` 显式比较物理事实，不依赖 dataclass 相等。
- 计划头保存 receipt 的 v2 版本和字段；重载、重放、全库审计共用同一检查。

HMAC 仍可选；签名不是本轮强制项。摘要只能证明“数据未被无意改动”，不能证明写入者没有重算摘要。

## 4. 内容代次解析

### 4.1 解析顺序

对每个观察对象，按以下顺序处理：

1. 候选/观察物理事实不一致：`CONFLICT`。
2. 现有路径绑定不存在：建立 `BASELINE` 或首次 `REUSE`，取决于内容摘要是否已知。
3. 绑定存在且 `content_key` 相同、size/摘要相同：`REUSE`。
4. 绑定存在、逻辑资产相同、物理对象相同、内容变化：`NEW_REVISION`，revision 增加 1，旧内容/旧包不变。
5. 绑定存在、物理对象不同：`REPLACEMENT`，必须携带用户/操作意图；在未确认前 `NEEDS_CONFIRMATION`。
6. 绑定存在、内容基线未知：只建立新观察基线，不把旧未知摘要改成“已验证”。

### 4.2 数据模型选择

本轮优先选择“**同逻辑资产的新 revision**”，不直接删除 `archive_asset_path_owner` 触发器：

- 同一 `asset_id` 的 revision+1 可以在同一 `path_key` 上存在；现有触发器只阻止不同 asset_id 抢占路径。
- `archive_contents` 以 `(asset_id, revision)` 唯一，旧 revision 行保留。
- `bind_archive_path` 在事务内以旧 `content_key` 为 expected 条件更新到新 revision；旧计划仍指向旧 revision。
- 若物理替换意味着“不同作品/不同逻辑资产”，不能自动沿用 asset_id；返回 `NEEDS_CONFIRMATION` 或要求显式新资产 ID，不覆盖旧资产。

### 4.3 写入事务

解析器可以队外产生建议，但唯一写入事务内必须重新读取当前绑定并按 expected 旧 key 条件更新；条件失败返回结构化冲突，不自动重试覆盖新结果。保存失败仍整批回滚。

## 5. 路径键与根 ID

AR2-07 的修正是**按路径分段**处理 ASCII 大小写，而不是“整条路径只要含非 ASCII 就全部原样保留”：

```text
segment = segment.casefold() if segment.isascii() else segment
root_id = root_id.casefold()  # 根 ID 注册契约只允许 ASCII 标识
```

- ASCII `画/A.png` 与 `画/a.png` 得到同一 identity_key；
- NFC/NFD 的不同非 ASCII 拼写仍不同；
- `straße` 与 `strasse` 仍不同；
- 原始 `relative_path` 保留，身份键只用于比较，不用于显示/恢复。
- 生产 `PathRef.target_key` 不直接改成同一规则；本次只改 `PathRef.identity_key`，并增加“`target_key` 未变”的回归测试。若后续统一，必须单独回归下载主线。
- 2026-11-12 F3 实现明确授权修改 `core/task_contracts.py` 的 `identity_key` 属性和相应测试；这是归档专用属性，不属于下载主线的 `target_key` 契约。

## 6. 测试矩阵

| 场景 | 期望 |
| --- | --- |
| 首次真实文件枚举→观察→保存 | baseline/reuse，路径与摘要可重开 |
| 重复枚举同一文件 | reuse，无新 revision |
| 原地等长改写 | 新摘要/新 revision，旧计划仍读旧内容 |
| 大小变化 | 新 revision，容量重算 |
| os.replace 同名新文件 | needs_confirmation 或明确 replacement，不抛通用 DB 错误 |
| 路径别名重选 | 物理身份相同则复用；不同则冲突 |
| 根映射换代 | 旧 receipt 拒绝，新根重新枚举 |
| NFC/NFD 双文件 | 两路径保留 |
| 画/A vs 画/a | 归档 identity_key 一致；显示保持原拼写 |
| 物理身份被篡改 | receipt/plan digest 拒绝 |
| 旧计划重载 | 仍指旧 revision，不要求当前绑定 |
| 观察期间文件变化 | conflict，不产生计划 |

## 7. 本轮边界

- 本文只定义接口和状态；F3 代码尚未实现，不能把 AR2-03/04/07 标为已关闭。
- 不修改生产 `target_key`，不接生产库，不读取真实图库。
- 若选择“本轮不支持同名替换”，必须让调用者得到结构化、可重试的待确认结果，并明确范围未收全；不能以回滚异常冒充完整。
