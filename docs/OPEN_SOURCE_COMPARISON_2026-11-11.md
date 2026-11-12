# Pixiv 开源工具选型与 JpgPng 的保留价值

核查日期：2026-11-11。结论来自官方文档、版本元数据和指定版本源码的静态检查；本轮没有安装扩展、运行第三方源码、读取或转交账号凭据，也没有对三个下载器做同条件联网测速。文中的“优先”是选型建议，不是已完成迁移或实机胜出的结论。

单论通用 Pixiv 下载，Powerful Pixiv Downloader 和 gallery-dl 都值得优先验证，覆盖了 JpgPng 很多已有功能。最初进入全面重构前，应先做这轮成熟工具评估；缺少这一步造成了可以避免的重复工作。后续投入应按用户实际收益决定，历史投入不是继续维护全部自研底层的理由。

## 结论与适用场景

| 需求 | 优先候选 | 相对 JpgPng 的收益与限制 |
| --- | --- | --- |
| 用户在网站浏览、筛选、批量下载，尽快日常使用 | Powerful Pixiv Downloader | 复用浏览器登录，原图、多页、关注用户作品、记录、暂停恢复、元数据与动图能力较全；正常安装使用不需 Node。受浏览器下载目录和站点数据存储约束 |
| AI 代操作、文本列表、长期收藏、新增画师补历史、元数据导出 | gallery-dl | CLI、输入列表、命名、过滤、SQLite 去重、部分文件续传和多站点适配已有实现；适合作为候选下载底座。须配置原图失败策略、授权、限额、记录与本地核验 |
| 较深的 Pixiv 专用传统批量工具和数据库管理 | PixivUtil2 | 关注画师、作品列表、SQLite 管理等能力齐全；配置和依赖较多，关键图像校验默认关闭，首选优先级低于前两者 |
| 用户定制的界面、分批账本、失败解释与 AI 多图分组 | 保留 JpgPng 对应部分 | 可以继续利用现有实现；“工具能运行”与分类完成、长期使用可靠是不同事实。适配成熟引擎前仍要验证交付语义 |

对这个用户的具体偏好——平时按关注下载、默认渐进补历史、质量优先、轻量、由 AI 代整理——建议先验证 gallery-dl 的受控配置作为长期下载候选，同时将 Powerful Pixiv Downloader 作为易用性与下载覆盖的对照。迁移后可以保留现有界面与账本，也可以直接使用成熟工具；是否再加管理界面，由实际缺口决定，避免提前再造完整框架。

## 核查范围与版本

