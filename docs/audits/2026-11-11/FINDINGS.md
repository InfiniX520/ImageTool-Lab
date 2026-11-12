# 问题明细与复现依据

日期：2026-11-11。基线、功能矩阵和选型结论见 [主报告](README.md)，执行边界见 [验证说明](VALIDATION.md)。这里区分实现缺陷、默认策略、能力边界与依赖公告，不把它们混成一个“漏洞数量”。

## 1. 分级和使用方法

高优先级表示可能导致凭据暴露，或与本用户最重要的原图/收藏完整性要求直接冲突；中优先级表示条件性错误、恢复/边界问题或应补足的核验；低优先级/信息项表示局部加固、维护或证据不足。**优先级不是 CVSS 分数，也不是实际触发概率。** 依赖维护者的公告等级另行保留，不能直接映射为本机已受攻击。

“已复现”仅表示文中合成输入确实经过指定代码后产生该结果。PixivUtil2 的探针执行从固定源码提取的 AST 函数/方法及替身依赖，未启动完整应用。源码引用的行号以固定快照或本轮 JpgPng 基线为准。

独立探针代码：[test_audit_probes.py](../../../../.runtime/jpgpng_audit_20261111_01/test_audit_probes.py)；观察结果：[probes04_observations.json](../../../../.runtime/jpgpng_audit_20261111_01/probes04_observations.json)。探针编号 G01、P07、J04 等是测试编号，与本文问题编号分开使用。

## 2. JpgPng

### J-01：跨任务复用收据没有重新确认磁盘文件

**优先级：高；类别：库完整性缺陷；证据：静态确认 + 受控复现（J04，两个输入）。**

`DeliveryRepository._find_reusable_artifact()` 根据数据库中的 `structure_checked`、大小、摘要、输出位置及质量策略寻找其他任务的 artifact。`_artifact_is_valid()` 验证的是记录字段；返回前没有重新打开记录指向的磁盘文件。调用层据此复用页交付，不安排新的下载。

本轮复现步骤：

1. 在临时资料根创建真实 LibraryRepository，以实际 PageDeliveryExecutor 交付测试 PNG。
2. 结束来源并结算批次，保留完成收据。
3. 分别删除 PNG，或将其替换成等长非图字节。
4. 在同一临时库创建新任务，接受同一作品页。
5. 结果为没有新的待交付页；缺失/被替换的文件没有修复，旧收据仍被复用。

这是“初次交付可靠”与“长期库仍然完整”的区别。已有 SHA-256 不是无效设计，但没有在这次复用中检查文件，就不能发现后续变化。对经常新增画师、长期续跑的收藏库，这一缺口应先于更多美化或功能扩张处理。

建议：引入明确的复用核验策略和 artifact 状态。至少检查路径、非链接普通文件、存在与大小；严格模式比较保存摘要，并将缺失/变化转为待修复。检查在 I/O 任务中执行，避免持有数据库写事务等待全文件哈希。文件检查与真正使用之间仍存在竞争窗口，不能再把一次 stat 包装成永久可信。

源码：[delivery_repository.py](../../../core/delivery_repository.py) L1245 起 `_find_reusable_artifact`；[library_repository.py](../../../core/library_repository.py) 复用与任务计数路径。**本轮未修复，也未扫描真实下载库，不能推断实际受影响文件数量。**

### J-02：图像结构证据不等于完整像素解码

**优先级：中；类别：已知校验边界；证据：静态确认。**

`inspect_image()` 对 PNG 做较深入的 CRC/压缩数据结构检查，对 JPEG/GIF/WebP 的证据主要是容器、标记、尺寸等结构。代码注释明确写出 JPEG/GIF/WebP 未完整像素解码；不支持的动画 PNG/WebP 保持未验证状态。

因此可说“通过当前结构策略并保存了摘要”，不能说“所有图都已完整解码且无任何像素错误”。PNG 的坏图拒绝探针 J01 只证明该样本，不证明所有格式的所有畸形输入均被拒绝。

