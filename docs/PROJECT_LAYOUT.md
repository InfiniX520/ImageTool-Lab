# 项目目录说明

下载器双击 `启动.bat`，AI 整理工具使用 `organize.cmd`；操作分别见 [使用说明](../使用说明.md) 和 [CLI 说明](../ORGANIZER_CLI.md)。计划和历史统一从 [文档导航](README.md) 查找。

## 根目录各项用途

| 文件或目录 | 用途 |
| --- | --- |
| `启动.bat`、`main.py` | 当前下载器；`启动.bat --legacy` 保留旧版入口 |
| `预览新界面.bat` | 同一份代码的离线演示 |
| `启动.sh` | 现存脚本入口，保留；本机日常使用 Windows 启动器 |
| `organize.cmd`、`organize.py` | 独立关注整理 CLI |
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

项目内 `.runtime/` 保存离线演示资料和 DSH 隔离工作区、回执。工作区上级 `.runtime/jpgpng_*/` 保存分批开发、冻结候选、测试报告、真实小样与源码基线。其中的真实样本和验收材料需要保留。

五套旧界面试验包、已完成阶段的中间副本和一批模拟测试目录已进入 [2026-10-24 归档](../../_archives/jpgpng_tidy_20261024/README.md)。清单保留原工作区相对路径和 SHA-256；历史文档提到的旧副本可据此找回。每阶段最终候选、正式报告、原始基线、DSH 原交付和真实联网小样保留原位。

后续维护约定：

1. 当前导航放 `docs/`；旧重构设计/进度留 `docs/refactor/`，优化与归档当前总入口为 `docs/optimization/IMPLEMENTATION.md`，归档专项在 `docs/archiving/`，分批审计在 `docs/audits/`；维护结论放工作区 `docs/maintenance/`。
2. 测试和报告放新的工作区 `.runtime/jpgpng_<任务>_<日期>_<批次>/`，不覆盖已完成批次。
3. 整理先列明确路径，检查引用、链接和保护边界；压缩并逐文件核验后，再移除原件并保存恢复索引。
4. 不运行旧版通用清理脚本，不把 `.runtime/`、日志、备份或 SQLite 所在目录一概清空。

## 当前 A1 开发资料（2026-11-12）

- 联合实施是唯一总排期；[本轮审计](audits/2026-11-12-a1/README.md)只有总览、问题、开发指导三份，避免每个小修增加顶层计划。
- [审计证据批次](../../.runtime/jpgpng_a1_audit_20261111_01/)保存源码摘要/快照、合成反例和失败报告，不能当作通过回执。
- [writer 草稿批次](../../.runtime/jpgpng_a1_writer_compat_20261111_01/README.md)尚未完成；改前副本和失败 XML 保留，修复应新建批次并引用它。
- 2026-11-11 的 foundation/scope_adapter 等完成批次保留原报告与检查点；旧通过数量只对应当时源码。当前审计未搬动模块，也未清理测试现场或业务数据。
