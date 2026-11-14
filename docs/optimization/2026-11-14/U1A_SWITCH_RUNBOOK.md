# 切换与回退说明：W5/W6 与 post-tidy 修复轮

**当前状态（2026-11-14 post-tidy 修复轮）**：PA-01/PA-02/PA-03 与 WR-01..07 的修复与验收结果以[维护记录](../../maintenance/2026-11-14_jpgpng_post_tidy_closure.md)和最新审计为准。**真实库恢复仍未执行，本页只描述人工协议，不是执行许可。** 日常离线预览使用项目内 `预览新界面.bat`；归档中的播种/预览脚本不可直接重放。

下面 §0–§2、§4、§6–§7 的数字来自本批实测（见[维护记录](../../maintenance/2026-11-14_jpgpng_u1a_w5w6_proof.md)）；§3 与 §5 已按修订 A1/A4 改写。

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

清单同时要经得起严格校验：`copy_name` 必须是真正封存的数据库主成员，`bytes`/`sha256` 类型与长度不合规、成员重名或大小写别名、`-shm` 被封存，都按 `manifest_*` 错误处理（退出 3），不会被当成「大概是好的」。

## 3. 副本演练（三个不同的问题，别混）

```powershell
.\maintain.cmd verify  <备份目录>                    # 这个备份还信得过吗（封存校验）
.\maintain.cmd verify  <备份目录> --source <源库>     # 顺带回答：和现在的源库一样吗
.\maintain.cmd compare <源库> <副本或备份>            # 这两个库现在内容一样吗
```

- `verify <目录>` 是**恢复门槛**：`sealed`（成员字节对清单）、`logical`（内容指纹对清单）、`healthy`、`compatible` 四项都执行且都通过才 `ok=True`。
- 四项事实来自**同一份隔离副本**：核验先在备份目录同级（不可写时回退系统临时目录）建一个唯一私有目录，把成员文件组边复制边哈希，复制前后比对 `(卷身份, 文件 ID, 大小, mtime)`；复制期间被改写判 `changed_during_check`（退出 2）。逻辑指纹与健康检查在这份副本上共用一个只读连接、一个读事务。副本用完即删，删不掉会报 `verify_cleanup_failed` 并保留路径。
- **证明作用域**：报告描述的是那份副本；核验返回之后源目录再被改写，不在本次保证内。要回答「源现在还是不是这个内容」，用 `--source`（`source_equivalent`），它是独立问题，不影响 `ok`。
- **代价**：`verify` 需要一次完整复制（峰值占用 ≤ 备份目录大小，结束时释放）。这是换取「四项事实同一代次」的显式代价，不要去关掉它。
- `compare <源> <副本>` 回答另一个问题：`sealed` 与 `source_equivalent` 明确 `not_checked`，`ok` 只表示两侧对象定义与内容相同、健康、当前版本可打开。
- 旧写法 `verify <源> <副本>` 仍可用，等价于 `compare`，会打印弃用提示。

退出码（`verify`）：0 通过 / 2 不一致（含 `changed_during_check`、`logical_mismatch`） / 3 读不出（含缺清单、清单不可读、成员缺失、未知版本、清单语义不合规、两个位置的核验工作区都建不出来） / 4 参数或路径被拒。

```powershell
.\maintain.cmd scan .\download_logs\library.sqlite3 --root .
```

巡检默认走**全程**：一直翻页到 EOF 或预算/取消为止，只给一次最终结论；`--limit` 是页大小，不是全库总量。`--page` 才只跑单页。外部输出根从 `config/output_roots_v1.json` 只读加载（与桌面一致）；映射文件存在但读不了、版本未知或根非法时整条命令以 `mapping_unreadable` 停止（退出 3），**不会**猜根、不会 `mkdir`、也不会回退项目根。

巡检退出码：0 全程覆盖完整且全部一致（或库里没有成品）/ 2 有 missing、changed、unreadable、root_unavailable、changed_during_check / **5 没核验完、被预算或取消中断、或存在无法核验的行**（带 `原因: 次数` 明细）/ 3 账本或根映射读不出来 / 4 参数被拒。
裸 `--after` 是**续跑**，不是全库完成：即使游标已在末尾，也只报 `scope=continuation`、`coverage=partial`、退出 5。
想让「无摘要旧行」不阻塞：加 `--allow-unknown`（只放行 `no_recorded_size`/`no_recorded_digest`，且要求覆盖完整、无异常）。
`scan` 只读：不写库、不动图库文件，唯一副作用是 SQLite 打开 WAL 库时刷新 `-shm` 的 mtime（主库字节与 sha256 不变）。耗时与机器相关，不要当验收阈值。

