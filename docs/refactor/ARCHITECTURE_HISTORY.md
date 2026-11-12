# PiDL 架构与界面演进

本文合并 2026-07 的架构审计、UI/日志优化计划、SQLite 实施报告和 IWLTY 审美迁移记录。合并前原文已归档至 `record_backups/project_cleanup_20260729_215721/pre_cleanup_files.zip`。

## 当前架构

PiDL 采用模块化单体与清晰端口边界：

```text
main.py
  └─ gui/main_controller.py → gui/main_view.py → GUI mixins
       ├─ core/auth_context.py / cookie_provider.py
       ├─ core/fetch_strategies.py → PixivFetcher mixins
       │    └─ follow_crawl_service.py
       │         ├─ pixiv_api_policy.py
       │         └─ follow_crawl_state.py
       ├─ core/http_client.py → requests Session 池
       ├─ core/download_task_store.py → 持久化 outbox
       └─ core/record_manager.py → SQLite + legacy 投影
```

职责按认证、抓取策略、下载交付、记录存储、关注任务状态和界面呈现拆分。旧 `core.pixiv_utils`、`core.deduplication` 和 `DownloadEngine` 保留为兼容门面，外部调用方无需一次性迁移。

## 关键架构决策

- 路径统一由 `core/app_paths.py` 管理，源码与打包环境共享同一规则。
- Cookie 解析进入 `CookieProvider`，只暴露脱敏元数据；GUI 不保存新的明文 Cookie。
- HTTP Session 按线程复用并显式关闭，减少 TLS/连接初始化和资源泄露。
- API 抓取通过策略表分派，长任务使用独立状态库，不把抓取进度混入下载记录。
- 下载任务先写 SQLite outbox，再进入有界内存队列；成功后删除，失败或崩溃后保留。
- 下载记录以 SQLite + WAL 为主，JSON/Markdown 作为兼容投影；事件游标支持崩溃后重建。
- 日志按字节轮转并定时 flush；GUI 只显示最近 3,000 行，磁盘保留完整会话。
- 原图模式只接受 `/img-original/`，无法取得原图时明确失败，不降级为预览图。

## 性能与可靠性变化

- 避免每张图片全量重写大型 JSON/Markdown。
- API 元数据携带完整 `meta_pages`，正常原图路径不再逐作品访问网页 Ajax。
- 生产者/消费者队列有界，防止快速抓取把任务全部堆入内存。
- 日志批量刷新、UI 更新节流、SQLite 流式投影降低 I/O 和 Tk 渲染成本。
- 非零残片必须经过大小/完整性校验，不能直接冒充完成文件。
- 完成状态不会被后续失败/跳过事件降级，事件轨迹仍完整保留。

## 界面设计

IWLTY 源码仅用于只读审美研究，未被修改。网页的编辑式网格、纸张层次、克制色彩和编号轨被翻译为适合 Tkinter 的桌面布局；没有引入前端运行时或网页框架。

主界面按 `00 ACCESS`、`01 SOURCE`、`02 TASK`、`03 EXECUTE`、`04 SESSION LOG` 分区。认证明确区分 App API 与原图 Cookie；高级设置按需展开；日志区弹性占用剩余空间。

视觉验收截图：

- [主界面（Windows 150%）](screenshots/main_ui_editorial_final_150pct.png)
- [多条件筛选（Windows 150%）](screenshots/multi_filter_editorial_final_150pct.png)

## 数据迁移与回滚

- SQLite 首次启用时从旧 JSON 导入，保留源文件和迁移备份。
- 兼容投影记录事件游标；投影落后时按事件流式重建。
- 高风险记录清理必须先进入 `record_backups/` 并进行二次确认。
- 回滚代码不能删除新库；先保留 SQLite、JSON、ID 文本和下载目录，再切换读取路径。

## 验证基线

2026-07-29 收口时全量测试为 80 项，AST、`pip check`、`git diff --check` 均通过；两轮正式续跑新增 2,010 张原图且最终失败为 0。累计重构影响范围较大，发布前仍需保留 GitNexus 影响审计、全量测试和人工检查。

