# 整理后独立审计与接续入口（2026-11-14）

被审 HEAD：`4ba891e34c312f049ef933eeb517da1e928b7575`，日常分支 `daily/u1a-w5-w6-20261114`。审计分支：`audit/post-tidy-20261114`。产品源码与上一轮被审 `e4eb6ce` 相同；之后两次提交只更新文档和归档导航。因此目录变化很大，但不能把它计作又一批产品修复。

本轮完成代码边界审查、独立合成反例、全量回归和归档内容复核；没有修改产品、启动真实任务、读取真实图库内容、操作账号、恢复数据库、合并 main 或部署。

## 结论

- 新增 **PA-01（P1）、PA-02（P2）两项产品问题**：备份核验的多个阶段可能拼接不同版本的证据；SQL 名称过滤漏掉合法用户表。
- 新增 **PA-03（P2）一项归档证据问题**：18 批移动前记录的文件 ID、硬链接数全部为 0，不能支撑“身份与硬链接关系保全”的结论。独立 SHA-256 复核通过，不等于发现丢文件。
- 上一轮 **WR-01..07 仍未关闭**，保留原编号。本轮补强 WR-04 的反例：`copy_name` 不属于 sealed 成员时仍可能返回成功。
- 合计待修产品问题 **9 项（2 P1、7 P2）**，另有 PA-03 的证据更正与后续采集器修复。不要把旧发现重新计成新增问题；也不要因为现有测试全绿就关闭这些问题。

详细触发条件和边界见 [FINDINGS](FINDINGS.md)；下一轮按 [DEVELOPMENT_GUIDE](DEVELOPMENT_GUIDE.md)执行，继续沿用[契约 v6](../../optimization/2026-11-14/U1A_MAINTENANCE_CLOSURE_CONTRACT_V6.md)，补齐本轮条件后再开发，不另起一套产品路线。

## 验证

| 范围 | 本轮结果 |
| --- | --- |
| 主体套件，排除两个 Tk 展示文件 | 2,149 项：2,145 通过、4 跳过、0 失败/错误 |
| 两个 Tk 展示文件，各自独立进程 | 9/9 通过 |
| 现有套件合计 | **2,158 项：2,154 通过、4 跳过** |
| 本轮期望安全行为的独立反例 | **9 项：7 失败、2 对照通过**；失败为报告证据，没有放进绿色正式套件 |
| 已跟踪 Python 文件内存编译 | 基线 243 个通过，不生成 pyc |
| 历史归档路径/Git | 58 批目标存在、原址不再存在；73 worktree 注册/HEAD/分支一致，7 个 dirty 状态一致 |
| 有移动前摘要的归档 | 18 批、600,447 文件、20,221,053,327 字节，全量 SHA-256 和大小一致，0 缺失/0 内容不符 |

4 个跳过是本机真实符号链接能力及既有 W 阶段登记限制。核验面板测试已在主体套件内，不能再加一次。主体耗时 658.79 秒，同时存在归档读盘负载；这不是性能基准。

其余 40 批没有本轮搬迁前摘要，只有路径/Git/既有证据核对，**没有宣称逐文件搬迁前后等价**。18 批清单外的 34 个文件全部为迁移后重建的 `.git` 指针；单独以 Git 注册和 HEAD 验证。当前找到 5,800 个多硬链接文件条目，但旧 ID 缺失，不能反推历史链接拓扑未变。现场没有剩余待跟随的重解析点，本轮未重新创建旧合成链接。

## 整理与证据

本轮只增加一个审计包和一个 runtime 批次；不继续搬动、压缩或删除历史目录。

- 恢复 WC 原始反例文件，内容逐字节取自 `0c11b1a`，见 [原件](../2026-11-14-u1a-w5-w6/evidence/test_w5w6_recheck.py)。没有拿修复后的测试代替原始审计证据。
- 恢复原件独立运行 12/12，通过函数 AST 核对与正式承接测试一致；这 12 项是复核，不重复增加既有套件合计。
- 修正契约 v5 的失效相对链接、当前导航、旧索引名说明；补充维护 CLI 使用边界。历史审计原结论和 LOG 历史条目保留。
- [机器结果](evidence/results.json)和[新反例](evidence/test_post_tidy.py)随 Git 留档，不含凭据、真实库、图库和原始运行日志。
- 本地批次：工作区 `.runtime/jpgpng_post_tidy_audit_20261114_01/`；`full01.xml`、`tk_*.xml`、`probe03.xml`、`archive_check.json`、`archive_summary.json`、`baseline_python_sha256.json` 保留原始过程。
- 随 Git 保存的反例最终复跑见 `packaged02.xml`（仍为 7 红/2 绿）；WR-03 改用引擎生成 cursor，避免把 CLI 旧的默认单页行为误写成将来的修复要求。
- 之前的 WR 审计与现场仍保留，见[上一轮报告](../2026-11-14-u1a-w5w6-proof-independent/README.md)。文件身份取值更正由工作区维护记录承接，不改写移动前清单。

## 复现

在隔离工作树使用现有解释器，设置 `PYTHONNOUSERSITE=1`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，使用 `python -s -B -X utf8 -m pytest`。反例的 `PIXIVKEEP_AUDIT_REPO` 可指定被审树；默认自动找当前仓库根。临时路径必须新建，不复用历史 basetemp。

```text
python -s -B -X utf8 -m pytest tests -q -s -p no:cacheprovider --ignore=tests/test_collection_presentation.py --ignore=tests/test_source_presentation.py --basetemp=<新目录> --junitxml=<新报告.xml>
python -s -B -X utf8 -m pytest docs/audits/2026-11-14-post-tidy/evidence/test_post_tidy.py -q -s -p no:cacheprovider --basetemp=<另一新目录> --junitxml=<反例报告.xml>
```

两个 Tk 文件逐进程运行，使用当前共享环境的 `TCL_LIBRARY`/`TK_LIBRARY`。不要将反例 7 红描述为项目正式套件退化；它们揭示当前正式测试尚未覆盖的行为。
