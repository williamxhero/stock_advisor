# 保持 tasks.json 为正式日程唯一来源

状态：accepted；supersedes ADR-0009

正式日程只由 `resources/schedules/tasks.json` 定义，SQLite 日程表是可重建投影和周期冻结审计，不再独立决定未来任务。EvaluationObservatory 只能生成带基础版本和文件哈希的精确补丁建议；明确应用文件补丁并通过运行时校验后，新日程才对尚未 claim 的未来周期生效。该选择牺牲运行时直接改时点的便利，以避免只读安装资源、恢复默认和实际生产日程出现多个权威来源。
