# 审计范围、检索记录与复核说明

日期：2026-11-11。对应 [主报告](README.md) 与 [问题明细](FINDINGS.md)。本文件说明本轮实际做了什么、没有做什么，供后续复核和接续。

## 1. 输入与可追溯基线

| 输入 | 标识/位置 |
| --- | --- |
| JpgPng | 当前本地工作树；[本轮文件清单](../../../../.runtime/jpgpng_audit_20261111_01/inventory.json)中逐文件 SHA-256 标识。不能用 Git HEAD 代替大量已有本地修改 |
| gallery-dl | 官方 Codeberg `789a5085d900042090a197e8c40e5b94533f877a`，源码自报 1.33.0-dev |
| PixivUtil2 | 官方 GitHub `46bb910cd60c213c9199d939859670a044450239` |
| 下载包与 Git 对象核验 | [源码获取清单](../../../../references/pixiv_downloaders/2026-11-11/README.md)、[verification.json](../../../../references/pixiv_downloaders/2026-11-11/verification.json)及各 manifest/provenance |
| 原包摘要 | gallery ZIP：`b3e53f86e9fe237562808c48c8929d93eb10db3bbb7d89a8cc1a011d083e1c30`；PixivUtil2 ZIP：`d6e0b2b7bc343c1076f4551e311bc17b6afc265e126a71e6f0d33b6b2d6d918b` |
| 旧版静态补查 | 前轮比较中的 gallery-dl v1.32.19 五个核心文件；不属于本轮完整动态测试版本 |

源码获取批次的 `audit_status=not_started` 保留其当时事实，不改写成当时已审计。本轮状态由 [audit_result.json](../../../../.runtime/jpgpng_audit_20261111_01/audit_result.json)单独记录。

## 2. 文件与搜索覆盖

### 2.1 自动清点

| 项目 | 清点文件 | Python 文件 | Python 物理行 | 范围说明 |
| --- | ---: | ---: | ---: | --- |
| JpgPng | 178 | 163 | 49,560 | 根目录声明扩展名/requirements，core、gui、utils、organizer、tests 源码及相应说明/资源 |
| gallery-dl | 833 | 796 | 147,945 | 固定快照全部文件，包括其他站点适配器、测试、文档和配置示例 |
| PixivUtil2 | 152 | 40 | 17,275 | 固定快照全部文件，包括测试、GUI、锁文件、示例与 DLL |

物理行包含注释、测试、旧实现和与 Pixiv 无关的站点，不作为复杂度或质量分数。JpgPng 的 scripts 当前仅有维护说明，没有遗漏的在用脚本源码；`.git`、索引、历史归档、构建缓存和业务资料不属于运行源码搜索范围。

对全部清点文件计算 SHA-256；所有 Python 文件用 AST 做语法与 import 清点；可解码的 `.py/.ps1/.js/.sh/.bat/.cmd/.toml/.yml/.yaml` 做风险模式搜索。Markdown/RST/锁文件另按配置、功能、来源和依赖目的阅读，未宣称对二进制做文本扫描。

搜索类别：

| 类别 | 关键词/模式示例 | 后续人工判断 |
| --- | --- | --- |
| 系统执行 | subprocess、Popen、os.system、shell=True | 是用户显式功能、浏览器桥接、构建流程还是不可信输入 |
| 动态求值 | eval、exec、compile、pickle、marshal | `re.compile` 等也会命中，不能按命中数判漏洞 |
| 原生库 | ctypes、CDLL、WinDLL、FreeImage、pyexiv2 | 实际加载路径和可选组件，不按文件名猜测执行 |
| 网络安全 | verify、SSL、http/https、重定向、代理 | TLS 默认与关闭选项、目标主机、响应大小、编码 |
| 凭据和日志 | token、Cookie、Authorization、debug/dump | 是否真实流入日志，默认日志级别和输出位置 |
| 文件边界 | extractall、replace、rename、unlink、链接、commonpath | 远端名称、配置路径、最终解析范围及发布失败 |
| 交付与完整性 | fsync、SHA-256、Range、ETag、长度、截断图 | 证据怎样参与决策；不是只查有没有某个词 |
| 请求与分页预算 | next_url、offset、retry、429、checkpoint、budget | 是否前进、是否有总次数/时间/无进展边界 |

