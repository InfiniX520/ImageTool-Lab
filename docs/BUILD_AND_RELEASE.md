# 构建、发布与 Git 维护

## 当前交付方式

当前验收的是源码配合已有解释器运行：下载器 `启动.bat`，整理 CLI `organize.cmd`。普通使用不需要构建或安装依赖。正式入口、离线演示和回退见 [项目 README](../README.md)、[使用说明](../使用说明.md)。

`build_exe.py` 是保留的 PyInstaller 辅助入口，其存在不代表新版下载器和 CLI 的独立 EXE 发布已验收。需要独立发布包时另立批次，核对入口、资源、数据隔离和迁移，再做实机验收。

## 可选构建

安装或调整环境前阅读工作区 [安装守则](../../给AI的安装守则.md)。复用工作区解释器，设置 `PYTHONNOUSERSITE=1`；`requirements-build.txt` 不属于日常启动的安装步骤。

已有构建依赖齐备并确认脚本适用后，可从项目根目录查看参数：

```powershell
$env:PYTHONNOUSERSITE = '1'
$projectPython = 'D:\Miniconda3\envs\JuiLiu\python.exe'
& $projectPython -s -B build_exe.py --help
```

现有参数包括 `--pidl-mode`、`--pikey-mode`、`--skip-pikey`、`--skip-zip`、`--output-dir`。使用独立、明确的输出目录；执行 `--clean` 前检查最终路径和已有内容，不把业务目录或已完成的验收批次作为输出。缺依赖时按安装守则处理，不自动补装。

## 发布数据边界

发布包只含程序、经检查的默认配置和公开说明，逐文件审查，不直接压缩整个项目。排除：

- `pixiv_config.json`、`www.pixiv.net_cookies.txt` 及其他授权资料。
- `downl_Pixiv/`、`download_logs/`、`runtime_logs/`、`record_backups/`。
- `organizer_data/`、`downloaded_ids.txt`、`准备下载.txt`。
- 个人偏好 `config/collection_v2.json`、本机映射 `config/output_roots_v1.json`。
- 项目和工作区 `.runtime/`、DSH 回执、历史归档及本机资料。

## 验证与 Git 维护

按改动范围选择相关离线测试。Windows Tk 多窗口测试须关闭 pytest 文件描述符捕获（`-s`），命令示例：

```powershell
& $projectPython -s -B -m pytest tests/ -q -s -p no:cacheprovider
git diff --check
git status --short
```

测试输出和临时目录指定到新的工作区 `.runtime/jpgpng_<批次>/`。纯文档和可恢复整理核验路径、链接、格式及摘要，不额外触发下载或登录。若实际改动依赖，再做相应环境检查。

提交前核对 `.gitignore` 和明确文件清单，不使用 `git add -A` 盲目暂存。`git restore --staged <文件>` 只用于取消暂存；`git restore <文件>` 会覆盖未提交修改，不能当作无损恢复。`git reset --hard`、`git clean -fd` 不用于一般整理。先保全当前修改，提交和推送另按用户授权执行。

## 版本历史

### v1.0.0（2026-03-28）

首个公开版本，覆盖基础下载流程、GUI、核心模块和依赖清单。已知限制包括限流、网络稳定性和多来源交集为空。

### v2.0.0（2026-03-28）

入口统一为 `main.py`，代码收敛至 `gui/`、`core/`、`utils/` 和 `config/`，启动脚本完成环境探测与兼容处理。

### 2026-07 架构维护

完成 Cookie 自动读取、原图质量门禁、SQLite 主记录、轮转日志、持久化下载 outbox、关注画师智能续跑、429 退避和模块化 GUI。详细设计与实爬证据见 `docs/refactor/` 下三份历史文档。
