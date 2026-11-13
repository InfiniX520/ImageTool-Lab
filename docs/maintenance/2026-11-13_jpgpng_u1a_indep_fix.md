# U1-A 独立审计问题修复（UA-01..UA-11）

- 日期：2026-11-13
- 批次：`JPGPNG-U1A-INDEP-FIX-20261113-01`
- 分支：`fix/a1-u1a-indep-20261113`
- 审计基线：`8b1c751`（产品）/ `892a140`（含审计文档与 14 项反例）
- 契约基线：`8d8c62c`（[U1A_STATE_CONTRACT.md](../optimization/2026-11-13/U1A_STATE_CONTRACT.md)）
- 产品最终提交：`bff7c61`（本记录所在的文档提交紧随其后）
- 环境：`D:\Miniconda3\envs\JuiLiu\python.exe`，`-s -B -X utf8`、`PYTHONNOUSERSITE=1`；只用合成 PNG、临时 SQLite、假 HTTP。未访问真实图库、生产数据库、账号或网络。

## 1. 结果

| 验证 | 数量 | 结果 |
| --- | --- | --- |
| 非 Tk 全量 | 1,671 | 1,667 通过 / 0 失败 / 0 错误 / 4 跳过 |
| Tk 展示（单独运行） | 9 | 9/9 通过 |
| 合计 | 1,680 | 1,676 通过 / 4 跳过 / 0 失败 |
| 审计 14 项独立反例 | 14 | 14/14 通过 |
| 新增与带入测试 | 86 | 全部通过（含 14 项原样带入） |

4 个跳过沿用既有边界：3 个真实符号链接（本机环境无法创建）、1 个 W 阶段集成登记。Tk 需要显式设置 `TCL_LIBRARY`/`TK_LIBRARY`（共享环境未预设；缺省会报 `ttk/utils.tcl` 找不到，属环境变量问题，不是代码问题）。

## 2. 修复对照

| 编号 | 现象 | 修法 | 主要提交 |
| --- | --- | --- | --- |
| UA-01 | 完成与预检交错，完好图片被降为 `artifact_unknown` 待核验 | 预检返回 `PreflightSnapshot`（页/代次/交付/观察）；写者只在同页同代次同 artifact 有记录时复用；代次提前 → `RetryPreflight`，事务外有界重试，耗尽 `PreflightUnavailable`，零写入 | `0cbe6b2` |
| UA-02 | 读取意图跨任务，取消/恢复只处理部分任务 | 全局 `IntentKey`；记录 EXCLUDED 时跨 job 取消 `CANCELLABLE_STATES`；claim 复查；recovery 三动作前复查当前意图 | `0cbe6b2`、`84f0374` |
| UA-03 | 根不可用仍返回 held 可派发，executor 重建根 | `PathResolver.root_state`；`_new_stage` 只在 `available` 时创建；`missing_bound` 抛 `root_unavailable`；pending 遇不可用观察 → PARK 为 `needs_verification` | `ad69fa6`、`bb8a9bd` |
| UA-04 | 选中旧版本匹配，却按首个 changed 作决定 | 决定/展示/事件统一使用实际选中的观察；无匹配时给稳定原因 | `bb8a9bd` |
| UA-05 | carry 链外任务 hold 后无法恢复 | 恢复获得受控取代权（只退役 NV，记 `recovery_owner_transfer`）；不抢 transferring/validating/prepared | `bb8a9bd` |
| UA-06 | repair/park 无与 reuse 对称的观察校验 | 三动作共用 `_validate_recovery_observation`（策略/时效/根版本/artifact 绑定/page_key/expected-old）；拒绝零写入 | `bb8a9bd` |
| UA-07 | 哈希占用唯一控制循环，暂停/查询被堵 | 独立 `collection-observer` 与 `collection-control` 线程；入队时记录 `admitted_running`；settle 计入在途观察 | `0976b6f` |
| UA-08 | carry 每 item 重解整份 health | 每命令解析一次并按 artifact 索引（follow + query） | `97ecce4` |
| UA-09 | 同页重放仍重读整图 | 同批 receipt（page_identity + digest）只读快路径，返回已存 delivery ids，不打开文件；写者仍权威复核 digest | `0cbe6b2` |
| UA-10 | 坏意图被跳过，变成允许补下载 | `read_projection` 把坏事件作为一等结果；accept/recovery/publish 保守阻断；claim 阻断批次并报错（见 §4） | `0cbe6b2`、`84f0374`、`bff7c61` |
| UA-11 | 传输中接受 EXCLUDED 仍发布最终文件 | `publish_authorized` 在 `os.link` 前判定；排除 → `cancel_unpublished_delivery`（唯一 prepared→cancelled CAS），只删自己校验过的 `.part`；意图不可读 → `recovery_needed` 并保留字节 | `0976b6f`、`bff7c61` |

## 3. 固化的不变量