## 4. 切换（一行）

```powershell
cd E:\Zero9\JuiLiu\JpgPng
git status --porcelain          # 必须是空的
git checkout daily/u1a-w5-w6-20261114
```

- 只换代码。`download_logs`、`downl_Pixiv`、`pixiv_config.json`、cookie、`config/` 都是未跟踪/被忽略的业务资料，不会被这次 checkout 触碰。
- 切完先用 `启动.bat` 打开界面，进「核验」页看一眼：批次暂停时会显示「批次已暂停，恢复后才能核验或补下载」，这是设计如此。
- 想先看效果又不想切：双击项目根目录的 `预览新界面.bat`，使用独立的 `.runtime/offline_demo/` 示例资料。
- 旧 W5/W6 批次中的 `preview_panel.cmd` 与播种脚本已随历史批次归档，**仅供审阅，不可直接重放**。

## 5. 恢复（人工协议，七步；真实库仍不执行）

> 旧版把「复制覆盖主库 + 删除 WAL/SHM」当作恢复步骤，**已废止，不可照做**：那只在「旧 WAL 一定属于旧库且一定可以丢」的假设下成立，而真实故障里往往还连着进程、还留着旧文件组。

1. **关闭并验证**：关闭 JpgPng 主程序与整理/下载任务；确认没有进程还开着库（进程列表里没有 JpgPng，且 `library.sqlite3-wal` 不再增长）。只关任务不等于关进程。
2. **先验备份**：`maintain.cmd verify <备份目录>` 必须 `ok=True`；记下 `方式`（`online`/`file_group`）与成员清单。`ok=False` 时停手，不要继续。
3. **准备暂存组**：把备份成员复制到同卷的暂存目录（例如 `record_backups\restore_stage_<时间戳>`），在暂存目录里再验一次（`verify` 或 `compare` 对照备份的封存指纹）。`online` 备份是自包含单文件；`file_group` 备份是整组，必须整组准备。
4. **保留当前组**：把当前 `library.sqlite3`、`library.sqlite3-wal`、`library.sqlite3-shm`（存在哪个算哪个）**整组移入**同卷独立保留目录 `record_backups\restore_rollback_<时间戳>`。是移动，不删除、不覆盖、不改名混放。
5. **放入匹配组**：把暂存组放进 `download_logs`。`online` 的不要带任何 `-wal`/`-shm`；`file_group` 的整组一起放。**禁止新主库配旧 WAL**——那是把两个不同状态拼在一起，比不恢复更糟。
6. **重开对照**：`maintain.cmd inspect`（必须 `compatible`）+ `maintain.cmd compare <恢复后的库> <备份目录里的库>`（`内容一致=True`）。再开界面确认任务/批次/交付记录可读。
7. **失败退回**：任一步失败，把第 4 步的保留组整组移回原位，删掉第 5 步放入的组（只删本次放入的），并保留报告。不要在未退回的情况下继续试。

只有合成副本演练通过（WAL 残留、两种模式、连接未关被检出、恢复后逻辑指纹相等、保留组未被删除、非法组合被拒）才谈真实库恢复；真实库恢复仍需用户明确指令并另批执行。

## 6. 切换后建议做的三件事

1. `maintain.cmd scan .\download_logs\library.sqlite3 --root .` —— 确认图库与记录仍然一致；
2. 在「任务」页恢复那个暂停的批次（这是用户决定，不是自动的）；
3. 观察第一批下载：每一页的观察/准备/发布/确认都会写进事件；出问题先看 `runtime_logs/v2`。

## 7. 明确不做的

- 不做自动迁移、不做自动还原、不做后台静默修复；
- 不在批次运行时用 `--mode file_group` 备份（整组复制要求源安静，在线快照可以并发）；
- 不把 `suspended`（批次停住）当成事故，也不因为「记录里没有」就自动补下载；
- 清单摘要只证明「相对清单未被改」，**不是**抗同时篡改库与清单的签名；
- 不把 `verify` 的隔离副本当成「已修复所有备份」的证明：它证明的是这一次读到的这份副本。
