# U1-A / J-01 W0：状态与事务决定

日期：2026-11-13。基线：`bb2cb7f`（产品代码 `dda6fc1`，schema 5）。修复分支：`fix/a1-u1a-j01-20261113`。本页先于产品代码落库，作为 W1–W4 的实现约束；W5/W6 仍待完成，不是生产发布声明。

## 1. 不可让步的事实

1. **历史交付事实**（artifact、delivery、批次计数、收据）只追加，不因后续文件变化而改写或删除。
2. **当前文件观察**必须来自本次核验；数据库里“曾经完成”不是“现在仍在”的证据。
3. **用户意图**单独保存；主动删除/转存不得被自动补下载覆盖。W4 用追加式 `events(kind='removal_intent')` 保存，不删除旧记录；只有 EXCLUDED/NONE 已接线，OFFLOADED 仍明确拒绝，不得对外声称已支持。
4. 任何复用结论都带“核验于某时”；过期、缺失、不可读、根不可用、读取中变化一律不作为 `completed/existing`。
5. 检查不得覆盖、删除或重建用户磁盘上的现有字节；缺失修复只写新临时文件并走现有 prepared/publish 协议。

## 2. 复用判定表

| 当前观察 | 新任务页面状态 | 是否派发 | 旧记录 |
| --- | --- | --- | --- |
| `hash_match`（完整 SHA-256、身份/大小/mtime 稳定） | `completed/existing=1` | 否 | 原样保留 |
| `missing` | `pending=1`，新交付代次 | 是，可修复 | 原样保留 |
| `changed` | `needs_verification=1` | 否，不自动覆盖 | 原样保留 |
| `unreadable` / `root_unavailable` / `changed_during_check` / `unknown` | `needs_verification=1` | 否 | 原样保留 |
| `quick_match` 且策略显式 `quick` | 仅按快速策略复用 | 否 | 标注“快速检查” |
| `quick_match` 但策略 `full` | `needs_verification=1` | 否 | 不升级为严格 |
| `missing` 且同路径存在用户 `EXCLUDED` 意图 | `cancelled=1` | 否 | 原样保留；撤销后才会重新待修 |

本轮只启用完整 SHA-256 策略；快速策略保留契约，不默认启用。

## 3. 根绑定

`LibraryRepository` 本身不拥有输出根，只有 `PathRef(kind/root_id/relative_path)`。根由 `PathResolver` 拥有，生产在 `PageDeliveryExecutor` 构造时注册：

```
PageDeliveryExecutor(repository, resolver, ...)
  -> repository.bind_path_resolver(resolver)
```

约束：

- 一个库只能绑定同一 `project_root`；external 根可以后续增加，但不能换成另一个项目根。
- 未绑定根、external 根未映射、路径越界/链接/重解析点时，不得猜测文件位置，直接 `needs_verification`。
- 根映射 revision 由 `PathResolver.root_revision(ref)` 提供；本轮根不可移动，revision 为 1，但接口保留，后续重映射必须递增。
- 没有根绑定就**不**回退到“数据库记录即复用”的旧行为；这是 fail-closed，不是弱策略升级。

## 4. 事务与 I/O 顺序

```
调用线程：
  accept_source_page(batch_id, page)
    -> 只读连接读取 batch/job/request 与候选 artifact（短快照）
    -> PathResolver + LocalObserver 逐 artifact 核验（打开普通文件、完整 SHA-256、前后身份）
    -> 生成紧凑 health 记录（artifact_id, state, checked_at, observed size/sha, root rev, policy）
    -> 将 health 冻结进 command payload
    -> enqueue

writer 线程：
  BEGIN IMMEDIATE
    -> 重新读取 batch/job/page/receipt
    -> 对 health 记录做 artifact 行、大小、摘要、page/output、root rev、策略、时间新鲜度校验
    -> 仅 hash_match（或显式 quick）允许 link 旧 artifact
    -> missing 插入新 pending 交付代次；changed/unknown 插入 needs_verification 交付代次
    -> 更新 batch item / counts / checkpoint / receipt
  COMMIT
```

禁止：在 `BEGIN IMMEDIATE` 内做目录探测、哈希、解码、网络、文件写入或删除。预检与写者之间仍有时间窗口，因此写者必须重新校验；不能把预检当成永久健康。

## 5. 三条复用路径的接入

| 路径 | 改动 |
| --- | --- |
| `DeliveryRepository._find_reusable_artifact` | 增加 `health_by_artifact` 输入；无当前健康记录不返回 artifact；保留候选查询供预检使用 |
| `LibraryRepository` 同 job completed existing | 同一健康判定；不健康时不再直接 link completed delivery |
| `LibraryRepository` trusted 跨任务 | 只 link 健康 artifact；否则创建修复/待核验代次 |
| `FollowRepository._candidate_requires_write` | 使用同一批 health 结果决定 counts_work/预算；不在事务内哈希 |
| `download_service` 注释 | 明确 legacy trusted skip 不是 U1-A 当前健康证据 |

## 6. 失败与恢复

- 预检异常：该 artifact 记为 `unknown`，页面进入 `needs_verification`，不阻断命令；不静默复用。
- 写者发现 health 过期/与 artifact 行不匹配：不 link，按 `needs_verification` 或 `pending`（仅 missing）处理。
- 修复时最终路径已存在：沿用 executor 的 `output_file_conflict`，保留现有字节，不覆盖；页面保持失败/待核验，不伪造 completed。
- 批次含 `needs_verification` 或未完成 pending 时，settle 不得为 `completed`。
- 同一批同一 artifact 只观察一次；跨会话不把内存缓存当严格证据。

## 7. 影响面（静态清点）

- `_find_reusable_artifact`：直接调用者只剩 `library_repository`（写者）；`follow_repository` 改用同一批 health 记录，不再走无健康查询。
- `_candidate_requires_write`：仅 `library_repository._accept_source_page` 调用。
- `accept_source_page`：公开 API 被 coordinator、follow collection、测试调用；新增可选 health payload 不改变返回类型。
- `PageDeliveryExecutor`：生产 desktop/GUI 与测试均经过；新增根注册是构造期副作用，不触网、不写文件。
- 不修改 archive schema 5、不新增库表、不改历史收据。

## 8. 本轮验收

- W0 独立红测试 7 红、2 skip 保持为基线。
- W1/W2/W3 后 7 红转绿，8 个守卫保持绿；新增同 job resume、follow 预算、无根绑定、过期预检、I/O 线程边界测试。
- W4 的 EXCLUDED/撤销已由独立用例覆盖；接受后取消 pending、同 job 撤销、结算后记录/重放也已覆盖；OFFLOADED 明确拒绝。
- 独立复核的 P1-1（resume/claim 回灌）、P1-2（needs_verification 输出槽）、P1-3（已规划 pending）、P1-4（同 job 撤销）已修复并补测；P2-7/P3-10/P3-12 仍列残余。
- 全量（不含 Tk）1,546 项、0 失败、0 错误、4 跳过；Tk 展示文件单独 4/4。
- W6 的 2,000 文件、第二批缺失修复、进程退出后重开子集已在 `3b63f9c` 上通过合成证据；复核后恢复路径改动需再跑同规模。根不可用/链接/读取中变化/预算耗尽的**集成接线**仍只有契约或单元层覆盖，W5/W6 待补。
- 不接真实图库/账号/网络；不执行上传、删除、释放。