- 预检快照是“这一页被看过”的唯一凭据；空/缺记录不等于不健康。
- 意图键 `(illust,page,output_key,root_revision)` 全局唯一；坏事件永远不是“没有排除”。
- 恢复的 reuse/repair/park 使用同一冻结观察与同一闸门；拒绝时旧 hold 不退休。
- 发布线性化点是 `os.link` 之前；已落盘文件不因随后排除被删除，只约束后续。
- claim 遇坏投影：事务内阻断批次（`pausing/blocked/intent_unreadable`）+ 诊断事件 + 明确异常，不得静默空转。
- 重复命令与重放只返回已接受结果；不同内容/目标复用命令 ID 冲突。

## 4. 只读审查与返修

独立只读 fork 对照契约逐行审 `8d8c62c..HEAD`，提出 1 项 P1、6 项 P2、11 项 P3。`bff7c61` 已修：

- P1-1 坏意图让 claim 返回 `None`、coordinator 空转 → 提交式阻断 + `CorruptDataError`。
- P2-1 瞬时预检失败被当成永久滞留 → `preflight_failed/observation_failed` 重试。
- P2-2 不可观察的 trusted 候选被报 `artifact_unknown` → `RetryPreflight`。
- P2-3 坏意图取消 prepared 交付并丢弃字节 → `recovery_needed` 且保留 `.part`。
- P2-4/§P3-9 契约与实现漂移 → 文档改为“confirm 不复查、线性化点在 link 前”，修正分工表与根状态措辞。
- P2-5 close 与 accept 的入队竞态、哨兵共用 → 同一 guard 入队、分别投递、drain 未完成 Future。
- P2-6 已排队未执行的 accept 不计入 busy → `_observations.qsize()` 计入。
- P3-2 reuse 退休未校验前态 → 只允许 `pending/needs_verification`。
- P3-10 坏 `event_seq=-1` 抛错类型不一致 → 规范化为 0。

P3-1（completed 的幂等 claim）保留：否则“完成竞争后再次预检”的用例会在第二次 claim 失败；已加注释说明 coordinator 不会派发 completed。

## 5. 未关闭 / 遗留

1. 意图投影与索引（F13）：每次 accept/claim/recovery/publish 仍全表扫 `events`；2,000 页规模需实测并另做迁移门禁。
2. carry 长写事务（10.8 s 量级）与可续窗口未拆分（D4 剩余）；`_health_map` 仍只在解析时判定时效。
3. `RetryPreflight` 预算 3 次 / 2 s 对超大单页可能偏紧；未做规模定值。
4. 已完成页的“重新核验”没有入口：`recover_verification_page` 对 completed 保持幂等，W5 需独立 API。
5. 真实迁移/备份/崩溃回退演练、外置卷与真实重解析点端到端未做；3 个符号链接跳过仍是平台边界。
6. 旧 `record_manager` 路径未接健康门；J-01 只在 v2 三条复用路径 + recovery 覆盖。
7. 8 MiB 命令上限在快照生成后才检查；大页可能白读一遍。
8. UNKNOWN 的 park 仍映射 `artifact_unknown`（诊断码与“没观察过”易混），未改。
9. `root_state` 每次做一次 `is_dir()`，网络卷可能阻塞；未测。
10. `recover_local` 遇外置根掉线仍报 `recovery_needed/local_recovery_conflict`（C 路有意未改）。
11. W5 UI/CLI、正常启动空闲、用户不选修复不联网的界面仍未做。

## 6. 复现

```powershell
$env:PYTHONNOUSERSITE = '1'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest tests `
  -q -p no:cacheprovider `
  --ignore=tests/test_collection_presentation.py `
  --ignore=tests/test_source_presentation.py

$env:TCL_LIBRARY = 'D:\Miniconda3\envs\JuiLiu\Library\lib\tcl8.6'
$env:TK_LIBRARY  = 'D:\Miniconda3\envs\JuiLiu\Library\lib\tk8.6'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest `
  tests/test_collection_presentation.py tests/test_source_presentation.py -q
```

证据：`.runtime/jpgpng_u1a_indep_fix_20261113_01/`（`red01/` 红基线、`full03/` 最终全量、`tk04/` Tk、各 lane 的 `run*/`）。

## 7. 改动文件

- 新增：`core/observation_snapshot.py`、`core/removal_scope.py`、`docs/optimization/2026-11-13/U1A_STATE_CONTRACT.md`
- 产品：`core/library_repository.py`、`core/delivery_recovery.py`、`core/page_delivery.py`、`core/library_paths.py`、`core/follow_repository.py`、`core/query_repository.py`、`core/task_coordinator.py`
- 测试：`tests/test_u1a_indep_contract.py`、`tests/test_u1a_independent_regressions.py`、`tests/test_u1a_accept_race.py`、`tests/test_u1a_recovery_ownership.py`、`tests/test_u1a_root_guard.py`、`tests/test_u1a_cross_lane_boundaries.py`、`tests/test_u1a_carry_parse_once.py`、`tests/test_u1a_publish_intent.py`、`tests/test_u1a_audit_followups.py`；`tests/test_u1a_reuse_gate.py` 仅适配 stale 预检管道。
