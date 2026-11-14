# 逐项发现：52e6936 / 产品 01b91c3

优先级按用户影响排序：P1 为可能让错误副本通过恢复门槛；P2 为可操作的功能/诊断错误；P3 为展示契约缺口。没有把未实测的猜想列为已确认缺陷。

## WC-01 / P1：副本内容改变后仍返回 ok=True

位置：`core/library_maintenance.py:826` (`verify_library_copy`)，`:626` (`_objects_match`)；调用者 CLI `verify`，运行说明将 `ok=True` 作为副本演练门槛。

独立场景一：用实际 `backup_library` 生成一个含 1 个 job 的在线快照；只把副本 job 的 `state` 从 `queued` 改为 `cancelled`，不改变任何表的行数。源为 queued，副本为 cancelled；实际返回 `counts_match=True, objects_match_source=True, checks_match=True, healthy=True, main_sha256_match=False, reasons=[], ok=True`。生成时的 manifest 摘要也未被 `verify` 读取。

独立场景二：两库业务表完全一样，额外索引都叫 `extra_same_name`，一个索引 jobs.state，另一个索引 jobs.request_id。仍返回 `objects_match_source=True, ok=True`。当前函数只比较“相对于声明的缺失/多余/不匹配名称集合”，没有比较两边对象实际定义；两边偏离声明的方式可以不同。

影响：误拿旧副本、同数量记录的变化或备份后发生内容改写，都可能被宣布为“忠实副本”。SQLite 结构健康只说明库内结构符合检查，不证明它与指定源/封存点的内容相同。此处是**契约设计与实现的共同缺口**：冻结稿只列对象/计数/检查，维护记录和函数文档却把它解释为忠实性，必须一起修。

不能简单要求源主文件与备份主文件 SHA 相同。在线快照可把 WAL 中已提交内容合入，当前实现还将副本 journal 转为 DELETE；本轮 WAL 正向对照确有主文件 SHA 不同但逻辑内容正确的情况。也不能把对“当前不断变化源库”的比较与对“生成时封存副本”的校验混成一个布尔值。

验收至少包括：同计数改单值被发现、同名异定义对象被发现、封存后改单字节/记录被发现、正常 WAL 快照仍通过、源继续写入时正确说明“与当前源不同”而非说备份坏了。详细协议在开发指导 S0/S1。

探针：`test_wc01_same_counts_changed_backup_is_rejected`、`test_wc01_same_object_names_different_ddl_is_rejected`，均失败；`test_control_online_snapshot_wal_is_valid` 通过。

## WC-02 / P2：未知结果被总结为全部一致

位置：`core/library_maintenance.py:1068` (`_scan_lines`)，`:1057` 的 `_SCAN_FINDINGS`，以及 scan 退出码分支。

独立场景：通过真实执行器写入 70 字节 PNG，随后以 `--max-bytes 1` 巡检。核心正确返回 `examined=1, matched=0, unknown=1, bytes_hashed=0, reason=file_exceeds_budget`；CLI 却输出“旧行或缺摘要”及“本次核验的每一行都与记录一致”，退出 0。

根因：unknown 被排除在 findings 外，`else` 把“没有确定异常”提升成“均已核验一致”；unknown 的各类原因统一换成旧行/缺摘要。`corrupt_removal_intent`、路径拒绝、时间不足同样可能被掩盖。

边界：当前 runbook 确实规定 unknown 不影响退出码，所以不能只以“退出 0”指控违反既定接口。已确认错误是文案与原因不实；退出码/自动化门槛应在 S0 明确定义，避免调用方仍把 0 当成完整覆盖和健康。

修复必须保留真实 reason、匹配/未知/排除/未覆盖计数，区分空库、正常完整一致、部分完成、未知及确定异常。不得靠删除 unknown 行来“全绿”。探针 `test_wc02_scan_budget_unknown_is_not_all_verified` 失败。

## WC-03 / P2：异常核验结果在同一回调链内被绿色完成覆盖

位置：`gui/collection_controller.py:777` (`_verify_finished`)、`:786` (`_finish_verification`)。

`_verify_finished` 先用 `describe_result` 展示正确的 held/refused/未知原因，随即 `_verify_step` 在队列空时调用 `_finish_verification`，将最后一条结果无条件覆盖成 success 色的“本轮核验完成：1/1 页”。没有保存结果分布，也没有保留可查看的最终失败原因。

独立测试分别返回 held/artifact_changed、refused/batch_not_running、mystery/brand_new_reason；三者最终可见调用都是 success。旧测试覆盖了纯格式化函数及调用曾经出现过，未检验用户最后看到什么。

影响：用户会把文件内容异常、当前不能操作或未知结果当成核验成功。批量中途的失败也会消失。结果“处理完”与“核验通过”必须分别计数，单条应保留详情；批量应保留各类数量及异常原因。`repair_queued` 只能说明已排补下，不能统计成已核验恢复成功。

探针 `test_wc03_ui_final_result_preserves_non_success` 三个参数失败；正常 reused 的绿色对照通过。该问题不代表核心已覆盖/删除图像，核心结果本身仍正确。

