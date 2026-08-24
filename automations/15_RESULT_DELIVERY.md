# Local Result Delivery

Contract ID: `LocalResultDelivery-v1.0`  
Message Contract: `ai-decision-message/v1`  
Scope: 所有 Registry 中 `delivery_policy=local_outbox` 的有效任务

## 1. 唯一事实来源

- `data/runtime/stock_advisor.sqlite3` 是自动化运行、完整回复和投递状态的源端审计库。
- `%LOCALAPPDATA%\AIDecisionCenter\inbox` 是两个本地模块之间唯一的消息交换 seam。
- Decision Center 的 `decision-center.db` 只保存展示副本与用户审阅状态；禁止任何一方直接读写另一方数据库。

## 2. 初始化运行

只有 dispatcher 选出有效 `task_key`，且 Registry 与目标 Protocol 身份核对通过后，才运行：

```powershell
python scripts/automation_results.py prepare `
  --task-key "<task_key>" `
  --task-name "<Registry task_name>" `
  --task-type "<Registry task_type>" `
  --scheduled-for "<计划时点，带Asia/Shanghai的+08:00偏移>" `
  --registry-id "<完整Registry ID>" `
  --protocol-id "<完整Protocol ID>"
```

记录命令返回的`run_id`、`body_path`、`summary_path`和`payload_path`。`prepare`失败时不得继续业务写入；停止并报告ResultStore异常。

## 3. 生成结果

- `body_path`：写入本次要在Codex任务中输出的唯一完整Markdown正文。
- `summary_path`：写入一行、不超过240字符的纯文本摘要。
- `payload_path`：写入JSON object；v1没有稳定结构化结果时写`{}`，不得写数组或无效JSON。
- 正常执行使用`succeeded`；非交易日或周期尚未到期使用`skipped`；协议、数据或写入失败使用`failed`。
- 异常发生在`prepare`之后时，仍应尽量形成错误正文、摘要和空payload，并完成同一个run。

## 4. 完成与投递

```powershell
python scripts/automation_results.py complete --run-id "<run_id>" --status "<succeeded|skipped|failed>"
```

`complete`在单一SQLite事务中完成run、完整正文和Outbox，然后原子写入Inbox。命令成功后，只在接收工作的汇总任务中从`body_path`原样输出正文；投递任务不得复制正文。不得追加、删减或重新措辞。命令失败时保留staging并报告，不得假称Decision Center已收到。

待投递恢复与诊断：

```powershell
python scripts/automation_results.py dispatch
python scripts/automation_results.py status
```

## 5. 状态语义

- 调度异常或无效启动窗口：不创建run。
- `skipped`：有效计划任务已执行日历或范围判断，但本次无需业务分析。
- `failed`：有效任务已启动但无法形成正常业务结果；正文必须说明失败位置和未完成写入。
- 同一计划时点的人工重跑生成新run；源端和Decision Center历史都保留，今日节点显示最新完成的一条。
