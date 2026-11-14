# 项目目录说明

**当前入口**：版本与排期见[联合实施总入口](optimization/IMPLEMENTATION.md)、[最新独立审计](audits/2026-11-14-u1a-w5w6-proof-independent/README.md)。当前活跃证据位于工作区 `.runtime/jpgpng_e4_audit_20261114_01/`；历史 JpgPng 开发批次已归入[本机归档索引](../../_archives/jpgpng_development_20261114/INDEX.md)。本页说明目录/归档边界，历史验收不替代当前审计。

**前置 A1 阶段（2026-11-13，历史）**：dda6fc1：AR4-01..08 已关闭；schema 5；全量 1,494 项 1,491 通过/3 跳过（Tk 文件单独 4/4），独立 29/29；A1 离线验收完成，尚未接生产。历史问题见[接受事务复审](audits/2026-11-13-a1-acceptance/README.md)与[修复记录](maintenance/2026-11-13_jpgpng_a1_ar4_fix.md)；原轮次证据保留。

下载器双击 `启动.bat`，AI 整理工具使用 `organize.cmd`；操作分别见 [使用说明](../使用说明.md) 和 [CLI 说明](../ORGANIZER_CLI.md)。计划和历史统一从 [文档导航](README.md) 查找。

## 根目录各项用途

| 文件或目录 | 用途 |
| --- | --- |
| `启动.bat`、`main.py` | 当前下载器；`启动.bat --legacy` 保留旧版入口 |
| `预览新界面.bat` | 同一份代码的离线演示 |
| `启动.sh` | 现存脚本入口，保留；本机日常使用 Windows 启动器 |
| `organize.cmd`、`organize.py` | 独立关注整理 CLI |
| `maintain.cmd`、`maintain.py` | 库预检、备份、封存校验与只读巡检；不自动恢复数据库 |
| `README.md`、`使用说明.md`、`ORGANIZER_CLI.md` | 三个阅读入口 |
| `core/`、`gui/`、`utils/`、`organizer/` | 下载核心、界面、共用工具和关注整理实现 |
| `tests/` | 离线功能测试 |
| `docs/` | 当前导航、设计、进度和历史文档 |
| `scripts/` | 维护工具说明；旧清理脚本已归档 |
| `requirements*.txt`、`pyproject.toml`、`.flake8` | 依赖和开发配置，保留工具默认识别位置 |
| `build_exe.py`、`pixiv_get_token.py` | 现存构建、认证辅助入口，不是日常启动步骤 |
| `LOG.json`、`TASK_PLAN.json` | 累积日志和计划；最新阶段见进度页和 `current_plan` |
| `.venv-path` | 本机解释器指针；启动器也可读取上级指针 |
| `.git/`、`.gitignore`、`.gitnexus/`、`.claude/`、`AGENTS.md`、`CLAUDE.md` | 版本、索引与开发约定，保留工具识别位置 |

源码入口、依赖配置和账号文件保留原位，避免改变程序查找规则。根目录不再新增截图、测试报告或临时副本。

## 业务资料

| 位置 | 内容 |
| --- | --- |
| `downl_Pixiv/` 或选择的输出目录 | 原始图片 |
| `download_logs/` | 新旧账本、任务和断点 |
| `organizer_data/` | 账号绑定、关注快照、样图和操作记录 |
| `config/` | 当前偏好、默认配置和本机输出目录映射 |
| `pixiv_config.json`、`www.pixiv.net_cookies.txt` | 敏感授权资料 |
| `downloaded_ids.txt`、`准备下载.txt` | 旧完成记录和下载输入 |
| `record_backups/` | 业务与版本备份，包含早期归档原件 |
| `runtime_logs/` | 运行诊断日志，由程序自身的容量策略管理 |