## WC-04 / P2：旧任务迟到查询仍能驱动恢复动作

位置：`gui/collection_controller.py:289` (`_select_task`)、`:660` (`_refresh_verification`)、`_verification_pages`、`_recover_verification`。

独立事件顺序：选 job-a/batch-a → 进入核验并挂起列表 Future → 返回任务页选择 job-b/batch-b → 再进入核验 → batch-a 的旧 Future 完成 → 点击其列表行核验。实际 `_current_batch='batch-b'`，显示行和提交命令却是 batch-a。

根因：核验查询使用固定 pending key，没有像记录页那样绑定版本/任务；切换选择不清空/作废旧核验列表。旧请求仍 pending 时，B 的刷新被跳过。恢复入口又只验证 key 是否存在于旧缓存，没有验证缓存属于当前批次。

这是用户选择作用域错误，不是账号越权。核心收到的是合法 A 坐标，仍会执行 A 的恢复规则，不能期待数据库层替界面猜测“用户现在想操作 B”。后端现有暂停/所有权/CAS 检查必须继续保留。

验收：慢 A/快 B、摘要/列表逆序完成、同 job 新 batch、刷新重复响应、选择改变发生在恢复进行中，以及 close 后完成回调。读取 epoch、批次身份与展示/动作状态需一起冻结。不能通过取消旧 Future 就假设旧后台工作已停止。探针 `test_wc04_ui_ignores_rows_for_previous_selected_batch` 失败。

## WC-05 / P2：不合法数值没有进入行级诊断，整次巡检崩溃

位置：`core/library_audit.py:271` (`_audit_row`) 在任何记录验证之前调用 `:420` (`_row_fields`)，后者无保护地 `int(row['size'])`；CLI 只捕获已定义维护/审计错误。

独立场景：合成正常发布一页，执行 `UPDATE artifacts SET size='broken-size'`。在当前非 STRICT SQLite 表中该值可落库，DDL/quick_check/integrity_check 仍健康；scan 在 `_row_fields` 抛 `ValueError`，来不及进入 `_recorded_expectation` 的 malformed/legacy 分支。

这是异常账本防御场景，不声称正常下载器会写此字符串。巡检本身用于定位异常记录，应保留该行身份和原因，继续可安全游标化的下一行；若关键身份不可用，应明确结构化停止并保留最后安全游标，不能输出 Python traceback 或悄悄跳过。

验收包括 TEXT/BLOB/NULL/REAL 尺寸、异常页号、合法的 0/缺摘要旧行、混合好坏两行、JSON/文本 CLI。不得将异常值强行变成 0 再声称文件缺失，更不得写回修正业务库。探针 `test_wc05_scan_malformed_numeric_row_is_reported_not_traceback` 失败。

## WC-06 / P3：已有 artifact-only 页未获得清楚的展示统计

位置：`core/delivery_repository.py:806` (`verification_summary`)、`gui/verification_panel.py:231` (`summary_text`)。

独立场景：任务一正常保存 PNG；任务二接受同页并严格复用成功。任务二的 batch_item_pages 为 `delivery_id=NULL, artifact_id=<已有记录>`，这是合法状态；摘要 `total_pages=1, linked_pages=0`，counts 没有 completed，最终显示“本批 1 页 · 已登记 0 页”。

按现有接口，counts 明确是 delivery 状态计数，所以本项不定性为数据损坏或状态机回归。问题是用户可见语义缺少“已有页”，且 docstring 错称 total-linked 恰好是尚未开始页。用户不应以为已有文件没登记。

建议保留 delivery 计数含义，补独立 `existing_pages`/未挂接页统计并正确渲染。不要为了统计完整而伪造 delivery，或将历史 artifact 等同于当前磁盘健康。设计探针允许 completed 或独立 existing_pages 覆盖该页，失败用于提醒此处需要设计决定。

## 未作为缺陷的观察

- 文件组恢复残留 WAL 的合成探针通过，不能据静态怀疑声称恢复已损坏。
- 旧审计函数未被削弱：RFB 引入的原始函数 AST 相同；旧观察计数更新为“首次观察+确认 pass”与新严格契约相符，cleanup 游标的归零与已证明 EOF 相符。
- 当前导航同时写“RFB 待修”“W5 已完成”“日常仍旧版”，属于维护漂移，本轮直接修正当前入口并保存历史原文，不计入产品 P1/P2。
- 备份封存晚期异常清理、额外 trigger/view 范围、活跃库快照比较边界尚应纳入下一轮协议测试；本轮没有故障注入结果，不给它们套已确认缺陷编号。

技术依据仅用一手资料：在线备份产生一致快照的用途见 [SQLite Backup API](https://www.sqlite.org/backup.html)；WAL 与主文件共同决定读取状态见 [SQLite WAL](https://www.sqlite.org/wal.html)；`integrity_check` 的检查范围见 [SQLite PRAGMA](https://www.sqlite.org/pragma.html#pragma_integrity_check)。本轮具体失败结论来自本地源码与独立反例，不来自网络评价。
