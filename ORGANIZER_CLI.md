# Pixiv 关注整理 CLI

这是供 AI 操作者使用的独立工具：读取你关注的画师，按作品集取样，实际看图后提出分组，再把经过核对的改动写到 Pixiv。你在网站上查看和调整结果，无需自己填写几百位画师的分类。

本阶段交付工具。十来位画师的分类小样、分类口径校准和大批量整理分别属于 E1-3、E1-4；工具发布不表示这些工作已经完成。下载器入口和界面保持独立，普通下载不会获取整理用样图。

## 启动与账号

在项目根目录的 PowerShell 运行 `./organize.cmd`。使用已有 Python、requests、Windows PowerShell 和普通 Chrome，新增运行依赖为零，不需要 Node、独立浏览器或另一套环境。所有业务路径相对项目根定位，与打开终端的位置无关；迁移时一起保存 `organizer_data/`，并调整项目或上级 `.venv-path` 指针。

```powershell
.\organize.cmd --help
.\organize.cmd status
.\organize.cmd browser
.\organize.cmd connect
```

`--help`、`status`、`browser` 不向 Pixiv 发请求。首次 `connect` 核对你已打开的 Pixiv 页面及会员身份，绑定 `organizer_data/`；后续每个网络命令都重新核对当前网页账号。多个 Chrome 窗口时，将 `browser` 返回的进程编号作为全局参数，例如 `--browser-pid 12345`；重启浏览器后重新读取编号，不沿用历史 PID。

首版在已登录的 Chrome 页面内执行站点请求，通过 Windows UI Automation 操作 DevTools 控制台，Cookie 和 CSRF 值留在浏览器，不导出登录资料。需要保留一个当前标签为 Pixiv 的普通 Chrome 窗口；命令运行会切到该窗口，期间避免切页、输入或关闭开发者工具。窗口无法聚焦、账号变化、登录失效或验证限流都会保存进度后停止，不自动重新登录。网页结构或浏览器版本变化可能需要更新适配器；这是依赖网页的本地 CLI，不是 Pixiv 官方接口。

运行网络命令前关闭新版和旧版下载器。新版通过同一个进程锁强制互斥；旧版入口不使用该锁，需先手动关闭。离线查看、导出不占下载器锁。

## 盘点、取样和查看

全局参数放在子命令前。每次命令都有限额：默认 80 请求、32 MiB 图片、600 秒，串行执行，请求至少间隔 0.5 秒。可降低上限；耗尽会保存断点，不能把这一批结束当成全量完成。

```powershell
.\organize.cmd --max-requests 30 snapshot
.\organize.cmd list snapshot
.\organize.cmd show snapshot SNAPSHOT_ID
.\organize.cmd --max-requests 30 snapshot --resume SNAPSHOT_ID
.\organize.cmd sample --snapshot SNAPSHOT_ID --artist ARTIST_ID --count 16
.\organize.cmd list sample
.\organize.cmd show sample SAMPLE_ID
.\organize.cmd sample --snapshot SNAPSHOT_ID --artist ARTIST_ID --resume SAMPLE_ID
.\organize.cmd sample-page --sample SAMPLE_ID --work WORK_ID --page 1
```

盘点分页读取公开和非公开关注，以及各分组的成员；按画师 ID 关联，记录总数、首屏锚点、分页范围和未知部分。只有盘点完成才设置 `memberships_complete=true`；限额中断时缺少成员字段不等于该画师没有分组。续跑发现关注或分组变化时停止，重新生成快照。网站没有提供原子快照，`non_atomic=true` 始终保留，写回前仍重新核对现状。

默认从作品 ID 的新旧范围均匀选 16 个不同作品，不把 ID 间隔说成均匀年份分布；每张记录实际作品日期。只取判断需要的中等尺寸，保存原响应字节、格式校验和 SHA-256。多页默认先取第 0 页，`sample-page` 的页号从 0 开始，用来按需补看。动图等暂不支持类型单列，不能记作已查看。作品或样图加载失败就停止，已有记录可续用。

`show sample` 给出图片在 `organizer_data/` 内的 `relative_path`。AI 必须实际打开多张图片，再填写审阅记录；文字标签、样图下载成功或生成一份 JSON 都不等于看过。默认需要至少 12 个不同作品；画师作品不足 12 时按实际可用数量说明。混合风格、不可访问或证据不足时设 `needs_more=true`，不会进入写回计划。补看同一作品的十几页不能冒充十几个作品。

## 审阅、计划与网站写回

AI 根据实际查看结果填写审阅 JSON。下面的 ID 和摘要只是字段示例，必须替换成 `show sample` 中的真实证据；也必须在 `viewed` 中列齐实际看过的图片。

```json
{
  "sample_id": "SAMPLE_ID",
  "observer": "本次实际看图的 AI 操作者",
  "rules_version": "pilot-v1",
  "viewed": [{"illust_id": "123456", "page": 0, "sha256": "实际文件摘要"}],
  "rationale": "基于多件作品写出的简短判断依据",
  "coverage_note": "实际覆盖的时期、作品数量，以及还需补看的内容",
  "proposed_groups": ["拟定分组"],
  "needs_more": true
}
```