建议：保留证据层级命名；若确实需要完整解码，评估受限进程/时间/像素预算的解码核验，并把新增依赖和成本写清。不要为了比较而立即安装大套图像工具。

源码：[image_validation.py](../../../core/image_validation.py) L375 起及各格式解析器。

### J-03：类型覆盖和“收全”含义仍受限制

**优先级：需求相关；类别：功能边界；证据：静态确认。**

新主线没有与 gallery-dl/PixivUtil2 同等的 Ugoira 动图流程，也没有完整小说支持。关注适配器分开读取 illust/manga/ugoira，是发现和分类来源的措施，不能推导所有类型都已具备交付器。

此外，当前关注适配器依赖 App API 返回的可见集合。gallery-dl 对部分受限作品另有网页 AJAX ID 对照，说明成熟工具已经处理过额外接口差异。即使 JpgPng 的 offset 检查更严格，也不能证明它返回的历史集合比其他项目更全。

建议：任务结果明确列出未支持、无权访问、接口不可见和筛选跳过；把“在当前来源与范围内完成”与“画师全部作品永久收全”分开。若动图进入需求，单独设计原始帧、时序和容器的质量规则。

源码：[pixiv_follow_source.py](../../../core/pixiv_follow_source.py)、[candidate_filter.py](../../../core/candidate_filter.py)、[page_planner.py](../../../core/page_planner.py)；上游对照：[pixiv.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/extractor/pixiv.py) L439—523。

### J-04：关注整理工具已交付，但完整产品流程尚未完成

**优先级：需求相关；类别：交付边界/运行限制；证据：源码与历史记录。**

organizer 有多图取样、审阅交接、计划、网站写回、读回和补偿，不是空壳；但它没有自动接入视觉模型。实际看图判断依赖操作者，E1-3 分类小样和 E1-4 分批整理仍未完成。

Windows UI Automation + Chrome DevTools 的桥接依赖可聚焦窗口、界面结构及浏览器状态。浏览器升级或布局变化可能破坏操作；历史技术小样成功不能代替长批运行验证。这是现有架构限制，不是本轮观察到真实账号写错。

建议：继续保持下载无预览、整理按需看多图；先做分类规则和少量样本，再扩大写入。若后续换浏览器连接方式，应保留计划、身份核对、读回和补偿协议。

源码：[organizer/web.py](../../../organizer/web.py)、[organizer/plans.py](../../../organizer/plans.py)、[organizer/samples.py](../../../organizer/samples.py)；说明：[ORGANIZER_CLI.md](../../../ORGANIZER_CLI.md)。

### J-05：旧新实现共存，环境和交付复现仍有维护债务

**优先级：中；类别：维护性；证据：静态确认。**

`main.py` 默认进入新版，`--legacy` 明确保留旧版；本轮没有把旧版存在的所有宽松行为当成新版默认。与此同时，两套实现、历史兼容和共用 requirements 确实增加维护面。requirements 使用下限范围，不能复现唯一依赖集合；某些工具配置注释仍引用早期测试数。根目录未发现 LICENSE/COPYING，若将来公开分发应先明确授权和所复用组件的许可。

建议：以实际默认调用链区分当前/历史模块，逐步收敛直接依赖，保存可重建环境信息。不要因文档过时把已交付功能误判为未实现，也不要把工作树上的本地修改说成正式稳定发布。

源码：[main.py](../../../main.py)、[collection_desktop.py](../../../core/collection_desktop.py)、[requirements.txt](../../../requirements.txt)、[pyproject.toml](../../../pyproject.toml)。

## 3. gallery-dl

### G-01：原图失败后默认允许较小图 fallback

**优先级：高（针对本用户质量策略）；类别：可配置默认策略；证据：静态确认 + G01。**

Pixiv `_fallback_image()` 会产生 `img-master`、`master1200` 等 URL；DownloadJob 默认读取 `fallback=True`。首选原图下载失败后可以继续尝试这些 URL，并按成功文件处理。

