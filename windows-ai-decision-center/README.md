# A股 AI Decision Center

一个本地 Windows 客户端，用 Gmail 作为消息入口，接收 ChatGPT 定时任务的完整结果并形成当天的决策时间线。

## 已实现

- Gmail OAuth 2.0 桌面登录，只申请 `gmail.readonly`
- 节点到达后每 30 秒轮询，最多持续 20 分钟；超时显示 `PASS` 并等待下一任务节点
- 分页扫描全部 `[ChatGPTTask]` 邮件，只下载本地尚未保存的 Gmail message id
- 每封邮件作为一条独立 SQLite 记录永久保存，并按接收时间倒序浏览
- 09:00 / 09:45 / 10:30 / 14:30 / 15:20 五节点看板
- “今日节点 / 历史消息”双视图，显示时间、项目分类、任务类型和完整 Markdown 正文
- Windows 系统托盘通知，并播放系统提示音
- 可选开机启动
- OAuth token 使用 Windows DPAPI 加密，仅当前 Windows 用户可解密

## 目录结构

```text
windows-ai-decision-center/
├─ src/
│  ├─ AIDecisionCenter.Core/       # 邮件标题契约、领域模型、纯文本解析
│  └─ AIDecisionCenter.App/        # WPF UI、Gmail、SQLite、通知、启动项
├─ tests/AIDecisionCenter.Tests/   # 标题解析、HTML 回退、SQLite 去重测试
├─ config/                         # 可提交的配置示例；不保存真实 OAuth secret
├─ scripts/                        # 运行、OAuth 安装、发布脚本
├─ docs/                           # Gmail 和 ChatGPT 端接入说明
└─ artifacts/                      # publish 产物（已忽略）
```

应用图标源文件和多尺寸 Windows 图标位于 `src/AIDecisionCenter.App/Assets/`；修改源图后可运行 `scripts/build-icon.ps1` 重新生成 `.ico`。

运行数据统一保存在：

```text
%LOCALAPPDATA%\AIDecisionCenter\
├─ appsettings.json
├─ oauth-client.json
├─ decision-center.db
└─ tokens\                         # DPAPI 加密后的 OAuth token
```

## 启动本地客户端

```powershell
cd D:\WILL\AGENT\agent\windows-ai-decision-center
.\scripts\run.ps1
```

窗口启动后会显示当天五个真实任务节点；只有收到对应 Gmail 邮件后，节点才会变成“已完成”。

## 接入真实 Gmail

先按 [Gmail OAuth 配置](docs/gmail-oauth.md) 创建 Desktop app 客户端，然后执行：

```powershell
.\scripts\install-oauth-client.ps1 -ClientJson C:\path\to\client_secret_xxx.json
.\scripts\run.ps1
```

首次同步会打开浏览器，让你登录并授权。之后客户端只读取符合查询条件的邮件；不会发送、修改或删除 Gmail 邮件。

为了避免后台意外唤起登录页，客户端在首次授权完成前不会自动轮询；只有主动点击“立即同步”才会打开 Google 授权流程。

## 邮件标题契约

客户端只导入以下格式的邮件：

```text
[ChatGPTTask][A股][09:00] 盘前机会发现 2026-08-24
[ChatGPTTask][A股][09:45] 开盘异常发现 2026-08-24
[ChatGPTTask][A股][10:30] 趋势确认 2026-08-24
[ChatGPTTask][A股][14:30] 操作决策 2026-08-24
[ChatGPTTask][A股][15:20] 收盘复盘 2026-08-24
```

正文保持 Markdown 即可。ChatGPT 端建议遵循 [定时任务邮件契约](docs/chatgpt-task-email-contract.md)。

## 验证与发布

```powershell
dotnet test .\AIDecisionCenter.sln
.\scripts\publish.ps1
```

默认发布到 `artifacts\win-x64\`，目标机器需要安装 .NET 8 Desktop Runtime。
