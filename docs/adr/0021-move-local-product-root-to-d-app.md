# 将本地产品根迁移到 D:\APP

状态：accepted（2026-09-09 用户确认）

AITradingCompanion 的正式本地产品根固定为 `D:\APP\AITradingCompanion`。安装资源位于其只读 `app\` 子目录；用户数据、Runtime 数据库、Exchange、配置、草稿、日志、缓存和隔离 Python runtime 均位于产品根内。桌面端、Tool Manager、Python runtime、安装器、验证器和启动项必须使用同一根目录，不得各自回退到 LocalAppData。

从旧 `%LOCALAPPDATA%\AITradingCompanion` 切换时采用可恢复迁移：先停止旧产品进程，将正式状态复制到目标并至少核对主数据库 SHA-256，再安装和验证新版本、切换启动项，最后把旧根改名保留为带时间戳的迁移备份。旧根及备份不得成为正式运行输入，不自动删除。

`AI_TRADING_COMPANION_HOME` 只保留给隔离测试、开发预览和显式运维命令；正式安装与普通启动不依赖该环境变量。该决定迁移 ADR 0016 的物理路径，但不改变 Runtime 的事实所有权、Exchange 合同、消息不可变性或 MemoryHub 边界。