完整搜索命中只保存文件、行号和类别，不输出业务配置；记录见 [whole_source_scan.json](../../../../.runtime/jpgpng_audit_20261111_01/whole_source_scan.json)。清点脚本见 [inventory.py](../../../../.runtime/jpgpng_audit_20261111_01/inventory.py)。**不要直接重跑它覆盖本轮基线和 git_status_before；新的审计应另建批次。**

### 2.2 人工深入审查路径

| 项目/层 | 主要文件 | 实际关注点 |
| --- | --- | --- |
| JpgPng 入口/界面连接 | main.py、core/collection_desktop.py、collection_runtime.py、gui/collection_app.py、collection_controller.py、collection_sources.py | 默认/legacy、工作线程、真实按钮功能与主线归属 |
| JpgPng 来源 | pixiv_follow_source.py、pixiv_query_source.py、follow_collection.py、query_collection.py、candidate_filter.py、query_parameters.py | 历史/新作、筛选、分页、身份、信息缺失与停止原因 |
| JpgPng 交付 | page_planner.py、page_delivery.py、image_validation.py、library_paths.py、task_contracts.py | 原图 URL、路径、范围、ETag、临时文件、摘要和恢复 |
| JpgPng 账本 | library_repository.py、delivery_repository.py、follow_repository.py、library_queries.py | 收据复用、页/作品计数、跨批复用和事务边界 |
| JpgPng 网络/日志 | api_transport.py、diagnostic_logging.py、request_pacing.py、authorization_settings.py、相关旧下载模块 | 新主线边界、脱敏、预算；识别旧实现，避免混淆默认 |
| JpgPng 整理 | organizer/cli.py、web.py、samples.py、plans.py、browser_console.ps1、page_bridge.js | 多图证据、身份、网站写回/核对/补偿、占用浏览器的限制 |
| gallery-dl Pixiv | extractor/pixiv.py、相关固定配置说明 | 作品类型、多页、关注、原图、fallback、AJAX 补漏、OAuth、分页/限流 |
| gallery-dl 共用层 | downloader/http.py、job.py、path.py、archive.py、cache.py、config.py、extractor/common.py | Range、大小、签名、路径、归档、令牌存储、请求重试 |
| gallery-dl 扩展/分发 | postprocessor/hash.py、exec.py、ugoira.py、oauth.py、update.py、setup.py、相关测试 | 可选后处理/命令执行、动画、安装与许可边界；本轮未调用更新器 |
| PixivUtil2 配置/鉴权 | common/PixivConfig.py、PixivBrowserFactory.py、PixivOAuth.py、PixivOAuthBrowser.py、PixivHelper.py | 默认校验、日志、TLS、Cookie、动态配置、路径 |
| PixivUtil2 下载/记录 | handler/PixivDownloadHandler.py、PixivImageHandler.py、model/PixivImage.py、PixivDBManager.py | 临时文件、长度、Pillow、跳过、缺失文件清理、页记录 |
| PixivUtil2 用户入口/依赖 | PixivUtil2.py、PixivUtilGUI.py、README、changelog、requirements、pyproject、uv.lock、DLL 元数据 | 能力与文档一致性、GUI 存在、原生组件和环境复现 |

阅读路径不等于每个文件每行的形式化审计，更不等于所有可选功能的运行验收。机器可读覆盖说明见 [audit_coverage.json](../../../../.runtime/jpgpng_audit_20261111_01/audit_coverage.json)。

## 3. 公开资料与全网检索方法

### 3.1 检索主题

围绕三项目交叉检索：官方来源与迁移说明、当前提交与发布、Pixiv/关注/多页/动画功能、原图 fallback、resume/Range/Content-Range、archive/数据库/丢失文件、OAuth/日志/权限、path traversal、依赖 GHSA/CVE、GUI 与运行环境。对具体发现再回到固定源码和受控探针确认。

