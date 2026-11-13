# JpgPng 文档导航

**当前接续（2026-11-14）**：`fix/a1-u1a-w5-w6-20261114` 在 RFB 修复轮之后交付 U1-A 的 W5/W6：待核验页面只读列表、全库巡检（只报告）、白蓝界面「核验」面板、库预检/备份/副本校验 CLI（`maintain.py`）。开工实测真实库与新代码声明**逐条相同**（49 对象、quick/integrity ok、WAL 0 字节），因此**不需要 schema 迁移**；`inspect` 前后主库字节与 sha256 不变，`scan` 11 个 artifact 全部 `match`。先读[维护记录](maintenance/2026-11-14_jpgpng_u1a_w5_w6.md)、[范围冻结稿](optimization/2026-11-14/U1A_W5_W6_SCOPE.md)与[切换 runbook](optimization/2026-11-14/U1A_SWITCH_RUNBOOK.md)；RFB 轮记录（`482eb03`，12 探针 12/12）与[契约 v4](optimization/2026-11-14/U1A_STATE_CONTRACT_V4.md)保留原文。未合并 main、未部署、未触碰真实图库/账号/网络，日常目录保持 `5465e0c`。

下载器 P0—P8、独立整理 CLI E1-2 已交付；E1-3 分类小样、E1-4 分批整理待实施。2026-11-11 三项目审计另发现需修复的库完整性和依赖风险，详见下方审计报告；历史交付不代表已消除这些问题。日常使用从根目录的说明进入，这里集中索引计划、当前进度和历史材料。

## 使用与维护

| 要做什么 | 入口 |
| --- | --- |
| 启动和了解当前能力 | [项目 README](../README.md) |
| 下载、筛选、续跑、名单、设置 | [使用说明](../使用说明.md) |
| AI 盘点关注、看图和受控分组 | [关注整理 CLI](../ORGANIZER_CLI.md) |
| 找文件、辨认业务资料和开发产物 | [项目目录说明](PROJECT_LAYOUT.md) |
| Git 检查点、分阶段留档和安全恢复 | [版本留档](VERSION_CONTROL.md) |
| 更新下载器授权、区分浏览器登录 | [账号与认证](AUTHENTICATION.md) |
| 离线体验界面 | [新版界面演示](refactor/新版界面演示.md) |
| 查看源码交付、可选构建和发布边界 | [构建与发布](BUILD_AND_RELEASE.md) |

## 当前进度与计划

- [优化、功能与归档联合实施：当前总入口](optimization/IMPLEMENTATION.md)：RFB-01..06 已按行为关闭（第 4K 节），W5/W6 已在本批交付（第 4L 节）；剩余为已登记遗留。
- [U1-A W5/W6 范围与冻结接口](optimization/2026-11-14/U1A_W5_W6_SCOPE.md)：四个工作包、文件归属、接口契约与"不做"清单。
- [切换与回退 runbook](optimization/2026-11-14/U1A_SWITCH_RUNBOOK.md)：预检、备份、副本演练、切换与回退命令，含退出码与副作用说明。
- [前置接受事务复审与开发指导](audits/2026-11-13-a1-acceptance/README.md)：AR4、修复复核、R0–R6 和版本证据。
- [U1-A/J-01 生产接线评审](audits/2026-11-13-u1a-wiring/README.md)：复用收据不等于当前磁盘健康；7 项独立红测试与 W0–W6。
- [上一轮后续实现审计](audits/2026-11-12-a1-final/README.md)：历史 AR3、N0–N6 与原始证据，不能当新树结果。
- [上一轮 A1 修复后复审与开发指导](audits/2026-11-12-a1-recheck/README.md)：逐项复核首轮修复、实际反例和 F0–F5，先补约束再评审生产。
- [A1 首轮审计](audits/2026-11-12-a1/README.md)：历史 250/5/3 和 12 项问题保留，不能作为当前树测试结果。
- [图库归档与空间释放：专项设计](archiving/README.md)：主动删图不默认补下；历史离线通过记录保留，生产迁移与真实归档仍未发布。
- [2026-11-11 优化与拓展设计](optimization/2026-11-11/README.md)：结合三项目审计，设计文件核验/修复、覆盖与筛选、性能及成熟引擎接入；含 yt-dlp 适用性。当前为讨论稿，尚未实施。
- [重构进度](refactor/REFACTOR_PROGRESS.md)：阶段状态、DSH 回执、实测结果和关机接续入口；查看最新状态先读页首。
- [详细设计总览](refactor/detailed_design/README.md)：01—07 核心设计与实施计划，08 为关注整理扩展。设计可选项不等于已发布能力；旧历史导入未选择、未发布。
- [关注画师整理扩展](refactor/detailed_design/08_FOLLOW_ORGANIZER_EXTENSION.md)：多图判断、真实账号分组、网站人工调整和后续验收。
- [2026-10-20 总体规划](refactor/REFACTOR_PLAN_2026-10-20.md)：已认可的原图质量、性能、无预览、轻量界面和相对路径要求。
- [认可的视觉基准](refactor/designs/approved_2026-10-21/VISUAL_BASELINE.md)：参考图和布局约束。

