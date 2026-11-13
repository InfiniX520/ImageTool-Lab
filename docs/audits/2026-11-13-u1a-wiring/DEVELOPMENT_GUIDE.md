# 下一轮：U1-A / J-01 生产接线开发指导

日期：2026-11-13；从 `4023fca`（代码 `dda6fc1`）接续。先读[问题明细](FINDINGS.md)与[独立红测试](https://github.com/InfiniX520/ImageTool-Lab/blob/audit/u1a-j01-tests-20261113/tests/test_u1a_j01_health.py)。本轮不是生产发布，不接触真实图库/账号/网络。

## W0：冻结与失败基线

1. 把 `audit/u1a-j01-tests-20261113` 的 7 个红测试合入新修复分支；保留 2 个明确 skip，不改成 xfail。
2. 复核基线 `4023fca` 的 7 红/2 skip 与全量 1,515/7/5；记录 XML。
3. 先写一页状态/事务决定，再改产品；不要先加 `os.path.exists()` 的浅修。
4. 独立测试的 H1–H7 是硬条件：completed=0、existing=0、total=1、pending+needs_verification=1、缺失场景 pending=1 且可派发、批次不得 completed、磁盘字节不被覆盖。

## W1：artifact 健康契约

建议新增（名称可调整）`ArtifactExpectation` / `ArtifactHealth` / `IntegrityPolicy`：

- 期望：artifact_id、PathRef、root revision、expected size、expected sha256、original/quality、policy version。
- 观察：文件身份、实际 size、mtime、完整 SHA-256、算法、检查时间、root 状态、原因码。
- 状态：`hash_match`、`quick_match`、`missing`、`changed`、`unreadable`、`root_unavailable`、`changed_during_check`、`unknown`。
- 完整策略默认校验路径边界、普通文件、身份、大小、完整 SHA-256；快速策略只做显式选用的弱检查，不能自动升级为严格。
- 历史交付事实只追加，不覆盖旧 artifact/delivery；当前观察单独追加；用户意图单独保存。
- 单批每 artifact/文件代次只核验一次；有文件数/读盘字节/时间预算；预算耗尽为 `needs_verification`，不静默弱化。

## W2：I/O 与事务边界

1. 在进入 `_accept_source_page` 的写事务**之前**做预检：只读查询候选 artifact/期望，交给文件 I/O 所有者逐页观察。
2. 把小型观察结果（artifact_id、identity、size、mtime、sha256、root revision、policy、checked_at、state）冻结进命令 payload。
3. 写者短事务内按 artifact/delivery 代次、root revision、policy 版本比较后提交；不接受过期观察覆盖新状态。
4. 不在 `BEGIN IMMEDIATE` 内做目录扫描、哈希、网络或解码；不能先检查再随意重开。
5. 检查前后身份/大小/mtime 变化标 `changed_during_check`；根不可用标 `root_unavailable`，不能把整根当丢失。
6. 预检与使用之间有竞争窗口；结论必须带“核验于某时”，不能声称永久健康。

## W3：接入三条复用路径

- `DeliveryRepository._find_reusable_artifact` 增加“已验证观察”输入；没有当前 `hash_match`（或显式 quick policy）时不得返回可复用 artifact。
- `LibraryRepository` 同 job completed existing 分支必须走同一健康判定；不能只查 completed delivery。
- `FollowRepository._candidate_requires_write` 用同一预检结果决定 `counts_work`/预算；不要在事务内哈希。
- `download_service` 的 trusted 注释必须与实现一致：外层没有健康证据时不得声称可信跳过。
- 归档转存/离线接受模块继续保持未接线；不要用 A1 的 acceptance 代替 artifact 当前健康。

## W4：缺失/变化与修复

- 缺失/变化/不可读默认不 completed、不自动联网；进入 `needs_verification` 或显式修复候选。
- 用户选择“补齐缺失”后才生成范围明确的修复交付；修复使用现有下载/验证/prepared/发布协议，生成新交付代次，不覆盖旧 artifact/delivery/收据。
- 路径被其他程序重建、同名文件已变化、根重映射时转冲突；不覆盖现有字节。
- `RemovalIntent.EXCLUDED`（有意移除）必须抑制自动补下载；旧记录保留，可撤销；不能把一次手工删除变成永久自动重下。
- 同一作品在同批不重复占修复名额；额度不足保持待修。

## W5：UI/CLI 与恢复

- 显示“核验于某时”、完整/快速、状态与原因；提供“重新核验/补齐缺失/保留现状/定位新目录/标记有意移除”。
- 恢复旧任务时必须说明原策略；旧策略结果不得自动重标为严格核验。
- 全库巡检独立、可暂停、与下载批次预算分开；不要在每个页面回调里做哈希。
- 默认只检查，不自动联网；用户显式选择修复才请求网络。

## W6：验收与迁移

至少覆盖：

- B 的 7 红全部转绿；8 个守卫保持绿（未改动复用、redownload、旧记录不变、EXCLUDED 决策）。
- 删除/等长非图/同尺寸换摘要/同尺寸真 PNG 四种；缺失修复后字节恢复、旧 artifact 保留、new_completed=1/existing=0。
- 根不可用、符号链接/重解析点、读取中变化、快速策略不升级、单批只核验一次、预算耗尽。
- 2,000 合成文件整链 + 第二批变化 + 进程退出；旧计划/旧收据逐字不变。
- 不接真实图库；真实迁移、上传/删除/释放、云端回读另批。

DDL/迁移：artifact 观察表或投影须在 W1 定稿后单独评审版本与索引；不要与 archive schema 5 混为一个版本，不要在同一个 schema 号上偷换对象。旧库升级必须先备份、副本演练、崩溃/回退测试。
