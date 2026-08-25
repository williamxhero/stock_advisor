# A股 AI Decision Center

本地优先的 Windows 客户端，用于接收、查看和维护 `stock_advisor` 的 AI 定时回复。消息接收、存储和审阅不依赖 Gmail、OAuth、HTTP 端口、云端项目或共享数据库；可选的高级 TTS 首次生成音频时需要联网。

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
- 启动时自动对账 processed 与 SQLite；归档已存在但数据库漏行时自动补收，原文件不移动。
- 契约、状态、时间或 SHA-256 无效时进入 dead-letter，并保留错误说明。
- `(source, external_id)` 幂等去重，重复文件不会重复通知。
- 09:00、09:45、10:30、14:30、15:20 五节点看板。
- 五个日内时点由本地确定性 scheduler 创建伴生 cycle；15 分钟内允许睡眠/重启补偿，超时明确显示“未按时运行”。
- 伴生服务单实例运行并写 5 秒心跳；客户端发现心跳过期会自动拉起，单次 Codex 失败不会阻断后续时点。
- 今日列表显示“AI 研究中 / 等待提交 H0 / 正在生成 M1 / 正在生成 M2 / AI 判断已完成 / AI 运行失败”等真实状态；正式 Inbox 正文不会冒充 M0/M1/M2。
- 月度、季度、年度回复进入历史记录和通知，不占每日节点。
- 历史列表按日期树形分组，并保留全文搜索。
- 右上角“朗读”使用 Edge `zh-TW-HsiaoYuNeural` 神经语音，并以 `1.3×` 速度播放；再次点击停止，切换消息自动停止。
- 朗读会识别并跳过报告开头的标题与运行元数据块，例如计划节点、实际执行、Protocol、Run ID，再从正式结论开始。
- 高级 TTS 复用 `edge-tts`、FFmpeg 和 `ffplay`，按正文与语音参数缓存最近50个MP3；同一正文再次朗读无需联网生成。
- AI 原始正文不可编辑或删除；个人维护字段单独保存在 SQLite。
- Windows 系统托盘通知和可选开机启动。
- 主界面“持仓”窗口展示事实持仓与独立的 AI 状态；价格均保留时间口径。
- AI 消息按当前任务显示一条自然语言时间线，M0、独立 M1和伴生 M2使用锚点定位。
- “我的消息”使用“发送→提交 H0/提交”批次边界；待提交消息可撤回，输入框草稿按任务保存且永不自动发送。
- “我的消息”中的明确成交陈述会由 Codex CLI 提取、由本地规则校验后更新持仓；计划和缺字段文本不会修改事实。
- 本地语音输入在录音时显示音量波形，转写只追加到可编辑草稿。

## 运行数据目录

```text
%LOCALAPPDATA%\AIDecisionCenter\
├─ appsettings.json
├─ window-state.json
├─ companion-drafts.json
├─ decision-center.db
├─ decision-center.db.pre-local-inbox-v1.bak
├─ tts-cache\message-*.mp3
├─ exchange\
├─ audio\
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

客户端无需额外账号配置。启动客户端会同时确保本地 companion runtime 正常运行；右上角状态会显示新消息导入、归档补收或对账失败。

高级朗读使用本机 `%LOCALAPPDATA%\Programs\Python\Python313\python.exe` 中已安装的 `edge-tts`，以及 WinGet 链接中的 `ffmpeg` / `ffplay`。首次朗读一篇新正文时会生成缓存，期间按钮显示“停止”；已缓存正文会直接播放。

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