## 最近交付与整理

- [2026-11-14 U1-A W5/W6 维护记录](maintenance/2026-11-14_jpgpng_u1a_w5_w6.md)：待核验列表、全库巡检、W5 界面、库预检/备份、真实库只读演练、偏离登记与审计结论。
- [2026-11-14 U1-A RFB 修复记录](maintenance/2026-11-14_jpgpng_u1a_rfb_fix.md)：RFB-01..06 关闭（确认 pass、进度推进、清理 EOF、中间 handover、账本故障、每页预算），1,897 项全量与合成测量数字。
- [2026-11-14 U1-A RFA 修复记录](maintenance/2026-11-14_jpgpng_u1a_rfa_fix.md)：RFA-01..07 关闭、审计追加 P2 修复、1,831 项全量与合成测量数字。
- [2026-11-11 三项目详细审计](audits/2026-11-11/README.md)：JpgPng、gallery-dl、PixivUtil2 的功能/质量/性能/安全/维护对比，附问题明细、检索覆盖、离线复现和适用条件；未迁移或修改生产代码。
- [gallery-dl、PixivUtil2 源码快照](../../references/pixiv_downloaders/2026-11-11/README.md)：完整源码、固定提交、原包及逐文件摘要；获取记录保留当时状态，本轮审计结论见上一项。
- [2026-11-11 开源选型对比](OPEN_SOURCE_COMPARISON_2026-11-11.md)：核对 Powerful Pixiv Downloader、gallery-dl、PixivUtil2 与现有项目，记录原图默认策略、恢复边界及后续对照方案；本轮未安装或迁移。
- [下载器发布与首轮归档](../../docs/maintenance/2026-10-23_jpgpng_release_and_cleanup.md)：正式切换及验收。
- [CLI 发布记录](../../docs/maintenance/2026-10-24_jpgpng_organizer_cli.md)：工具、602 位盘点和有界真实技术小样，尚未正式分类。
- [项目目录整理](../../docs/maintenance/2026-10-24_jpgpng_directory_tidy.md)：本次范围、文档修订和核验。
- [本次归档及恢复说明](../../_archives/jpgpng_tidy_20261024/README.md)、[工作区归档总索引](../../_archives/README.md)。

## 历史资料

以下保留当时的日期、路径和结论；其中的安装、启动及“下一步”描述不替代当前说明。

- [2026-07 审计报告](AUDIT_REPORT.md)
- [早期清理与文档维护历史](maintenance/CLEANUP_HISTORY.md)
- [架构与界面演进](refactor/ARCHITECTURE_HISTORY.md)
- [下载、原图与关注续跑历史](refactor/DOWNLOAD_AND_CRAWL_HISTORY.md)
- [用户数据保护与快照历史](refactor/DATA_PROTECTION_HISTORY.md)

早期合并前的 444 个文档和工具原件、清单保存在项目 `record_backups/project_cleanup_20260729_215721/`。截图和设计稿保存在 `refactor/screenshots/`、`refactor/designs/`，继续作为验收依据。

当前环境以 [工作区环境说明](../../环境说明.md) 为准；本次整理没有安装依赖或调整解释器。
