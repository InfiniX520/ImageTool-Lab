# 切换与回退：把日常入口换到 W5/W6 分支

本页给的是**可执行命令**，不是承诺。所有数字来自本批实测（见[维护记录](../../maintenance/2026-11-14_jpgpng_u1a_w5_w6.md)）。

## 0. 开工前的事实（已实测）

| 项目 | 值 |
| --- | --- |
| 真实库 | `JpgPng/download_logs/library.sqlite3`，4,435,968 字节，`-wal` 0 字节 |
| 预检 | `status=compatible`、`healthy=true`、49/49 对象、`quick_check/integrity_check=ok`、外键 0 冲突 |
| 内容 | 1 job / 1 batch（`paused`，user_pause）/ 540 batch_items / 908 deliveries（897 `suspended` + 11 `completed`）/ 11 artifacts |
| 巡检 | 11 个 artifact **全部 `match`**（完整 SHA-256，95,574,715 字节，0.12 秒） |
| 日常入口现状 | 分支 `fix/a1-ar3-20261112` @ `5465e0c`，工作树干净 |

结论：**库不需要迁移**，新代码可以直接打开它；切换是「换代码」，不是「改数据」。

## 1. 预检（只读，随时可跑）

```powershell
cd E:\Zero9\JuiLiu\.runtime\jpgpng_u1a_w5_w6_20261114_01\integration_worktree
python maintain.py inspect E:\Zero9\JuiLiu\JpgPng\download_logs\library.sqlite3
```

通过标准：`状态: compatible`，`完整性: quick_check=ok integrity_check=ok 外键冲突=0`。
注意：`-shm` 的 mtime 会变，这是 SQLite 的簿记文件，不是数据变化；主库字节数与 sha256 不变（已逐字节验证）。

## 2. 备份（写目标目录，不写源库）

```powershell
python maintain.py backup E:\Zero9\JuiLiu\JpgPng\download_logs\library.sqlite3 E:\Zero9\JuiLiu\.runtime\jpgpng_u1a_w5_w6_20261114_01\rehearsal --label pre-switch
```

通过标准：`校验: verified=True 对象一致=True 计数一致=True`、`源库: 主文件未变=True 整组未变=True`。
产物：目录内一个自包含的 `library.sqlite3`（在线快照会把副本的 journal 归一成 delete，所以还原时**不能**配旧 `-wal`）+ `manifest.json`（含每文件 sha256 与还原步骤）。
没有 `manifest.json` 的目录不算备份。

## 3. 副本演练（确认新代码能对它做完整的事）

```powershell
python maintain.py verify <源库> <备份目录>\library.sqlite3
python maintain.py scan   <备份目录>\library.sqlite3 --root E:\Zero9\JuiLiu\JpgPng
```

通过标准：`verify` 输出 `ok=True`；`scan` 输出 `核验: N 行（匹配 N，无法核验 0）` 且 `结论: 本次核验的每一行都与记录一致。`
`scan` 的退出码：0 = 全部一致（`unknown` 只统计不报错，会单独打印"另有 N 行没有可核验的记录"）；2 = 有 missing/changed/unreadable/root_unavailable/changed_during_check；3 = 账本读不出来；4 = 参数被拒。
`scan` 只读：它不写库、不动图库文件，唯一副作用是 SQLite 打开 WAL 库时刷新 `-shm` 的 mtime（主库字节与 sha256 不变）。耗时与机器相关（本机 11 个文件 / 95 MB 约 0.1–0.3 秒），不要当验收阈值。

## 4. 切换（一行）

```powershell
cd E:\Zero9\JuiLiu\JpgPng
git status --porcelain          # 必须是空的
git checkout fix/a1-u1a-w5-w6-20261114
```

- 只换代码。`download_logs`、`downl_Pixiv`、`pixiv_config.json`、cookie、`config/` 都是未跟踪/被忽略的业务资料，不会被这次 checkout 触碰。
- 切完先用 `启动.bat` 打开界面，进「核验」页看一眼：如果批次是暂停的，会显示「批次已暂停，恢复后才能核验或补下载」，这是设计如此。
- 想先看效果又不想切：双击 `.runtime\jpgpng_u1a_w5_w6_20261114_01\preview_panel.cmd`（用真实库的**副本**跑演示入口，动不到真库）。

## 5. 回退（两件事，顺序无关）

```powershell
cd E:\Zero9\JuiLiu\JpgPng
git checkout fix/a1-ar3-20261112        # 代码回到旧分支 5465e0c
```

只有在你**确实怀疑库被写坏**时才需要还原数据：

1. 关闭 JpgPng 主程序，确认没有下载/整理任务在跑；
2. 把备份目录里的 `library.sqlite3` 复制覆盖回 `JpgPng/download_logs/library.sqlite3`；
3. 如果原路径旁还有 `library.sqlite3-wal` 或 `library.sqlite3-shm`，一并删除（旧事务日志不能配新文件）；
4. 再跑一次 `maintain.py inspect`，必须是 `compatible`。

## 6. 切换后建议做的三件事

1. `python maintain.py scan <库>` —— 确认图库与记录仍然一致；
2. 在「任务」页恢复那个暂停的批次（这是用户决定，不是自动的）；
3. 观察第一批下载：新引擎会把每一页的观察、准备、发布、确认都写进事件；出问题先看 `runtime_logs/v2`。

## 7. 明确不做的

- 不做自动迁移、不做自动还原、不做后台静默修复；
- 不在批次运行时用 `--mode file_group` 备份（整组复制要求源安静，在线快照可以并发）；
- 不把 `suspended`（批次停住）当成事故，也不因为「记录里没有」就自动补下载。
