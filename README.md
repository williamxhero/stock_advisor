# A股实盘决策与复盘（本地版）

本目录由 ChatGPT 在线项目 `A股实盘决策与复盘` 于 2026-08-24 迁移而来；现在只在本地运行和维护，不依赖任何云端任务或云端项目。

## 目录与运行入口

- `AGENTS.md`：项目级硬规则；新 task/thread 也必须遵守薄触发与本地边界。
- `automations/prompts/triggers/`：8 个单时点投递入口；每个定时器只绑定一个投递任务。
- `automations/prompts/trigger-handoff.md`：投递任务的公共转交规则。
- `automations/prompts/run-registered-task.md`：三个汇总任务统一使用的正式工作入口。
- `automations/thread-map.local.json`：8 个投递任务与 3 个汇总任务的本地映射。
- `automations/14_SCHEDULE_REGISTRY.md`：8 个业务任务的统一注册与路由。
- `docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md`：09:45、10:30、14:30、15:20。
- `docs/protocols/08_PERIODIC_REVIEW_PROTOCOL.md`：月度、季度、年度复盘。
- `docs/protocols/09_OPPORTUNITY_DISCOVERY_PROTOCOL.md`：09:00 盘前机会发现。
- `data/`：真实持仓、当前分析状态和追加式日志。
- `reports/periodic/`：新生成的月度、季度和年度报告。
- `scripts/validate_automations.ps1`：检查目录、路由、薄触发和已安装任务。

实际安装在 Codex 中的自动化 prompt 只引用一个 `automations/prompts/triggers/*.md` 文件。投递任务只把固定工作入口发送给对应汇总任务；任务路由和业务逻辑全部留在项目文件中，后续修改不需要复制长 prompt。

正式回复集中在三个汇总任务：`每日盘中操作`、`每日盘前盘后`、`月季年复盘`。8 个单时点投递任务只保留一行成功确认；成功运行不主动通知，投递失败才通知。

## 历史原件归档

- `archive/snapshots/online-library-2026-08-24/`：迁移时 15 个最新版文件的原样副本。
- `archive/snapshots/project-sources-2026-08-21/`：迁移时 7 个早期文件的原样副本。
- `archive/source-packages/A股实盘决策系统.zip`：使用上述早期原文件重建的历史压缩包。

归档只用于溯源，不参与任何定时任务运行。

## 定时计划（Asia/Shanghai）

| 任务 | 计划 |
|---|---|
| A股 09:00盘前机会发现 | 周一至周五 09:00 |
| A股 09:45异常发现 | 周一至周五 09:45 |
| A股 10:30趋势确认 | 周一至周五 10:30 |
| A股 14:30操作决策 | 周一至周五 14:30 |
| A股 15:20收盘复盘 | 周一至周五 15:20 |
| A股月度复盘 | 每月 1 日 19:00 |
| A股季度复盘 | 每 3 个月的 2 日 19:30 |
| A股年度复盘 | 每年 1 月 3 日 20:00 |

每日任务仍会按 Protocol 判断沪深交易所实际交易日；工作日触发不等于一定写入业务文件。

本地定时器只生成表中的正式时点。后台启动允许延迟不足 15 分钟，避免计划时点后的短暂排队被误判为无效任务。
