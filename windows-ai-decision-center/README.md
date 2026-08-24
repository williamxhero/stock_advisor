# A股 AI Decision Center

完全离线的 Windows 客户端，用于接收、查看和维护 `stock_advisor` 的 AI 定时回复。客户端不依赖 Gmail、OAuth、HTTP 端口、云端项目或共享数据库。

## 数据流

```text
stock_advisor ResultStore
  → 事务 delivery_outbox
  → %LOCALAPPDATA%\AIDecisionCenter\inbox\pending\*.json
  → LocalInboxService
  → decision-center.db
  → 今日节点 / 历史审阅 / Windows 通知
```

- `D:\WILL\STOCK\stock_advisor\data\runtime\stock_advisor.sqlite3`：源端运行与完整回复审计库。
- `%LOCALAPPDATA%\AIDecisionCenter\decision-center.db`：客户端展示副本和个人审阅状态。
- Inbox JSON 是两套 SQLite 之间唯一的消息交换 interface。

## 已实现

- `FileSystemWatcher` 近实时发现，300ms debounce，每30秒补扫。
- 应用关闭时消息留在 pending；下次启动自动补收。
- 原子认领到 processing，成功后按日期归档到 processed。
- 契约、状态、时间或 SHA-256 无效时进入 dead-letter，并保留错误说明。
- `(source, external_id)` 幂等去重，重复文件不会重复通知。
- 09:00、09:45、10:30、14:30、15:20 五节点看板。
- 月度、季度、年度回复进入历史记录和通知，不占每日节点。
- 全文搜索、状态筛选、已读/未读、收藏、归档、个人备注和 Markdown 导出。
- AI 原始正文不可编辑或删除；个人维护字段单独保存在 SQLite。
- Windows 系统托盘通知和可选开机启动。

## 运行数据目录

```text
%LOCALAPPDATA%\AIDecisionCenter\
├─ appsettings.json
├─ decision-center.db
├─ decision-center.db.pre-local-inbox-v1.bak
└─ inbox\
   ├─ pending\
   ├─ processing\
   ├─ processed\yyyy-MM-dd\
   └─ dead-letter\
```

从旧版升级时，现有 Gmail 历史会迁移为 `source=gmail-legacy`；旧 OAuth 文件不再使用，但客户端不会主动删除用户文件。

## 启动

```powershell
cd D:\WILL\STOCK\stock_advisor\windows-ai-decision-center
.\scripts\run.ps1
```

客户端无需任何账号配置。点击“立即扫描”可手动补扫 Inbox；“运行目录”打开本地数据库、Inbox 和 dead-letter 所在目录。

## ResultStore 操作

在仓库根目录运行：

```powershell
python scripts/automation_results.py status
python scripts/automation_results.py dispatch
```

定时任务的完整 `prepare → complete` 合同由 `automations/15_RESULT_DELIVERY.md` 定义。

## 验证与发布

```powershell
python -m unittest discover -s tests -p "test_*.py"
dotnet test .\windows-ai-decision-center\AIDecisionCenter.sln
.\scripts\validate_automations.ps1 -CheckInstalled
.\windows-ai-decision-center\scripts\publish.ps1
```

默认发布到 `windows-ai-decision-center\artifacts\win-x64\`，目标机器需要 .NET 8 Desktop Runtime。
