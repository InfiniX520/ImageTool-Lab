# 项目清理与文档维护历史

## 原则

清理只处理可再生缓存、明确无引用的工具副本、临时调试产物和已经合并的重复文档。用户下载、记录、日志、配置、Cookie、备份与用户输入文件永远不属于清理对象。

## 2026-07-20 清理

- 删除旧顶层过渡实现和已迁移的构建日志。
- 删除 Python/pytest 缓存。
- 保留兼容门面、启动脚本、打包入口、截图证据和全部用户数据。

## 2026-07-29 首次维护

清理脚本将临时 `_check_*.py` 和旧会话日志移入 `_cleanup_trash/20260729_123850`，并生成 `_cleanup_reports/`。这些内容在本次清理前已整体归档，因此隔离目录和中间 JSON 报告可以删除。

## 2026-07-29 综合收口

清理前归档：

- `record_backups/project_cleanup_20260729_215721/pre_cleanup_files.zip`
- `record_backups/project_cleanup_20260729_215721/pre_cleanup_manifest.csv`

归档覆盖 444 个文件、约 11.74 MiB，并记录每个源文件的大小、修改时间和 SHA-256。

实际清理/替换原始内容约 11.29 MiB；增加 3.86 MiB 的压缩归档后，项目工作区净减少约 7.43 MiB。`docs/` 从 29 个文件收敛为 9 个文件，其中 7 份 Markdown、2 张 UI 验收图。

### 删除

- `.agents/skills/frontend-slides/`：与下载器无关，且与 `.claude` 副本及其内部插件副本重复。
- `.claude/skills/frontend-slides/`：与项目无关；保留 `.claude/skills/gitnexus/`。
- `.github/agents/OS.agent.md`：通用推理 agent，与运行、构建、测试无引用关系。
- `.idea/`、`.vscode/`：本机 IDE 元数据。
- `_cleanup_trash/`、`_cleanup_reports/`：已归档的一次性隔离和中间报告。
- 已合并的用户、认证、发布、Git、重构和数据快照文档。

### 合并后结构

- `README.md`：项目入口和数据红线。
- `使用说明.md`：完整用户操作。
- `docs/AUTHENTICATION.md`：Token 与 Cookie。
- `docs/BUILD_AND_RELEASE.md`：构建、发布、Git 与版本历史。
- `docs/refactor/ARCHITECTURE_HISTORY.md`：架构、UI、日志和 SQLite。
- `docs/refactor/DOWNLOAD_AND_CRAWL_HISTORY.md`：原图、重试、outbox 和智能续跑。
- `docs/refactor/DATA_PROTECTION_HISTORY.md`：保护规则和快照时间线。
- 本文：删除依据、归档和验证记录。

### 保留

运行代码、测试、构建脚本、启动脚本、GitNexus 索引/技能、UI 截图证据以及全部业务数据均保留。

静态导入审计发现 `utils/hash_utils.py` 与 `utils/request_utils.py` 没有仓库内部调用；GitNexus upstream 风险均为 LOW、内部调用者为 0。但两者明确标记为 backward-compatible 公共门面，仓库外脚本无法由内部图谱证明不存在，因此按“零功能差异”原则保留，不把兼容 API 当作垃圾删除。`AGENTS.md` 与 `CLAUDE.md` 内容相同但分别服务不同 agent 入口，也作为有意兼容文件保留。

## 验证门槛

清理后必须执行 Markdown 链接检查、AST 解析、全量测试、`pip check`、`git diff --check`、GitNexus `detect-changes` 和正式数据完整性复核；随后删除测试缓存并确认相关进程为 0。

### 本次结果

- Markdown：11 份入口/工具文档，15 个本地链接，断链 0。
- AST：67 个 Python 文件解析通过。
- 测试：80/80 通过。
- 依赖：`pip check` 通过。
- Git：`git diff --check` 通过，仅保留既有 LF/CRLF 提示。
- 数据：记录 58,759、事件 34,077、兼容 JSON 58,759、游标 34,077、outbox 0；三套 SQLite 均为 `integrity_check=ok`。
- GitNexus：`detect-changes(compare main)` 显示累计未提交重构影响 52 条执行流、风险 `critical`；本次清理没有修改或删除运行时符号，风险来源是此前整套架构重构。

## 2026-07-30 维护收口

本日完成三轮修复/重构，按时间顺序：

### 第一轮：GUI 交互三大 Bug 修复 + 诊断日志 + 性能优化

