# ImageTool-Lab

一个基于 Python + Tkinter 的 Pixiv 图片下载工具，支持多种数据来源、基础筛选和多线程下载。

## 功能概览

- 图形界面操作，配置自动保存
- 支持多种下载模式：
  - 关键词搜索
  - 指定用户作品
  - 关注新作
  - 关注用户全部作品
  - 排行榜（支持日期范围）
- 支持基础筛选：
  - 跳过 AI 作品
  - 最低收藏数
  - 跳过已下载作品（断点续传）
- 支持文件命名模板、按画师分文件夹
- 提供多数据源交集下载界面（交集模式）

## 环境要求

- Python 3.9+
- Windows / macOS / Linux（当前主要在 Windows 上验证）

## 安装依赖

```bash
pip install -r requirements.txt
```

## 快速开始

### 1. 获取 Refresh Token

```bash
python pixiv_get_token.py
```

按脚本提示完成网页登录流程，拿到 Refresh Token。

### 2. 启动主程序

```bash
python pixiv_downloader.py
```

在界面中填入 Refresh Token，登录后选择下载模式并开始下载。

## 主要脚本说明

- pixiv_downloader.py: 主 GUI 程序入口
- pixiv_fetcher.py: 不同模式的作品获取逻辑
- pixiv_utils.py: 下载、日志、ID 记录、防封等待等公共工具
- pixiv_multi_filter.py: 多数据源交集下载界面
- pixiv_get_token.py: 获取 Pixiv Refresh Token 的辅助脚本

## 配置与安全

- 本地运行会生成 pixiv_config.json（包含本地配置与 token 信息）
- 该文件已在 .gitignore 中忽略，不会被提交到仓库
- 建议不要在任何公开场景泄露 token 或 cookie

## 常见使用建议

- 首次先小规模下载测试参数（数量、线程、间隔）
- 若出现 API 空数据或限流，适当调大延迟并降低并发
- 排行榜批量日期下载建议分批运行，减少中断影响

## 已知限制

- 依赖 Pixiv 接口可用性与账号状态
- 接口策略变化可能导致部分模式短期失效
- 交集模式至少选择 2 个数据源才可执行

## 免责声明

请遵守 Pixiv 服务条款与当地法律法规，仅用于个人学习与合规用途。

## 文档导航

- Git 文档索引: docs/git-guides/README.md
- 上传与推送流程: docs/git-guides/GITHUB_UPLOAD_PROCESS_2026-03-28.md
- 本地恢复手册: docs/git-guides/LOCAL_RECOVERY_GUIDE.md
- 首版发布说明: docs/git-guides/RELEASE_v1.0.0.md
