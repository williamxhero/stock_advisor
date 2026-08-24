# Gmail OAuth 配置

客户端使用 Google 官方 Gmail API 和 OAuth 2.0 Desktop app 流程，不保存 Gmail 密码。

## 一次性配置

1. 打开 [Google Cloud Console](https://console.cloud.google.com/) 并创建或选择一个项目。
2. 启用 [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)。
3. 在 Google Auth Platform 配置 Branding、Audience 和 Data Access。
4. 如果 Audience 是 External/Testing，把你自己的 Gmail 加入 Test users。
5. 在 Clients 中创建 OAuth client，Application type 选择 **Desktop app**。
6. 下载 JSON，然后在项目根目录执行：

```powershell
.\scripts\install-oauth-client.ps1 -ClientJson C:\Downloads\client_secret_xxx.json
```

Google 的官方流程也要求先启用 Gmail API、配置 OAuth consent，再创建 Desktop app client。客户端只请求 `https://www.googleapis.com/auth/gmail.readonly`，用于查看邮件和设置，不具备发送或删除权限。

不要使用 OAuth Playground 或 Web application 类型的 JSON。Desktop app 下载文件的顶层必须是 `installed`；如果顶层是 `web`，本地随机端口回调会触发 `redirect_uri_mismatch`，安装脚本会直接拒绝它。

## 首次授权

1. 启动客户端。
2. 点击“立即同步”。
3. 浏览器会打开 Google 授权页。
4. 选择接收任务邮件的 Gmail，确认只读权限。
5. 授权完成后返回客户端。

刷新 token 会加密保存在 `%LOCALAPPDATA%\AIDecisionCenter\tokens\`。加密绑定当前 Windows 用户。

## 撤销授权

关闭客户端后，删除：

```text
%LOCALAPPDATA%\AIDecisionCenter\tokens\
```

如需在 Google 侧彻底撤销，也可以在 Google Account 的第三方应用访问设置中移除该应用。

## 常见问题

- `未配置 Gmail OAuth`：确认 `%LOCALAPPDATA%\AIDecisionCenter\oauth-client.json` 存在。
- `access_denied`：External/Testing 应用通常需要先把当前 Gmail 加入 Test users。
- `redirect_uri_mismatch`：下载了 Web application 类型的 JSON；重新创建 Desktop app Client，不能只修改本地 JSON。
- 浏览器授权后仍失败：关闭客户端，删除 `tokens` 目录后重新授权。
- 收不到任务：先在 Gmail 搜索 `subject:"[ChatGPTTask]" newer_than:14d`，并核对标题格式。