- **BUG-001（critical）**：`DualScrollText` 继承 `tk.Frame`，`__getattr__` 不委托 `tk.Misc` 上的 `config/configure/cget/yview/see`，导致 `config(state='normal')` 抛 `TclError`、日志不显示且 `_clear_log` 崩溃使下载线程无法启动。修复：显式 override 这些方法委托给内部 `tk.Text`。
- **BUG-002（high）**：`_process_log_queue` 无异常保护且 `ok=False` 时终止 `after` 调度链。修复：包 try/except 保活，`ok=False` 时继续调度。
- **BUG-003（high）**：`_download_task` 的 `join(timeout=None)` 永久阻塞，`close_current_http_session` 只关本线程 session。修复：join 永远有限超时（停止 2s / 正常 30s），停止时改用 `close_http_sessions()` 关闭全部 session。
- **BUG-004（medium）**：`download_file` 的 `iter_content` 循环不检查 `stop_event`，停止按钮无效。修复：新增 `stop_event` 参数，检测到时抛 `RuntimeError`（后改为 `DownloadStopped`）。
- **LOG-001**：在 join 块、downloader except、download_file iter_content、`_process_log_queue` except 共 4 处加诊断日志。
- **PERF-001**：`MAX_UI_LOG_LINES` 3000→512，完整日志仍通过 `SessionLogQueue` 分卷落盘。
- **PERF-002**：移除蓝色旋转进度条（`ttk.Progressbar` indeterminate）。
- **PERF-003**：移除无意义颜色动画，`set_pill`/`bind_hover_highlight` 改为即时 `config`。

### 第二轮：4 代理联合审计 + 系统性修复（7 项）

来源：`docs/AUDIT_REPORT.md`（24 findings，综合评分 7.6/10）。规划：`TASK_PLAN.json`。

- **CQ-1**：删除死代码 `animate_color`/`ColorAnimator`/`_build_frames`/`_ANIMATOR_ATTR`。
- **CQ-7**：新增 `class DownloadStopped(Exception)`，6 处 `raise RuntimeError('任务已停止')` 改为 `raise DownloadStopped()`，downloader 用 `isinstance` 判断。
- **SE-1**：`_load_cookie_file` 成功加载 Netscape 后主动删除 `config.json` 中残留的 `cookie` 字段。
- **SE-2**：`_validate_download_request_inputs` 新增 `delay>300` 和 `api_delay>60` 上限校验。
- **CQ-2**：审查 26 处 `except Exception`，2 处业务逻辑加 `log_warning`（`follow_crawl mark_artist_swept` + downloader `mark_failed`）。
- **CQ-6**：提取 `JOIN_TIMEOUT_STOP=2.0` / `JOIN_TIMEOUT_NORMAL=30.0` 类常量替代魔法数字。

跳过 CQ-3（拆分 `_download_task`）和 CQ-4（移除 `pixiv_utils` 门面）因高风险。

### 第三轮：第二轮跳过项 + isort 统一

- **CQ-4**：移除 `core/pixiv_utils.py` 的 `_CompatibilityModule` 模块替换 hack，删除 `sys`/`types` 导入；测试改用 `set_debug_mode()` 函数 API。
- **CQ-3**：拆分 `_download_task`（337→231 行，-31%），提取 `_setup_download_context`(27行) / `_join_download_threads`(30行) / `_report_download_completion`(40行)；downloader 闭包因捕获 15+ 变量保持内联。
- **CQ-8**：`isort 8.0.1` 以 `--profile black` 统一全项目 32 个文件的 import 分组。

### 验证

- 测试：80/80 通过（每轮后均验证）。
- 运行时：GUI 启动正常，日志显示正常，停止按钮有效，参数校验生效。
- 数据：未触碰任何用户下载/记录/Cookie 数据。

### 未完成跳过项

- **LA-1**（根目录文件归类）：评估后跳过，移动入口脚本会破坏用户 `python pixiv_get_token.py` 使用习惯且需联动 `PROTECTED_FILES` 与启动脚本路径，收益小风险高。
- **AR-2**（GUI 直接依赖 core）：桌面单体应用耦合可接受，过度抽象反而增加复杂度。
- **AR-3**（`pixiv_utils` 门面职责）：需评估外部脚本依赖，风险高。
- **AR-4**（HTTP session 生命周期）：当前固定 3 线程足够。

## 2026-07-30 第四至第八轮重构