代表性查询包括 `gallery-dl pixiv original fallback range security`、`site:github.com/mikf/gallery-dl path traversal`、`site:github.com/Nandaka/PixivUtil2 verifyImage token security issues`、`PixivUtil2 FreeImage`，以及按实际包名、版本和公告 ID 的交叉查询。部分宽泛查询返回论坛、镜像和无关产品，只作发现线索，没有用作技术结论。

全网搜索不可能保证索引覆盖所有页面；本轮的“全量”落实为固定源码的完整清点、声明范围内全量模式搜索、关键调用链深读和有边界的复现。**不把搜索引擎无结果当作不存在问题的证明，也不声称审完互联网所有信息。**

### 3.2 采用的来源

| 来源 | 用途 |
| --- | --- |
| [gallery-dl 官方 Codeberg](https://codeberg.org/mikf/gallery-dl)、[GitHub 官方说明](https://github.com/mikf/gallery-dl) | 核实活跃开发迁移、源码与发布归属 |
| [gallery-dl 配置文档](https://gdl-org.github.io/docs/configuration.html)、[CLI 选项](https://gdl-org.github.io/docs/options.html)、[站点支持](https://gdl-org.github.io/docs/supportedsites.html) | 对照选项含义；关键行为以固定源码为准 |
| [gallery-dl issue #9831](https://github.com/mikf/gallery-dl/issues/9831) | 路径问题公开线索；另做本轮独立验证 |
| [PixivUtil2 官方源码](https://github.com/Nandaka/PixivUtil2/tree/46bb910cd60c213c9199d939859670a044450239)、[README](https://github.com/Nandaka/PixivUtil2/blob/46bb910cd60c213c9199d939859670a044450239/README.md)、[发布](https://github.com/Nandaka/PixivUtil2/releases) | 功能、文档、版本和配置；不采用第三方下载站 |
| [PixivUtil2 issue #1206](https://github.com/Nandaka/PixivUtil2/issues/1206)、[changelog](https://github.com/Nandaka/PixivUtil2/blob/46bb910cd60c213c9199d939859670a044450239/changelog.txt) | 识别历史问题和修复记录，避免直接算作当前缺陷 |
| [OSV](https://osv.dev/)、[OSV API](https://google.github.io/osv.dev/api/) | 精确包名/版本公告匹配，再核对维护者公告 |
| urllib3、certifi、curl_cffi 官方安全公告 | 受影响版本、修复版本、攻击条件，链接见 FINDINGS 的 D-01/D-02 |
| 本地源码与历史冻结验收 | JpgPng 已实现能力和已知限制，明确区分历史与本轮 |

Codeberg 某些网页入口被网页读取工具的 robots 限制拦截；来源获取使用了此前核验的官方 HTTPS/API 快照。报告不假装网页工具读到了被拦截页面，也不因此改用来历不明的镜像。没有执行网页或仓库文档提供的安装命令。

排除依据：搜索摘要不够；匿名排行不够；closed issue 不等于修复；高 star/大代码量不等于指定条件正确；商店或发布页不证明二进制等同源码；不同软件使用同名依赖不构成 CVE 转移。

## 4. 依赖核对

第一轮查询 PixivUtil2 uv.lock 的 29 个注册包条目和当前共用环境 10 个重点包，共 39 条；随后从已安装 distribution metadata 解析 JpgPng 六个直接依赖的活动传递依赖及 extras，共 24 个包，gallery-dl 最小 requests 链为 5 个。复用前轮已查询的同名同版本，另外补查 13 条，**总计 52 条查询，去除锁文件和本机重叠项后覆盖 50 个不同包版本组合**。

| 证据 | 内容 |
| --- | --- |
| [dependency_queries.json](../../../../.runtime/jpgpng_audit_20261111_01/dependency_queries.json) | 初始 39 项包名、版本和来源 |
| [dependency_osv_raw.json](../../../../.runtime/jpgpng_audit_20261111_01/dependency_osv_raw.json)、[summary](../../../../.runtime/jpgpng_audit_20261111_01/dependency_osv_summary.json) | OSV 原始响应与合并说明 |
| [dependency_closure.json](../../../../.runtime/jpgpng_audit_20261111_01/dependency_closure.json) | 当前标记环境下的运行依赖闭包、依赖边和版本；不 import 目标包 |
| [补充查询](../../../../.runtime/jpgpng_audit_20261111_01/dependency_closure_additional_queries.json)、[补充结果](../../../../.runtime/jpgpng_audit_20261111_01/dependency_closure_osv_summary.json) | 新增 13 项，未匹配新增公告 |
| `GHSA-*.json` | 五个独立 GHSA 的 OSV 明细与维护者原文链接；别名不重复计数 |

只向官方公开漏洞接口发送公共包名和版本；未上传本地源码、环境路径、配置、用户数据。没有使用自动安装/扫描脚本，没有调用 uv/pip 去解析和下载新环境。

局限：这不是对所有 gallery-dl 站点的可选依赖、FFmpeg/浏览器/操作系统和所有解码器的完整供应链审计；当前安装元数据不证明包文件未被篡改，也没有重建所有 wheel。零匹配只表示查询时数据库未返回相应已知记录。PixivUtil2 锁文件未安装，不能据此声称其完整运行环境已通过。

## 5. 受控离线执行

### 5.1 执行措施

固定源码保持只读，执行副本在本批次 `work/`；无真实配置、账号令牌、下载库或整理资料。解释器使用工作区既有 Python，保留 `PYTHONNOUSERSITE=1`、`-s -B -X utf8`，关闭 pytest 自动插件发现和项目外临时写入。

[offline_runner.py](../../../../.runtime/jpgpng_audit_20261111_01/offline_runner.py) 安装 Python audit hook，拒绝外部 DNS/网络、子进程、批次外写入以及读取批次外的在用工作区文件；允许本机 loopback 供上游 HTTP 测试使用。**不是 OS 沙箱，不能用于执行任意恶意原生代码。** 先阅读所选测试再执行，未载入旧 DLL、未运行安装器、未调用三方 CLI 主入口。

PixivUtil2 因缺少 Pillow、mechanize、demjson3、curl_cffi 等完整依赖，不启动应用。只从 AST 提取已审阅方法，传入简单替身；未伪装成安装成功或全应用验收。

### 5.2 最终保留的有效执行

| 组 | 范围 | 原始结果 |
| --- | --- | --- |
| JpgPng | test_image_validation、test_page_planner、test_candidate_filter、test_page_delivery、test_delivery_repository、test_follow_collection、test_organizer | [jpgpng_tests03.xml](../../../../.runtime/jpgpng_audit_20261111_01/jpgpng_tests03.xml)：208 通过；[日志](../../../../.runtime/jpgpng_audit_20261111_01/jpgpng_tests03.log)记录 3 项排除 |
| gallery-dl | test_path、test_job、test_config、test_downloader | [gallery_tests02.xml](../../../../.runtime/jpgpng_audit_20261111_01/gallery_tests02.xml)：120 项中 112 通过、8 失败；[日志](../../../../.runtime/jpgpng_audit_20261111_01/gallery_tests02.log)保留断言差异 |
| 独立探针 | G01—G10，P01—P07，J01—J04（J04 两种变动） | [probes04.xml](../../../../.runtime/jpgpng_audit_20261111_01/probes04.xml)：22 个断言符合预期；[观察 JSON](../../../../.runtime/jpgpng_audit_20261111_01/probes04_observations.json)给出具体行为 |

JpgPng 排除 `actual_process_exit` 的两个参数化用例，以及 `cli_help_and_status` 的一个实际子进程用例；本轮守卫禁止子进程。历史确有实际退出测试，但未在此重跑。gallery-dl 八项失败涉及 Windows `\\` 与测试固定 `/` 预期；没有修改上游测试、假称全部通过，也没有因此推导八个产品功能故障。

### 5.3 独立探针对照

| 探针 | 输入/控制 | 观察 |
| --- | --- | --- |
| G01 | 原图 fallback 生成器 | 包含 master1200 较小图 |
| G02 | 自定义单字段目录，值 `..` | 路径可解析到根外；无越界写入 |
| G03 | 已有 4 字节，请求从 4 续传，206 声称从 0 | 被接受并拼接；无 If-Range |
| G04 | 未知长度，签名像 PNG、内部损坏 | HTTP 层接受 |
| G05 | HTML 响应 | 拒绝，正常防护对照 |
| G06 | 已知长度短响应 | 拒绝，正常防护对照 |
| G07 | next offset 一直为 0 | 同页生成三次，探针主动停止 |
| G08 | 发布前临时文件消失 | finalize=False 仍可进入成功/归档路径 |
| G09 | HTTP 200、业务 rate limit | 三次逻辑等待参数为 300；第四次哨兵停止，无真实睡眠/网络 |
| G10 | 正常 PNG | 字节保持 |
| P01 | 大小相同但不检查内容 | 跳过 |
| P02 | 远端大小未知、已有文件 | 跳过 |
| P03 | 自定义清理配置无害表达式 | 表达式被执行；无系统命令 |
| P04 | sanitize_filename 的父目录输入 | 函数不提供根约束；未证明实际远端输入链 |
| P05 | 未知长度、非图字节 | 底层发布；非全应用探针 |
| P06 | 已知长度不足 | 底层拒绝 |
| P07 | OAuth 200 假响应 | 响应令牌字段进入 debug logger |
| J01 | 未知长度坏 PNG | 拒绝 |
| J02 | PathRef 父目录分量 | 拒绝 |
| J03 | next offset 不前进 | 拒绝 |
| J04 × 2 | 完成后删除/等长替换，再建新任务 | 仍复用数据库收据 |

这些样本不是统计抽样成功率；未把一组专门探测失败的用例应用给某项目，再用测试通过数量给它排名。

### 5.4 初始执行错误没有隐藏

初始 runner 阻止 pytest 的 NUL 日志文件写入、对 Windows 扩展路径前缀处理不全；后续修正的是本轮测试工具。JpgPng 一项子进程测试被 guard 拦截后，按预先声明的禁止子进程范围排除。独立探针初轮存在不完整替身、真实 API 名称/方法对齐等工具错误，调整工具后保留最终四号结果。

原始 `*01.log`、`*02.log`、`probes03*` 和执行回执全部保留，不算产品缺陷、不回写原项目测试，也不把旧错误结果覆盖成成功。

### 5.5 复核方式

每次最终执行的 `*_execution.json` 记录完整参数和 guard 设置，优先按回执在**新的批次**复核。以下只是读取现有结果，不触发下载或改库：

```powershell
$auditRoot = Join-Path (Get-Location) '.runtime/jpgpng_audit_20261111_01'
Get-Content -LiteralPath (Join-Path $auditRoot 'audit_result.json') -Raw
Get-Content -LiteralPath (Join-Path $auditRoot 'probes04_observations.json') -Raw
```

运行目录为 JuiLiu 工作区根；项目内路径都相对定位，没有把用户盘符写入应用配置。不要为了复核直接重跑带相同 junitxml/basetemp 的命令，以免覆盖已完成证据。

## 6. 结束检查与安全边界

收尾验证重新比对 985 个上游文件及 JpgPng 声明范围内生产源码、测试、入口和依赖声明的基线；检查新增 Markdown 的本地链接、UTF-8 无 BOM/LF、JSON 结构。报告入口、LOG、TASK_PLAN 的更新属于本轮预期变化。具体结果写入 [closeout.json](../../../../.runtime/jpgpng_audit_20261111_01/closeout.json)。

没有回退原工作树、提交 Git、改业务配置、扫描或迁移真实下载库、调用 Pixiv 账号接口、改关注分组、安装包或执行原生 DLL。未对业务资料建立新的全盘摘要，因此不使用“本轮逐文件重新证明全部业务数据不变”的说法；措施是把这些位置排除在输入和写入范围之外，并限制测试 I/O。

本轮审计完成不等于所有发现已修复、重构没有遗留缺陷或迁移已批准。之后的修复、部署和联网对照应使用各自独立批次及验收证据。
