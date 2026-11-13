<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ImageTool-Lab** (6236 symbols, 15032 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ImageTool-Lab/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ImageTool-Lab/clusters` | All functional areas |
| `gitnexus://repo/ImageTool-Lab/processes` | All execution flows |
| `gitnexus://repo/ImageTool-Lab/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## 版本保全与接续

- 当前版本留档方法见 `docs/VERSION_CONTROL.md`。先读 `git status`、当前分支、`TASK_PLAN.json` 和最新审计，再修改代码；已知失败不得说成已通过。
- 2026-11-12 的审计后、修复前基线标签为 `checkpoint-2026-11-12-a1-audit`；之后已存在但未验收的 schema 3 草稿另存 `checkpoint-2026-11-12-a1-wip`；2026-11-13 AR4 关闭/schema 5 的离线验收点为 `checkpoint-2026-11-13-a1-ar4`。旧审计统计不能套到后一个版本。检查点标签保留不移动，不作为稳定发布声明。
- 后续功能/修复从已保存基线建立独立工作分支。每个有意义的工作包完成后核对差异、运行相应测试、更新进度并提交；用户已要求版本留档，相关提交可继续保存到现有私有远端，不改变可见性。
- 不用 `reset --hard`、`clean -fd`、强制推送、重写已发布历史或删除检查点来整理现场。恢复旧版先用新的 detached worktree 或导出目录，不覆盖当前工作树或业务资料。
- 提交前检查新增文件与暂存区；账号、Cookie、Token、本机配置、下载名单、数据库、图库、organizer_data、archive_store、runtime 和备份不入库。`config/default_config.yaml` 只作为无凭据默认模板保留。忽略规则不能代替对已跟踪文件的检查。
- Git 负责源码、测试、设计和版本记录；业务数据的备份与 schema 迁移回退独立处理，不能以源码标签代替数据备份。