### 第四轮：文档化重构（S4）

- **AR-1**：为 10 个 Mixin 类添加 Host contract docstring，列出期望宿主提供的属性/方法及来源。
- **LA-2**：补充 layout_mixin 8 个 `_build_*`/`_configure_theme` 方法 docstring；验证 core/ 公开函数 docstring 覆盖率已近 100%。
- **LA-4**：追加本文档 2026-07-30 三轮记录（即上方"第一轮"至"第三轮"）。
- **LA-3**（阻塞）：`node .gitnexus/run.cjs analyze` 因 EPERM 写 `~/.gitnexus/registry.json` 失败，需管理员权限。
- **LA-1**（跳过）：移动入口脚本破坏用户习惯，收益小风险高。
- 测试：80/80 通过。

### 第五轮：测试覆盖率 + 工具链固化（S5）

- **P0-A**：覆盖率基线 55%（http_client 45%、download_engine 68%、ui_fx 14%）。
- **P0-C**：新增 10 回归测试（`test_download_engine_regression.py`），覆盖 BUG-003/004 + CQ-7 修复路径。http_client 45%→88%、download_engine 68%→72%。
- **P0-B**：创建 `pyproject.toml` + `.flake8` 固化 isort/black/flake8 工具链（line-length=100），更新 `requirements-dev.txt`。
- **P1-A**：vulture 清理 3 处死代码（`set_pill` animate 参数 + download_service 2 死导入），radon 定位 `download_image` F(45) 为下一轮首要目标。
- 测试：90/90 通过（80+10）。

### 第六轮：复杂度削减四连击（S6）

4 个 D/F 级函数全部降至 A-C 级：

- **R6-1**：`download_image` F(45)→C(19) -58%。
- **R6-2**：`fetch_original_meta` D(24)→C(14) -42%。
- **R6-3**：`_search` D(23)→A(5) -78%。
- **R6-4**：`_reconcile_snapshot` D(22)→C(11) -50%。
- **R6-5**：flake8 200→113 -43.5%，F401/W293/E302-E306 全清零。
- **R6-6**：`pip-audit` 无 CVE 漏洞。
- **R6-7**：search/ranking mixin 覆盖率 7%→30%/37%，新增 19 单元测试。
- 测试：109/109 通过（90+19）。

### 第七轮：收尾复杂度 + E501 清零（S7）

- **R7-1**：`_sweep_artist` C(18)→B(7) -61%，提取 3 个 helper（`_find_sweep_cutoff` + `_queue_sweep_items` + `_advance_sweep_cursor`）。
- **R7-2**：E501 长行 113→0，flake8 `--count` 自审计以来首次归零。black 自动修复 100 处 + 手动修复 13 处。
- **R7-3~R7-6**：新建 `test_sweep_artist_mock.py` 17 测试覆盖 `_sweep_artist` 5 核心分支；在 `_sweep_artist`/`download_image`/`_reconcile_snapshot` 添加诊断日志（`[D]` 前缀 / `log_message('debug')`）。
- 测试：126/126 通过（109+17）。

### 第八轮：E 级复杂度清零（S8）

- **R8-1**：`_validate_download_request_inputs` E(34)→A(3) -91%，提取 5 个校验 helper（全 B 级）。
- **R8-2**：`_process_illust` E(37)→B(7) -81%，提取 3 个流水线 helper（B(8)/C(12)/C(14)）。
- 里程碑：**项目当前无 E 级函数**，flake8 保持 0。
- 测试：126/126 通过。

### 验证

- 测试：126/126 通过（R8 后）。
- flake8：0（自 R7 首次归零后保持）。
- 复杂度：无 E 级函数，剩余 7 个 D 级为非目标项。
- 数据：未触碰任何用户下载/记录/Cookie 数据。

## 2026-10-24：下载器主线重构后的可恢复清理

P0—P8 完成并切换后，按逐文件清单归档移除 22,652 个生成测试文件和 15012 个空子目录，净回收约 3.39 GiB。ZIP 内大小/摘要与原件全部一致，移除前重新校验路径和摘要；链接拒绝处理。真实下载、配置、业务库和备份、源码快照、报告、截图、DSH 回执和预览数据保留。详见[最终记录](../../../docs/maintenance/2026-10-23_jpgpng_release_and_cleanup.md)和[恢复索引](../../../_archives/jpgpng_refactor_20261023/README.md)。本节不将旧轮次测试结果改写为当前验收。
