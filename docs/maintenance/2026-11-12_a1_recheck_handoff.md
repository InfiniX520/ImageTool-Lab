# A1 复审修复交接单（2026-11-12）

## 当前状态

- 分支：`fix/a1-recheck-f1-f4-20261112`
- HEAD：`231c7e1`；计划格式已升到 v3，scope 根代次与回执交叉校验；C 的 `LibraryBusyError` 已合并并跑全量。
- 产品工作树干净；仅有运行时更新的 `AGENTS.md`/`CLAUDE.md` 不纳入本批提交。
- **不是生产可用，不是真实图库迁移，不是删除/释放授权。**

## 已完成的代码提交

| 提交 | 内容 |
| --- | --- |
| `4fbda0c` | F1/F2 读取凭据、引用内容闭包、分段边界 |
| `4a9c3f3` | F4 范围目录 I/O、页内取消/预算 |
| `cd9f113` | F3 receipt v2、候选→观察→内容代次、identity_key |
| `c1b5306` / `f516bb1` | F6 迁移关闭/忙错误/root_revision |
| `d1e9279` | FR-05 部分计划计数一致 |
| `76ef92d` | O1 load 禁止静默绑定空内容回执 |
| `d42c0e2` | FR-04 非 planned 计划完整协议 |
| `b46daa8` | FR-03 结构化内容冲突 |
| `ef9038f` | FR-07/08/10 checkpoint/流式摘要/读取上限/load busy |
| `eb31dff` | O3 计划头 root_revision 与回执强制对照 |
| `bdfe8c4` | FR-04 缺失 `_strict_object` 导入的 P1 修复 |
| `8a7002e` | C：`LibraryBusyError` 写入者锁竞争类型化，已全量 |
| `1603252` / `4dfd26b` | 批次记录与交接文档 |

## 当前验证

- 全量：**1376 项 / 1373 通过 / 0 失败 / 0 error / 3 skip**（本轮 Tk/Tcl 未复现）。
- F5-v2 在 `6790565`：**576/576**，2,000 文件、12 段、payload 3,109,761 B。
- 当前树 `p100-current-v3`：**2,000 文件通过**，12 段、payload 3,109,779 B、10.51 s。
- 当前树负向：**41 个用例，40 个 required 通过**；唯一 observation 是 O2（未绑定回执 + 伪造清单摘要按设计接受）。
- F6：**61/61**。

## 正在运行（20 分钟截止中）

| 代理 | 任务 | worktree |
| --- | --- | --- |
| `25760354-...` | 当前 HEAD 最终只读终审 | `final_worktree2`（eb31dff） |
| `813a53ce-...` | AR2-04 库级解析结果→计划入口 | `ar204_worktree`（eb31dff） |
| `19d97838-...` | `LibraryRepository.submit` 忙错误 | `busy_worktree`（eb31dff） |

已经在 12 分钟内要求它们停止扩展、交当前状态/最小提交；不要在没有结果时把它们的未完成工作合并。

## 仍未闭合

1. **AR2-04 生产入口不存在**：解析器可用，但产品代码没有调用；B 代理正在做库级入口，仍需合并审查。
2. **FR-02**：生产接线是独立工作包。
3. **O2**：未绑定回执 + 伪造清单摘要按设计接受；回执不是内容验证。
4. FR-09/11 与断电报错、网络盘、真实工具、生产接线未覆盖。
5. 最终独立终审基于 `eb31dff`；若 B/C 产生新提交，需要再次终审。

## 恢复命令

从项目根目录运行：

```powershell
git status --short --branch
git log --oneline -12
$env:PYTHONNOUSERSITE='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest tests -q -p no:cacheprovider --basetemp ..\.runtime\jpgpng_a1_recheck_fix_20261112_01\handoff_full --junitxml ..\.runtime\jpgpng_a1_recheck_fix_20261112_01\handoff_full.xml
```

F5 当前树正链：

```powershell
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 '..\.runtime\jpgpng_a1_recheck_fix_20261112_01\f5_v2_agent\f5_v2_chain.py' --worktree '.' --fixture-root '..\.runtime\jpgpng_a1_recheck_fix_20261112_01\f5_v2_agent\tmp\full\fixture' --run-root '..\.runtime\jpgpng_a1_recheck_fix_20261112_01\f5_v2_agent\tmp\handoff-current' --out '..\.runtime\jpgpng_a1_recheck_fix_20261112_01\f5_v2_agent\results\handoff-current.json' --variant handoff-current --page-size 100 --works 400
```

## 禁止

- 不接生产、不打开真实图库、不使用账号/密码、不上传/删除/释放。
- 不 `reset --hard`、不 `clean -fd`、不强制推送、不覆盖旧 evidence/checkpoint。