这些目录都不能作为通用清理目标；完整保护范围在工作区 [workspace.json](../../workspace.json)。迁移前先关闭程序，保存数据库及关联资料；见 [备份与回退](../使用说明.md#备份与回退)。

## 开发产物和归档

项目内 `.runtime/` 保存当前离线演示资料；历史 DSH 隔离工作区、回执和早期验证材料已归入[本机开发归档](../../_archives/jpgpng_development_20261114/INDEX.md)的 `project_local/`。工作区上级 `.runtime/` 只保留当前活跃批次；历史 `jpgpng_*` 开发批次、冻结候选、测试报告、真实小样与源码基线均保留在归档中，没有删除。

五套旧界面试验包、已完成阶段的中间副本和一批模拟测试目录已进入 [2026-10-24 归档](../../_archives/jpgpng_tidy_20261024/README.md)。清单保留原工作区相对路径和 SHA-256；历史文档提到的旧副本可据此找回。2026-11-14 整理另外保存了逐批原路径、目标路径、Git HEAD 与未提交状态映射，见[开发归档索引](../../_archives/jpgpng_development_20261114/INDEX.md)。

后续维护约定：

1. 当前导航放 `docs/`；旧重构设计/进度留 `docs/refactor/`，优化与归档当前总入口为 `docs/optimization/IMPLEMENTATION.md`，归档专项在 `docs/archiving/`，分批审计在 `docs/audits/`；维护结论放工作区 `docs/maintenance/`。
2. 新测试和报告放工作区 `.runtime/jpgpng_<任务>_<日期>_<批次>/`，不覆盖已完成批次；每批有 README、明确角色与保全文末，完成后移入本机归档并更新索引。
3. 整理先列明确路径，检查引用、链接和保护边界；先保存清单和可恢复归档，再移动明确目录。worktree 使用 Git 支持的移动，保留分支、HEAD、dirty 文件与元数据；不使用 `reset --hard`、`clean -fd` 或递归强删。
4. 不运行旧版通用清理脚本，不把 `.runtime/`、日志、备份或 SQLite 所在目录一概清空。归档中的旧预览/播种/生成脚本默认**只供审阅，不可直接重放**；日常离线演示使用根目录 `预览新界面.bat`。
5. 旧文档里的 `.runtime/jpgpng_*` 路径是当时的记录，不机械改写历史事实；从归档索引按批次名查找。正在使用的导航不得指向已移出的旧路径。

## A1 首轮开发资料（历史）

- 联合实施是唯一总排期；[本轮审计](audits/2026-11-12-a1/README.md)只有总览、问题、开发指导三份，避免每个小修增加顶层计划。
- [审计证据批次](../../_archives/jpgpng_development_20261114/2026-11/jpgpng_a1_audit_20261111_01)保存源码摘要/快照、合成反例和失败报告，不能当作通过回执。
- [writer 草稿批次](../../_archives/jpgpng_development_20261114/2026-11/jpgpng_a1_writer_compat_20261111_01/README.md)尚未完成；改前副本和失败 XML 保留，修复应新建批次并引用它。
- 2026-11-11 的 foundation/scope_adapter 等完成批次保留原报告与检查点；旧通过数量只对应当时源码。当前审计未搬动模块，也未清理测试现场或业务数据。

## A1 上轮修复后复审资料（历史）

当时报告统一从[联合实施](optimization/IMPLEMENTATION.md)进入，[复审三份材料](audits/2026-11-12-a1-recheck/README.md)集中结论、问题和开发步骤。本轮调查集中工作区上级 `.runtime/jpgpng_a1_reaudit_20261112_01/`，不在源码目录新增临时脚本。

前序修复出现两级同名 runtime：项目内 `.runtime/jpgpng_a1_repair_20261112_01/` 保存原 XML/validation/检查点；工作区上级同名目录保存部分合成测试现场。两处材料均保留，现已分别归入开发归档的 `project_local/` 与 `2026-11/` 下，不合并覆盖。新批次一律使用工作区上级 runtime，准确记录运行 cwd 与相对路径。


## A1 接受事务审计与并发资料（2026-11-13 历史）

[当前三份报告](audits/2026-11-13-a1-acceptance/README.md)集中问题与下一步。证据统一在工作区 `.runtime/jpgpng_a1_acceptance_audit_20261112_01/README.md`，其中 `worktrees_inventory.json` 登记现有 worktree 的 HEAD 和未提交状态。当时上一执行批次为 `.runtime/jpgpng_a1_ar3_20261112_01/`；本节记录当时事实。2026-11-14 已按[归档索引](../../_archives/jpgpng_development_20261114/INDEX.md)移动历史开发批次，原报告内容保留。