```powershell
.\organize.cmd review --file organizer_data/review-input.json --attest-viewed
.\organize.cmd plan --review REVIEW_ID
.\organize.cmd apply --plan PLAN_ID
.\organize.cmd apply --plan PLAN_ID --execute --max-artists 5 --max-writes 10
```

`review` 核对图片摘要并保存不可变证据，`--attest-viewed` 是操作者的明确声明，工具本身没有接入视觉模型。`needs_more=false` 且证据数量满足要求后才可生成计划；不足时继续补看，不能为了通过检查伪造声明。

一个计划最多 20 位画师，可重复 `--review`。默认叠加分组、保留原组；只有明确需要替换时才给 `plan` 加 `--replace`。每位最多 5 组，名称按 JavaScript UTF-16 长度不超过 20；这些按已核验网页约束检查，不声称服务端全部极限已测试。

`apply` 默认只展示清单、零网络写入；`--execute` 才执行。每项操作先核对当前成员和目录，持久保存意图，再提交一次请求、独立读回。最后还要核对全部目标画师，才记录 `website_verified`。批次数量是本次最多处理数，不改变计划范围。工具没有取消关注、改变公开性、删除整个组或组改名命令。

## 中断、网站手动修改与撤销

```powershell
.\organize.cmd status
.\organize.cmd list operation
.\organize.cmd show operation OPERATION_ID
.\organize.cmd reconcile --plan PLAN_ID
.\organize.cmd undo-plan --plan PLAN_ID
.\organize.cmd apply --plan UNDO_PLAN_ID
.\organize.cmd apply --plan UNDO_PLAN_ID --execute --max-artists 5 --max-writes 10
```

断网、关机或窗口切走可能留下 `intent` / `unknown`。先 `reconcile` 只读网站核对，不重发 POST；已生效记 `confirmed`，未生效记 `not_applied`，与预期不符记 `conflict`。含后两种状态的旧计划不能直接重试，应重新读现状和生成计划。重跑 `apply` 遇待确认操作时也只做核对，本次不继续提交新写入。

你在网站改组、改名、删组或调整公开性后，旧计划会停下，保留网站现状，不凭旧快照自动恢复。检测依靠操作前后的实际读取，无法把网站接口变成数据库事务，也不能彻底排除恰好同时发生的修改。

`undo-plan` 只为本工具已确认的改动生成补偿计划，核对网站现状后仍需显式执行。部分完成的任务只补偿已完成部分，不改动没执行过的对象。若中途有人工修改，会拒绝覆盖；这不是整站快照回滚。

## 资料、导出与维护

| 位置 | 用途 |
| --- | --- |
| `organizer_data/organizer.sqlite3` | 账号绑定、快照、审阅、计划、操作意图、读回和作业记录，必须保留 |
| `organizer_data/samples/` | 看图样本，与正式原图库分开；摘要与审阅证据相互对应 |
| `organizer_data/.bridge/` | 请求期间的临时业务脚本，用后移除，不含 Cookie/Token |
| 工作区 `.runtime/jpgpng_organizer_cli_20261024_01/` | 开发验收、隔离账号测试日志和候选基线 |

样本最多占 512 MiB、20,000 个目录项；单图最多 8 MiB，每位最多保存 40 张。到限停止，不自动删除仍被证据引用的图片。需要腾空间时先关闭工具，连同数据库完整备份这一轮资料，再安排可恢复的样本归档和新一轮目录；不要把 `organizer_data/` 当通用缓存删除，也不要只复制正在使用的 SQLite 主文件而漏掉 WAL。

样图落盘后、回执提交前发生异常，可能留下 `sample_file_without_receipt` 或 `.part`。工具会停止而不冒认成功。操作者应先核对并可恢复归档该单个孤立文件，再重试样本；不要盲删整个目录。已记录图片摘要变化同样停止。

```powershell
.\organize.cmd export plan PLAN_ID --output organizer_data/plan-review.json
.\organize.cmd export snapshot SNAPSHOT_ID --output organizer_data/follow-snapshot.json
```

导出不会覆盖已有文件。SQLite 记录属于你的账号资料，虽不保存认证凭据，也不要公开上传。错误输出只含经过限制的错误码；`status` / `show job` 记录预算和断点。硬关机可能让上个 job 仍显示 `running`，它不证明进程仍在运行，先核对进程与操作记录。

浏览器请求数按本工具保守预留与可观察响应分别记录，浏览器超时后可能无法确定实际发送数；因此不能用缺少响应证明没有请求。网页自身加载资源不计入 CLI 预算。浏览器桥接每次有窗口定位和通信成本，本阶段以少量、串行、可核对的批次为目标；没有声称大批整理吞吐已通过验收。