这不是 gallery-dl “不能下载原图”，也不意味着每次都会降级。正常原图成功不经过该回退；通用下载器选择尽量取得可用内容是可解释的默认取舍，但不符合本用户“原图失败就说明失败”的要求。

建议：明确设 `extractor.pixiv.fallback=false`。故障验收应模拟原图失败、小图可用，确认仍登记原图失败；不能只核对正常样本的分辨率。若动图也要求高质量，另审 `ugoira`，不是只关静态图 fallback 就完成。

源码：[Pixiv `_fallback_image`](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/extractor/pixiv.py) L352；[job.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/job.py) L490 起及 initialize；[官方选项](https://gdl-org.github.io/docs/configuration.html#extractor-fallback)。v1.32.19 五文件静态补查也确认该默认策略，未对该稳定版做全部探针。

### G-02：部分续传未严格检查 206 起点和源一致性

**优先级：高（条件性数据错误）；类别：续传缺陷；证据：静态确认 + G03。**

受控样本：临时 `.part` 已有 `AAAA`，本地偏移为 4。下载器发出 `Range: bytes=4-`；替身服务器回 `206`、`Content-Range: bytes 0-3/8` 和 `BBBB`。代码接受响应并形成 `AAAABBBB`，返回成功；请求没有 If-Range。

正确续传应验证服务端响应从请求偏移开始，并在已保存前缀与远端资源之间建立一致性。正常服务器通常遵守 Range，所以本探针不表示日常文件全部拼坏；但源变化、错误 CDN/代理或异常 206 会暴露该缺口。仅有总大小恰好相符无法排除两段来自不同版本。

建议：校验 Content-Range 的 start/end/total 与实际响应长度；用强 ETag/If-Range 等验证器或重新下载策略处理源变化。严格配置中可临时放弃跨进程字节续传、从头下载，代价是流量和时间；这仍不能代替图像及发布验证。

源码：[downloader/http.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/downloader/http.py) L187、L219—227、L387 附近。探针只用临时文件及合成响应，未访问外网。

### G-03：自定义目录模板的 `..` 段缺少输出根约束

**优先级：中，条件性安全风险；类别：路径约束；证据：静态确认 + G02。**

当目录模板使用单独远端字段，例如 `directory=["{title}"]`，字段为 `..` 时，生成路径解析到配置输出根之外。本轮只计算路径，未在根外写入文件。

默认 Pixiv 目录为 `{category}`、`{user[id]} {user[account]}`，包含固定分类和 ID 前缀，不是上述输入；因此不能写成“默认 Pixiv 任意画师可越界”。普通字符替换不等价于对最终解析路径实施根目录约束。

建议：对生成后的完整路径实施解析和根范围约束，拒绝特殊目录段/链接边界；模板中保留可信 ID 前缀能缓解特定输入，但不代替最终边界。涉及合法的用户主动绝对路径功能时，应明确区分可信配置路径与远端字段。

源码：[path.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/path.py) L193、L277—317；公开线索 [issue #9831](https://github.com/mikf/gallery-dl/issues/9831) 与本轮独立探针相互补充。只按本轮快照下结论，不推断后续版本。

### G-04：最终发布失败返回 False，任务仍可能记录成功

**优先级：中；类别：文件/归档一致性缺陷；证据：静态确认 + G08。**

PathFormat.finalize() 在临时文件消失、目标父目录已经存在时可返回 False。DownloadJob.handle_url() 没检查该返回值，继续输出 success 并按默认 file 事件写入 archive。

本轮模拟下载已报告成功但临时文件在发布前消失，使用实际 PathFormat 和任务控制分支，观察到成功回调和 archive.add。触发可能来自外部删除、杀毒软件或竞争条件；没有证明它在通常完整下载中必然发生。已进入 archive 后，再运行可能跳过不存在的最终文件。

建议：finalize 使用明确成功契约；失败必须传播并阻止归档。文件存在检查只是最低限度，严格方案还应持久化临时文件、发布与归档之间的恢复信息。

源码：[path.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/path.py) L397—432；[job.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/job.py) L532 起。

### G-05：异常分页与业务限流缺少同等级总预算约束

**优先级：中；类别：运行边界；证据：静态确认 + G07/G09。**

`PixivAppAPI._pagination()` 按 next_url 更新 offset，没有拒绝不前进的偏移。合成 API 每次回 offset=0，同一条作品在三次调用中被重复生成。探针主动停止，没有继续无限跑。

`_call()` 对 HTTP 200 中的业务 `rate limit` 分支等待 300 秒后 `continue`，不受普通 HTTP 重试次数的同一上限限制。探针用替身等待器观察三次 300 秒参数，并在第四次调用用哨兵退出，没有真实等待或发请求。

这不表示 gallery-dl 完全没有限额、过滤、max-posts 或停止手段。问题是“HTTP retries=N”不能保证所有业务循环最多 N 次，max-posts 也不一定限制没有生成作品的限流等待。

建议：重复游标/重复页检测；来源调用数、时间、逻辑重试及无进展预算分别限制。外部进程超时可作保险，但不应把超时终止写成来源完整完成。

源码：[pixiv.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/extractor/pixiv.py) L1327—1376。

### G-06：默认内容检查和 archive 不构成持久完整性库

**优先级：中；类别：校验/产品语义边界；证据：静态确认 + G04/G05/G06/G10。**

未知长度、只有 PNG 签名而内部损坏的合成内容可以被 HTTP 下载器接受；正常 PNG 字节保持、HTML 拒绝和已知长度短响应拒绝也同时成立。准确说法是“有传输和签名检查，但不是完整图像结构/解码校验”，不能说“什么都不检查”。

DownloadJob 在 archive 命中时先跳过，不核对对应最终文件仍存在；本地存在也可使其跳过。archive 是下载 ID 去重档案，不是含页质量、摘要、任务状态及重核验协议的完整账本。hash 后处理可以建立摘要，但还需另外保存、比对并决定如何修复。

建议：若用作本项目执行层，明确按文件/页核验和作品聚合规则，保留本地库核对流程。JpgPng 同样有旧收据不查磁盘的缺口，不能用此条给自研作单方面加分。

源码：[http.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/downloader/http.py)、[job.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/job.py) L460、[archive.py](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/archive.py)、[hash 后处理](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/postprocessor/hash.py)。

## 4. PixivUtil2

### P-01：OAuth 成功响应中的令牌进入默认调试日志

**优先级：高；类别：敏感日志缺陷；证据：静态确认 + P07。**

`PixivOAuth.login()` 在解析前调用 `logger.debug("%s: %s", status, oauth_response.text)`。成功响应包含 access_token 和 refresh_token。配置默认 `logLevel=DEBUG`、`disableLog=False`，常规日志处理器支持将 DEBUG 写入轮转文件。因此经过该鉴权路径且沿用默认日志配置时，令牌可被持久化。

探针从源码隔离执行真实 login 方法，输入含假令牌字段的 200 响应，截获传给 logger 的原始响应。**没有读取真实日志、Cookie 或令牌，未判断当前机器日志是否已包含凭据。** 暴露主要发生在日志读取、备份、排错分享等环节；这不是向远程未知站点主动外传的证据。

建议：删除整个认证响应体日志，采用白名单状态字段；认证错误体也应按敏感来源处理。临时把日志级别改到 INFO 可减少此 debug 行落盘，但不是永久修复，也不能清掉既有日志。检查与处置真实敏感日志应另定范围，不能在审计报告里复制其内容。

源码：[PixivOAuth.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/common/PixivOAuth.py) L209—241，尤其 L228；[PixivConfig.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/common/PixivConfig.py) L71、L79；[PixivHelper.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/common/PixivHelper.py) 日志创建部分。

### P-02：默认图像验证关闭，已有文件判断可能接受损坏内容

**优先级：高/中，依赖具体收藏要求；类别：默认策略及完整性边界；证据：静态确认 + P01/P02/P05/P06。**

确认的行为：

- `verifyImage=False`、`alwaysCheckFileSize=False` 默认成立。存在文件可能先直接跳过；`checkLastModified=True` 的相同时间戳分支也可以早于后续大小检查。
- `check_file_exists()` 在大小相同时可跳过，与内容是否相同无关；远端大小未知时也可跳过已有文件。
- AST 隔离的底层下载函数在 Content-Length=-1 时会把非图片测试字节从 `.pixiv` 发布为最终文件；当已知长度不足时会报错并处理临时文件。这些探针不是整个作品流程的端到端结论。
- 可选 Pillow `img.load()` 路径启用了 `LOAD_TRUNCATED_IMAGES=True`。启用 verifyImage 也不能保证拒绝所有截断图；本机未安装 Pillow，本轮没有实际执行解码器。

建议：严格模式不以“文件存在/等大小/等时间”作为验证完成；关闭宽松截断并区分传输、结构和解码结果。只把 alwaysCheckFileSize 改 True 不足以补完所有早返回路径，应实际验证组合条件。

必须同时保留其优点：`PixivImageHandler.process_image()` 等路径在按 DB 跳过前调用 `cleanupFileExists()`，清理已丢失文件的记录。因此不能声称它和另外两者一样，在所有情况下都忽略磁盘缺失。

源码：[PixivConfig.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/common/PixivConfig.py) L102、L214—226；[PixivDownloadHandler.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/handler/PixivDownloadHandler.py) L70—90、L219—228；[PixivHelper.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/common/PixivHelper.py) L757—896；[PixivImageHandler.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/handler/PixivImageHandler.py)；[PixivDBManager.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/PixivDBManager.py) L1344 起。

### P-03：自定义清理表达式用 eval，本地配置必须可信

**优先级：中（信任边界）；类别：配置执行能力；证据：静态确认 + P03。**

自定义 CleanUpRe 配置进入 eval。探针输入仅调用记录标记的无害表达式，确认执行表达式；没有系统命令、文件破坏或外联。

这意味着不能把来自陌生人的配置当作纯数据导入。但本轮未发现远端标题直接进入该 eval 的证据，不能称为默认远程代码执行。gallery-dl 的 Python 过滤/表达式、exec 后处理同样需要可信本地配置，评价标准应一致。

建议：配置分发明确可执行边界；清理规则若本意是正则数据，改成解析受限结构而非任意 Python。用户明确配置执行后处理是功能，隐蔽执行未知代码才是另一类问题。

源码：[PixivHelper.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/common/PixivHelper.py) L1593 起；对照 [gallery-dl exec 后处理](../../../../references/pixiv_downloaders/2026-11-11/gallery-dl/gallery_dl/postprocessor/exec.py)。

### P-04：文件名清理辅助函数不等于根目录约束

**优先级：低/中（条件未完全建立）；类别：局部加固；证据：静态确认 + P04。**

对 `sanitize_filename("../outside.png", root)` 的 Windows 路径计算没有强制最终结果留在 root 内；未进行越界写入。调用者对画师/标题等还会做路径分隔符替换，正常元数据进入实际命名模板的条件不同。

因此本条只确认辅助函数不提供强根约束，**没有复现默认远端标题导致越界的端到端链路**。不能给它与 G-03 相同的默认可达性结论，更不能据此称其具有任意文件写入漏洞。

建议：最终写入边界实施解析路径约束，审查用户主动配置子目录和元数据字段的关系。自定义配置、绝对路径和特殊目录段分别处理。

源码：[PixivHelper.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/common/PixivHelper.py) L125—190 及 make_filename。

### P-05：随包旧 DLL 需明确必要性，尚未证明实际可达或恶意

**优先级：信息项；类别：供应链待澄清；证据：文件元数据和全局引用搜索。**

快照包含 `freeimage-3.15.4-win32.dll`，约 2.66 MiB，版本资源为 FreeImage 3.15.4.0，Windows 签名状态 NotSigned。本轮未加载该 DLL，源码搜索没有确认当前活动 Python 路径直接绑定它。

旧版本、无签名都值得记录，但不直接证明恶意、存在某一具体可利用 CVE，或当前图片下载会执行它。pyexiv2 可选 XMP 功能是另外的组件，不应混淆。

建议：在真正部署/打包前确认该 DLL 的用途、来源、许可证和必要性；若当前路径不需要，不把它额外引入运行目录。本轮保留原快照，不删除证据、不反汇编、不做恶意样本执行。

元数据：[dll_metadata.json](../../../../.runtime/jpgpng_audit_20261111_01/dll_metadata.json)。

## 5. 依赖与共用环境

### D-01：当前共用环境的 urllib3 和 certifi 有已公开修复项

**优先级：高，建议单独维护；类别：依赖公告匹配；证据：本地已安装版本元数据 + OSV + 维护者公告。**

本轮只查询公开包名和版本，没有上传源码、配置或凭据。共用环境的 urllib3 为 **2.7.0**，certifi 为 **2026.7.22**。

| 公告 | 受影响范围/修复版本 | 触发条件与本项目适用性 |
| --- | --- | --- |
| [GHSA-8988-9cw3-xx77](https://github.com/urllib3/urllib3/security/advisories/GHSA-8988-9cw3-xx77)，CVE-2026-97687 | urllib3 `>=1.26.0,<2.8.0`；修复 2.8.0 | HTTPS 代理的 TLS 配置可能被忽略/覆盖。需相关 HTTPS 代理和 TLS 配置路径；本轮没读取真实代理设置，不能判定已触发 |
| [GHSA-gh4c-6fx4-qh6g](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g)，CVE-2026-97688 | `>=2.6.2,<2.8.0`；修复 2.8.0 | 分块 Deflate 响应的流式解压可无进展循环。JpgPng 新元数据路径限制编码并自行有界处理 gzip，图片请求 identity，降低相应路径可达性；未对所有旧/外部依赖调用逐一证明不可达 |
| [GHSA-vxq7-64xx-v4gw](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw)，CVE-2026-97689 | `>=1.10.3,<2.8.0`；修复 2.8.0 | 恶意分块响应可使 chunk-size 行无限缓冲。风险在 HTTP 分块解析层，应用响应体大小限制不能自动证明已覆盖它 |
| [GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) | 受影响根证书集合包含在旧版本；修复 2026.10.18 | emSign 中间证书密钥受损背景下移除根证书。需实际使用此 CA 包及相应攻击条件；不是 certifi 在主动泄露信息 |

维护者的公告等级分别包含 High、Moderate、High、Critical；这些是组件公告等级。OSV 中的 PYSEC 与 GHSA/CVE 是同一事件的别名，本表是四项，不是八项。

这些风险直接关系到当前使用该环境的 JpgPng。如果 gallery-dl 使用同一解释器，也会继承相应传输依赖；不能记为 gallery-dl 独有缺陷。PixivUtil2 锁文件的 urllib3=2.8.0、certifi=2026.10.19 在本次查询中没有命中上述公告，不能把共享环境结果错贴到其锁文件。

建议：另开可回滚的共用环境维护批次，核对修复版、依赖约束和其他在用项目回归；不要仅修改 requirements 下限后宣称本机已修复。本轮未安装、升级或卸载任何包，也未对机器进行漏洞利用验证。

### D-02：PixivUtil2 锁文件含受重定向 SSRF 公告影响的 curl-cffi

**优先级：部署前处理；类别：锁文件版本公告；证据：静态锁文件 + OSV + 维护者公告。**

uv.lock 固定 `curl-cffi==0.13.0`，匹配 [GHSA-qw2m-4pqf-rmpp / CVE-2026-33752](https://github.com/lexiforest/curl_cffi/security/advisories/GHSA-qw2m-4pqf-rmpp)。维护者说明不可信 URL 经自动重定向可访问内部地址，修复正式版本为 0.15.0。

本机没有安装该依赖，本轮没有执行它。PixivUtil2 中确认的直接 curl_cffi 调用涉及 FANBOX 页面；实际利用还需攻击者控制可达 URL/重定向链及调用路径。**不能推断普通 Pixiv 图片下载已被利用，更不是本机当前感染证明。** requirements 是版本范围，按范围新解析的环境可能不同于 uv.lock；两种部署方式必须分开评价。

建议：部署前更新并验证锁文件；对不可信目标及重定向目的地限制网络范围。不要靠“模拟浏览器 TLS 指纹”推导出网络目的地安全。

源码：[uv.lock](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/uv.lock)、[PixivBrowserFactory.py](../../../../references/pixiv_downloaders/2026-11-11/PixivUtil2/common/PixivBrowserFactory.py) L1226 附近。

## 6. 正常行为、未确认事项和排除项

### 6.0 三者共有的本地信任边界

三者的本地配置/缓存都不能当作安全凭据保险箱：JpgPng 使用本机授权文件，gallery-dl 配置和 token 缓存、PixivUtil2 配置/相关会话也有敏感内容的本地保存路径。源码未显示默认对这些应用数据实施统一的系统密钥库加密。明文保存本身是本机工具常见取舍，风险取决于文件访问、备份和分享范围；不能仅凭“在本地”推断没有泄漏风险。

gallery-dl 的 exec/表达式及可选转换，PixivUtil2 的自定义表达式、转换和后处理，JpgPng 的浏览器桥接/本地入口均按操作者权限运行。三者都没有提供操作系统级恶意输入沙箱。报告中的受限测试措施属于本轮审计工具，不能归为其中某项目的运行安全能力。

本轮没有读取真实凭据来检查 ACL 或既有日志，不虚构凭据已经泄漏；也没有将下载目录、备份、浏览器资料作为可清理缓存处理。

### 6.1 已保留的正常行为

- gallery-dl 正常 PNG 字节不变，拒绝 HTML 和已知长度短响应；多页、archive、流式下载和 `.part` 不是纸面功能。
- PixivUtil2 已知长度短响应在底层被拒绝，实际有数据库、页记录、缺失文件清理、GUI 和下载名单；不能因局部缺陷抹去这些能力。
- JpgPng 的坏 PNG、父目录路径和不前进分页被拒绝，交付/恢复等选定回归通过；但这不冲销 J-01。

### 6.2 不作肯定判断的事项

| 事项 | 本轮边界 |
| --- | --- |
| 三者真实账号可用性和长期限流 | 没有新的真实账号联测，不能判断当前节点下谁最稳定 |
| 下载速度、峰值内存、CPU 排名 | 没有同条件基准 |
| PixivUtil2 GUI 完整体验 | 只定位源码，没有启动 GUI 或安装依赖 |
| 所有图片/动画格式的解码安全 | 没有完整模糊测试、全部解码器审计或原生库逆向 |
| 依赖全树绝对安全 | 52 项版本查询覆盖已声明锁文件和当前运行闭包，不覆盖所有可选依赖和未来版本；零命中不代表安全证明 |
| 上游公开 issue 的修复状态 | 以当前核对状态及固定源码为准；closed 可能是 stale/not planned，不一律视作已修复 |
| 上游二进制与源码一致 | 本轮下载的是源码，不是发布 EXE/安装包复现构建 |

### 6.3 排除的误导性线索

公开检索中涉及 ArchiveBox 的命令执行公告不能直接记成 gallery-dl 自身 CVE；旧 issue 已在 changelog 中说明修复的情形，不能当作当前确认缺陷；无关镜像、搜索摘要中的推荐排名和论坛经验不作为质量结论。没有发现足够证据证明这三份源码存在故意窃取本机资料的行为，但本轮审计也不是恶意代码不存在的数学证明。

以上问题均保留固定版本、触发条件和复核证据。修复、部署或迁移时应重新验证相关分支，不能把本报告当作以后所有版本永久有效的认证。