| 项目 | 核查版本 | 证据 |
| --- | --- | --- |
| Powerful Pixiv Downloader | v19.6.1，2026-10-29 发布，`2e137d0b45f4bc897f0792994dfa7176884922ce` | [官方版本](https://github.com/xuejianxianzun/PixivBatchDownloader/releases/tag/v19.6.1)、[固定源码](https://github.com/xuejianxianzun/PixivBatchDownloader/tree/2e137d0b45f4bc897f0792994dfa7176884922ce) |
| PPD 官方 Wiki | `e08423590696ba615c73194eb1ae60468de5378a` | [官方文档源码](https://github.com/xuejianxianzun/PBDWiki/tree/e08423590696ba615c73194eb1ae60468de5378a/docs/zh-cn) |
| gallery-dl | 稳定版 v1.32.19，2026-10-31 发布；另参考 2026-11-10 主线 `789a5085d900042090a197e8c40e5b94533f877a` | [主仓库](https://codeberg.org/mikf/gallery-dl)、[稳定版](https://codeberg.org/mikf/gallery-dl/releases/tag/v1.32.19)、[官方配置文档](https://gdl-org.github.io/docs/configuration.html) |
| PixivUtil2 | 主线 `46bb910cd60c213c9199d939859670a044450239`，2026-10-25 提交；API 返回的最新发布为 v20251112 | [官方仓库](https://github.com/Nandaka/PixivUtil2)、[固定源码](https://github.com/Nandaka/PixivUtil2/tree/46bb910cd60c213c9199d939859670a044450239) |
| JpgPng | 当前本地源码及 2026-10-23/24 验收记录 | [README](../README.md)、[重构进度](refactor/REFACTOR_PROGRESS.md)、[CLI 记录](../../docs/maintenance/2026-10-24_jpgpng_organizer_cli.md) |

主线源码不等于稳定发布。本轮另取 gallery-dl v1.32.19 的提取、下载、去重、任务和版本源码，确认下述关注列表、较小图片 fallback、默认启用 fallback 等关键事实同样存在于该稳定版。

GitHub 首页已明确说明 gallery-dl 的活跃开发迁至 Codeberg，当前官方 README 的 Windows 稳定程序入口也指向 Codeberg。不能再把旧 GitHub Releases 称为“唯一当前官方独立 EXE 页”；应沿 [作者 README](https://github.com/mikf/gallery-dl#readme) 提供的链接选择具体版本。网页商店搜索结果存在版本缓存差异，不把搜索摘要的旧版本号当作当天商店实际可安装版本。

## 质量：下载了原图和交付可信是两件事

原图质量至少有四个层次：来源 URL 正确；收到并保存完整响应；多页全部保存并准确登记；发生断网、退出或后续文件变动时能够发现并恢复。保存“原图”并不自动满足后面三项。本地 SHA-256 可建立落盘证据与发现后续变化；没有服务器可信摘要或独立参考时，不能仅靠它证明远端原件绝对正确。

### Powerful Pixiv Downloader

静态图默认 `imageSize: original`。作者说明普通图、小图和方形缩略图均为 Pixiv 生成，下载器不会自行裁剪或转换静态图片。它可以直接从关注页面抓取相应用户的全部作品，并非只能从单一画师或作品列表下载。普通使用只需安装浏览器扩展，Node 是开发构建的需求。[图片尺寸](https://github.com/xuejianxianzun/PBDWiki/blob/e08423590696ba615c73194eb1ae60468de5378a/docs/zh-cn/设置-下载/图片尺寸.md)、[关注页面](https://github.com/xuejianxianzun/PBDWiki/blob/e08423590696ba615c73194eb1ae60468de5378a/docs/zh-cn/按钮-开始抓取/关注页面.md)、[项目 README](https://github.com/xuejianxianzun/PixivBatchDownloader#readme)。

动图默认启用 WebP 且质量为 `lossy`。质量优先时需显式选择 ZIP/Ugoira 或其他经验证的无损方案。APNG 是解码后的无损编码，不表示与来源压缩包字节相同；作者说明 ZIP/Ugoira 会加入动画元数据 JSON，所以也不能把 ZIP 整包摘要等同于服务器原 ZIP。保留动画应同时考虑帧图和时序。[动图说明](https://github.com/xuejianxianzun/PBDWiki/blob/e08423590696ba615c73194eb1ae60468de5378a/docs/zh-cn/设置-下载/动图.md)、[设置默认值](https://github.com/xuejianxianzun/PixivBatchDownloader/blob/2e137d0b45f4bc897f0792994dfa7176884922ce/src/ts/setting/Settings.ts)。

正常 Chrome 路径会等待 `downloads.onChanged` 的 complete/error，并保存按图片页区分的下载记录；不能把它贬为“没有持久记录”。不过作者明确指出：记录只查自己的 IndexedDB，不能据此确认硬盘中的文件仍存在；手工删图后，旧记录仍可能使它跳过。清理网站数据也可能清掉这些记录，应导出备份。[记录说明](https://github.com/xuejianxianzun/PBDWiki/blob/e08423590696ba615c73194eb1ae60468de5378a/docs/zh-cn/设置-下载/下载记录.md)、[保存回执实现](https://github.com/xuejianxianzun/PixivBatchDownloader/blob/2e137d0b45f4bc897f0792994dfa7176884922ce/src/ts/serviceWorker/background.ts)。

其恢复以任务结果和文件状态为主。本轮检查的下载过程先在内存收集响应块，再组装 Blob 交给浏览器保存；恢复整个任务不等于对单个半下载图片保存持久的 HTTP 字节偏移。作者文档也区分“暂停”和“放弃”：后者清除当前恢复数据。[下载实现](https://github.com/xuejianxianzun/PixivBatchDownloader/blob/2e137d0b45f4bc897f0792994dfa7176884922ce/src/ts/download/Download.ts)、[恢复实现](https://github.com/xuejianxianzun/PixivBatchDownloader/blob/2e137d0b45f4bc897f0792994dfa7176884922ce/src/ts/download/Resume.ts)、[下载控制](https://github.com/xuejianxianzun/PBDWiki/blob/e08423590696ba615c73194eb1ae60468de5378a/docs/zh-cn/按钮-下载区域/下载控制.md)。

### gallery-dl

单页和多页的首选 URL 使用原图字段。但 v1.32.19 的 Pixiv `_fallback_image()` 会提供 `_master1200.jpg`，之后还可提供 large/medium 等 URL；任务默认 `fallback=true`。因此原图请求失败后，默认配置可能保存较小图片并登记成功。这符合通用工具尽量取得可用图片的取舍，但不符合本用户“原图失败明确失败”的要求。若选它，应显式设置 `extractor.pixiv.fallback=false`，并用故障样本验证，不仅检查正常成功路径。[稳定版 Pixiv 实现](https://codeberg.org/mikf/gallery-dl/src/tag/v1.32.19/gallery_dl/extractor/pixiv.py)、[任务实现](https://codeberg.org/mikf/gallery-dl/src/tag/v1.32.19/gallery_dl/job.py)、[fallback 配置](https://gdl-org.github.io/docs/configuration.html#extractor-fallback)。

gallery-dl 支持部分文件和 Range 续传，流式写盘，并检查短于声明长度的响应、异常 HTML、文件签名/扩展名等。其 `validate=true` 不等于对所有图片做完整解码或对照服务器摘要；`verify` 还要与 TLS 证书验证区分，不能误当图片完整校验。需要摘要可以配置 hash 后处理，但应保存并实际使用摘要进行核对。[HTTP 下载实现](https://codeberg.org/mikf/gallery-dl/src/tag/v1.32.19/gallery_dl/downloader/http.py)、[HTTP 配置](https://gdl-org.github.io/docs/configuration.html#downloader-http-validate)、[hash 实现](https://codeberg.org/mikf/gallery-dl/src/commit/789a5085d900042090a197e8c40e5b94533f877a/gallery_dl/postprocessor/hash.py)。

它的 `--download-archive` 保存成功文件 ID 到 SQLite，便于重跑跳过。该去重档案不是 JpgPng 的任务、游标、页收据、筛选原因和 prepared 恢复协议的直接替代；也不会因为开启 archive，就自动逐字节复查已下载文件。正常保存文件与写 archive 是分步动作，不应直接声称具有相同的跨文件/数据库交付语义。[archive 说明](https://gdl-org.github.io/docs/configuration.html#extractor-archive)、[archive 实现](https://codeberg.org/mikf/gallery-dl/src/tag/v1.32.19/gallery_dl/archive.py)、[任务实现](https://codeberg.org/mikf/gallery-dl/src/tag/v1.32.19/gallery_dl/job.py)。

动图默认下载含 JPEG 帧的 ZIP；官方另提供 `ugoira: original` 获取更高质量的独立帧。容器是 ZIP 不足以证明帧就是最高质量。静态图与动图必须分开验收。[动图配置](https://gdl-org.github.io/docs/configuration.html#extractor-pixiv-ugoira)。

### PixivUtil2

该项目有关注画师、文本列表、数据库管理和图像/ZIP 校验，但当前 `verifyImage`、`alwaysCheckFileSize` 默认都是 False。更细的检查发现，开启图片校验的实现使用 Pillow `img.load()`，同时开启 `LOAD_TRUNCATED_IMAGES=True`，所以“开了 verifyImage 就一定拒绝所有截断图”也不成立。它仍值得作为对照，但不因历史长、功能多而直接获得质量上的最高优先级。[默认配置](https://github.com/Nandaka/PixivUtil2/blob/46bb910cd60c213c9199d939859670a044450239/common/PixivConfig.py)、[下载校验实现](https://github.com/Nandaka/PixivUtil2/blob/46bb910cd60c213c9199d939859670a044450239/handler/PixivDownloadHandler.py)。

## JpgPng 已有价值与实际缺口

现有页交付实现将原图 URL、长度、部分传输范围、强 ETag、同目录临时文件、结构检查、SHA-256、prepared 记录、发布和恢复相互关联；多页作品只在所选页全部满足交付要求后计完成。这些是可保留的质量和解释能力，而不是“下载原图”这四个字本身带来的优势。可查 [page_delivery.py](../core/page_delivery.py)、[image_validation.py](../core/image_validation.py)、[delivery_repository.py](../core/delivery_repository.py)。

但当前也有明确边界，不能借本次对比粉饰：

- JPEG/GIF/WebP 结构检查并不是完整像素解码，未实现与服务器可信摘要逐件独立对照；动图包等类型未支持，无法与成熟工具的类型覆盖相比。
- `_find_reusable_artifact()` 查询数据库中的已验证收据、大小、摘要及品质条件，返回时未重新打开硬盘文件。跨任务复用可信记录不等于重新确认图片仍存在且未变化。因此“我们会自动发现用户后来手工删掉的每张图”不是当前能力，和上游一样需要专门的本地库核对流程。
- 核心验收记录有 906 项回归、两小时离线长测和有界联网小样，证明相应场景通过；不能据此推断与成熟工具相比速度更快、账号更安全，或全量真实任务绝无缺陷。
- 接口维护和浏览器变化目前主要靠本地维护，缺少成熟项目的用户覆盖与反馈。继续自研所有网站适配会增加持续成本。

AI 整理扩展的保留价值更明确：它要求实际看多幅作品，保存审阅证据，提出分组计划，写到真实账号后读回核对，并处理待确认状态和补偿。三款候选在本次所查文档和核心路径中，没有展示完整的同类流程；这不是“全网绝无类似工具”的证明。PPD 的“给未分类收藏作品加标签”和 PixivUtil2 的“按 Group Id 下载”，不能直接等同为 AI 依据作品集判断并整理关注画师。[CLI 说明](../ORGANIZER_CLI.md)、[审阅代码](../organizer/samples.py)、[写回计划](../organizer/plans.py)。

本地 CLI 还没有自动接入视觉模型：实际看图依赖操作者；仅发布工具，尚未完成 E1-3/E1-4。它通过 Windows UI Automation 操作 Chrome DevTools，需要占用能聚焦的窗口，遇到浏览器布局变化可能失效。这是未来适配应解决的实际短板，不是比直接浏览器扩展更稳的证据。

## 对推荐文案的纠正

1. “零封号风险”“绝不触发风控”“最安全”没有可核实的保证。PPD 作者专门说明大量请求可能导致警告或封禁，且没有固定安全数量。降低并发和延迟是节制请求的手段，不是安全证书。作者经验也不等于 Pixiv 官方承诺。[作者风险说明](https://github.com/xuejianxianzun/PBDWiki/blob/e08423590696ba615c73194eb1ae60468de5378a/docs/zh-cn/账号被封禁的风险.md)。
2. “每天几百张、10—15 分钟”取决于图片大小、页数、网络和请求数量，不能作为完成时间承诺；几百位画师的全部历史可能远超几百张。
3. “100% 无损”应分静态原图、动画帧质量、像素保持和原始文件字节保持。APNG 无损并不意味着恢复作者上传源文件或保留原压缩包。
4. 扩展有商店审核和权限边界，但不意味着完全安全。核查源码 manifest 的权限包括 downloads、storage、unlimitedStorage、webRequest 和 declarativeNetRequestWithHostAccess，站点范围为 Pixiv、Pixivision、pximg；这是源码声明，未证明商店二进制与源码逐字节一致，也没有完成整个第三方依赖的安全审计。[manifest](https://github.com/xuejianxianzun/PixivBatchDownloader/blob/2e137d0b45f4bc897f0792994dfa7176884922ce/src/manifest.json)、[Chrome 最低数据使用要求](https://developer.chrome.com/docs/webstore/program-policies/limited-use)。
5. gallery-dl 是成熟通用工具；“全球最权威”“行业标准霸主”是评价性宣传，不能作为具体功能和质量证据。维护来源按作者当前 README 核对，而不是通过推荐中的搜索跳转或第三方下载站。

## 若下一阶段验证迁移，验收什么

本轮完成选型研究，以下是尚未执行的具体对照方案：

1. 固定 gallery-dl 稳定版本，在项目之外的独立测试位置使用明确配置。账号信息留在受保护本机配置，不能放到命令行、报告或公开仓库。安装动作另按工作区安装守则处理。
2. 先做无账号、受控响应的故障验证：原图失败不能降级成功；长度不符、HTML 伪图、无长度截断、Range 不匹配、源 ETag 变化、同尺寸替换、落盘失败均需正确停止或登记失败。
3. 做一组小而有代表性的真实作品对照：JPEG、PNG、大图、至少两个多页作品；如果纳入动图，独立核验帧图与时序。固定相同原图 URL/版本时比摘要；若来源本身变动，保存证据而非武断判为下载器问题。
4. 下载中断、重启、重复执行、删除或替换本地文件，再看结果、页数、去重和修复；原库只读对照，测试输出隔离。旧 archive 不能直接冒充新版已验证收据。
5. 加入新画师、变化关注顺序、翻页期间新增作品、筛选信息缺失，以及有限批次耗尽：输出必须区分已完成、过滤、未知、待重试和未覆盖，不能把短页或预算结束当作历史收全。
6. 在同一网络、同一范围和请求间隔下记录时间、峰值内存、请求数、文件/页数和失败恢复。静态阅读和项目测试数量不能替代这个对照。

通过后优先接入或直接采用成熟下载工具，保留确有用途的界面、质量核对和 AI 整理部分。没有通过的关键条件应先补适配；不把大规模改写包装成“接一个 CLI 很简单”。当前正式启动入口和业务数据在选型期间继续保留。

本轮公开文本证据与摘要在工作区 `.runtime/jpgpng_comparison_20261111_01/`，包括源码版本、53 个主线文本文件及 5 个稳定版文件；只保存文本，没有下载 CRX/EXE、安装依赖或运行其中代码。搜索返回的镜像、第三方扩展统计站和无关页面未作为结论依据。
