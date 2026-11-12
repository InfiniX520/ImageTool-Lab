# 用户数据保护与快照历史

本文合并 2026-07-20 至 2026-07-29 的数据保护清单。历史原始报告及其 SHA-256 记录已保存在 `record_backups/project_cleanup_20260729_215721/pre_cleanup_files.zip`。

## 永久保护边界

以下内容是业务资产，不是缓存：

- `downl_Pixiv/`：下载图片与画师目录。
- `download_logs/download_records.sqlite3`：页级下载记录主库。
- `download_logs/master_downloaded_record.json`：兼容 JSON 投影。
- `download_logs/follow_crawl_state.sqlite3`：关注任务快照与断点。
- `download_logs/pending_download_tasks.sqlite3`：未完成下载任务 outbox。
- `downloaded_ids.txt`：作品级兼容断点。
- `runtime_logs/`：轮转会话日志。
- `record_backups/`：迁移、清理和恢复前快照。
- `pixiv_config.json`、`www.pixiv.net_cookies.txt`：敏感用户配置。
- `准备下载.txt`：用户维护的画师 ID 输入。

清理、测试和构建都不得删除或重置这些路径。

## 快照时间线

| 日期 | 事件 | 核心结论 |
|---|---|---|
| 2026-07-20 | 重构前基线 | 根目录记录、集中 JSON 和图片目录完成数量与哈希盘点 |
| 2026-07-20 | 受控实爬 | 原图 URL 与文件存在性通过，保护规则保持不变 |
| 2026-07-20 | SQLite 迁移 | SQLite 与兼容 JSON 对账，迁移前备份保留 |
| 2026-07-20 | 页级审计修复 | 多页缺口与重试语义修复后重新核对记录/文件 |
| 2026-07-28 | 关注续跑实施 | 关注状态库与下载主库隔离，真实探针前后数据哈希不变 |
| 2026-07-29 | 原图/交付恢复 | 历史失败作品补齐，outbox 与投影游标验证通过 |
| 2026-07-29 | 两轮正式续跑 | 新增 2,010 张原图，最终失败 0，断点安全保留 |

## 最新已验证状态（2026-07-29）

- 下载记录：58,759。
- 下载事件：34,077。
- 兼容 JSON：58,759。
- 投影游标：34,077 / 34,077。
- `downloaded_ids.txt`：25,319 个唯一纯数字 ID。
- outbox：0。
- 三套 SQLite：`integrity_check=ok`。
- 最近两批 2,010 条新增页记录：缺失 0、大小不一致 0、非原图 0。

## 操作规则

1. 高风险操作前创建同一时间点的 SQLite、WAL/SHM、JSON、ID 文本和关注状态备份。
2. 停止所有项目进程后再复制数据库；优先使用 SQLite backup/checkpoint 语义。
3. 不用空 JSON、空文本或旧快照覆盖当前记录。
4. 测试必须使用隔离临时目录，不得指向正式数据路径。
5. 清理后复核记录数、事件数、投影游标、outbox 和文件存在性。

