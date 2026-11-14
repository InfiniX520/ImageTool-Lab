# 历史切换与回退说明：W5/W6 分支

**当前限制（2026-11-14 独立审计）**：WR-01..07 尚未关闭。此页保留此前切换/回退流程供对照，**禁止按其中步骤恢复真实库**，也不要将单条 Git checkout 视为数据兼容性与恢复证明。日常离线预览使用项目内 `预览新界面.bat`；归档中的播种/预览脚本不可直接重放。先读[最新开发指导](../../audits/2026-11-14-u1a-w5w6-proof-independent/DEVELOPMENT_GUIDE.md)。

下文保留当时的命令和实测背景，不作为当前真实库恢复的执行许可。数字来自本批实测（见[维护记录](../../maintenance/2026-11-14_jpgpng_u1a_w5w6_proof.md)）。

## 0. 开工前的事实（已实测）

| 项目 | 值 |
| --- | --- |
| 真实库 | `JpgPng/download_logs/library.sqlite3`，4,435,968 字节，`-wal` 0 字节 |
| 预检 | `status=compatible`、`healthy=true`、49/49 对象、`quick_check/integrity_check=ok`、外键 0 冲突 |
| 内容 | 1 job / 1 batch（`paused`）/ 540 batch_items / 908 deliveries（897 `suspended` + 11 `completed`）/ 11 artifacts |
| 巡检 | 11 个 artifact **全部 `match`**（完整 SHA-256，95,574,715 字节） |

结论：**库不需要迁移**，新代码可以直接打开它；切换是「换代码」，不是「改数据」。

## 1. 预检（只读，随时可跑）

本机 `python` 默认是 Windows 商店占位符，直接敲 `python` 会失败。用仓库自带的 `maintain.cmd`（解释器解析顺序与 `启动.bat` 一致），或者先 `conda activate JuiLiu`。

```powershell
cd E:\Zero9\JuiLiu\JpgPng
.\maintain.cmd inspect .\download_logs\library.sqlite3
```

通过标准：`状态: compatible`、`完整性: quick_check=ok integrity_check=ok 外键冲突=0`。
注意：`-shm` 的 mtime 会变，这是 SQLite 的簿记文件，不是数据变化；主库字节数与 sha256 不变（已逐字节验证）。

## 2. 备份（写目标目录，不写源库）

```powershell
.\maintain.cmd backup .\download_logs\library.sqlite3 .\record_backups --label pre-switch
```

通过标准：`校验: verified=True 对象一致=True 计数一致=True`、`源库: 主文件未变=True 整组未变=True`。
产物：目录内一个自包含的 `library.sqlite3`（在线快照把副本 journal 归一成 delete，还原时**不能**配旧 `-wal`）+ `manifest.json`（清单 v2）。

**清单 v2 才带内容证明**：每次备份都会算一份逻辑指纹（对象定义 + 每表逐行内容），写进清单，并由 `sealed` 与 `logical` 两项分别校验。旧清单（v1）会被判 `manifest_too_old` 且 `ok=False`——它证明不了内容，请重新备份。

## 3. 副本演练（两个不同的问题，别混）

```powershell
.\maintain.cmd verify  <备份目录>                    # 这个备份还信得过吗（封存校验）
.\maintain.cmd verify  <备份目录> --source <源库>     # 顺带回答：和现在的源库一样吗
.\maintain.cmd compare <源库> <副本或备份>            # 这两个库现在内容一样吗
```

- `verify <目录>` 是**恢复门槛**：`sealed`（成员字节对清单）、`logical`（内容指纹对清单）、`healthy`、`compatible` 四项都执行且都通过才 `ok=True`。
- `compare <源> <副本>` 回答另一个问题：`sealed` 与 `source_equivalent` 明确 `not_checked`，`ok` 只表示两侧对象定义与内容相同、健康、当前版本可打开。
- 旧写法 `verify <源> <副本>` 仍可用，等价于 `compare`，会打印弃用提示。

退出码（`verify`）：0 通过 / 2 不一致 / 3 读不出（含缺清单、清单不可读、成员缺失、未知版本）/ 4 参数或路径被拒。

```powershell
.\maintain.cmd scan .\download_logs\library.sqlite3 --root .
```

巡检退出码：0 覆盖完整且全部一致（或库里没有成品）/ 2 有 missing、changed、unreadable、root_unavailable、changed_during_check / **5 没核验完或存在无法核验的行**（带 `原因: 次数` 明细）/ 3 账本读不出来 / 4 参数被拒。
想让「无摘要旧行」不阻塞：加 `--allow-unknown`（只放行 `no_recorded_size`/`no_recorded_digest`，且要求覆盖完整、无异常）。
`scan` 只读：不写库、不动图库文件，唯一副作用是 SQLite 打开 WAL 库时刷新 `-shm` 的 mtime（主库字节与 sha256 不变）。耗时与机器相关，不要当验收阈值。

## 4. 切换（一行）

```powershell
cd E:\Zero9\JuiLiu\JpgPng
git status --porcelain          # 必须是空的
git checkout daily/u1a-w5w6-20261114
```

- 只换代码。`download_logs`、`downl_Pixiv`、`pixiv_config.json`、cookie、`config/` 都是未跟踪/被忽略的业务资料，不会被这次 checkout 触碰。
- 切完先用 `启动.bat` 打开界面，进「核验」页看一眼：批次暂停时会显示「批次已暂停，恢复后才能核验或补下载」，这是设计如此。
- 想先看效果又不想切：双击项目根目录的 `预览新界面.bat`，使用独立的 `.runtime/offline_demo/` 示例资料。
- 旧 W5/W6 批次中的 `preview_panel.cmd` 与播种脚本已随历史批次归档，**仅供审阅，不可直接重放**。其中脚本含旧批次路径和删除/重建预览目录的逻辑，不能用改路径代替日常入口。

## 5. 回退（两件事，顺序无关）

```powershell
cd E:\Zero9\JuiLiu\JpgPng
git checkout fix/a1-ar3-20261112        # 代码回到旧分支 5465e0c
```

只有在你**确实怀疑库被写坏**时才需要还原数据：

1. 关闭 JpgPng 主程序，确认没有下载/整理任务在跑；
2. 把备份目录里的 `library.sqlite3` 复制覆盖回 `JpgPng/download_logs/library.sqlite3`；
3. 如果原路径旁还有 `library.sqlite3-wal` 或 `library.sqlite3-shm`，一并删除（旧事务日志不能配新文件）；
4. 再跑一次 `maintain.cmd inspect`（必须 `compatible`）与 `maintain.cmd verify <备份目录>`（必须 `ok=True`）。

## 6. 切换后建议做的三件事

1. `maintain.cmd scan .\download_logs\library.sqlite3 --root .` —— 确认图库与记录仍然一致；
2. 在「任务」页恢复那个暂停的批次（这是用户决定，不是自动的）；
3. 观察第一批下载：每一页的观察/准备/发布/确认都会写进事件；出问题先看 `runtime_logs/v2`。

## 7. 明确不做的

- 不做自动迁移、不做自动还原、不做后台静默修复；
- 不在批次运行时用 `--mode file_group` 备份（整组复制要求源安静，在线快照可以并发）；
- 不把 `suspended`（批次停住）当成事故，也不因为「记录里没有」就自动补下载；
- 清单摘要只证明「相对清单未被改」，**不是**抗同时篡改库与清单的签名。
