# Provider API 与研究运行时

正式 LLM 链路只使用 OpenAI Chat Completions 兼容的 Provider；不调用、不探测、也不回退 Codex CLI。CPA 通过 `/v1/chat/completions` 接收标准 `messages`、SSE、结构化输出和 `tool_calls`；工具续接由本地追加 `assistant` 与 `tool` 消息完成，不依赖 Provider 的响应上下文 ID。

本机默认目标已经固定为小电脑服务：CPA `http://yosef-server:8317/v1`，SearXNG `http://yosef-server:8801`。Provider URL、三个模型槽、effort、SearXNG、专用 Edge Profile 和下载上限保存在 `%LOCALAPPDATA%\AITradingCompanion\config\settings.local.json`；API key 只保存在 Windows Credential Manager 的 `AITradingCompanion/CPA` 凭据目标。

桌面端“运行配置”可更新这些值并请求运行时探测。也可使用 `python -m ai_trading_companion configure-provider --api-key-stdin`：该命令先写入当前用户的 Windows 凭据库，再验证普通响应、结构化输出、函数工具续接和流式能力，成功后才启用配置。模型槽必须以 CPA 当前实际可用模型为准；本机当前确认 `gpt-5.6-sol`、`gpt-5.6-terra` 可用，`gpt-5.6-luna` 返回 `unknown provider`，因此快速槽暂时复用 terra。

初始化浏览器研究 Profile 使用 `python -m ai_trading_companion bootstrap-browser-profile`。配置中的 `Profile 2` 表示 Edge 界面上的第二个用户配置，运行时会解析到本机实际目录 `Profile 1`。即使 Edge 正在运行，程序也只通过稳定文件读取和 SQLite 在线 backup 创建一致性快照，不要求关闭或接管 Edge；快照会先在 staging 目录校验，再原子安装。若运行中的 Edge 对 Cookies 施加独占锁，初始化仍建立仅限公共资料的专用 Profile，并明确报告认证 Cookie 未捕获，而不是伪造已登录状态或阻断公共新闻研究。程序只复制必要 Cookie 与偏好状态，不复制密码、自动填充、扩展、历史、缓存或下载记录。专用 Profile 不回写日常 Profile。

研究工具只有 SearXNG、无界面 Playwright 页面读取和受控下载。模型可自行选择顺序和查询；代码只限制读取权限、允许文件类型、50 MB 默认下载上限、秘密拦截、超时与循环故障。如果所有当前信息后端失败，运行时保留结构化失败记录而不生成依赖当前事实的结论。
