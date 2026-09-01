# 使用版本化 Exchange 作为桌面与运行时 interface

状态：accepted；由 ADR 0020 补充正式历史的物理所有者

桌面端与本地运行时只通过 `%LOCALAPPDATA%\AITradingCompanion\exchange` 的版本化 JSON 交换只读投影、幂等命令和回执，桌面端不直接读写业务 SQLite。该选择保留文件交换的可恢复性与独立进程隔离，并要求运行时成为事实与策略的唯一写入者；其他本机 Gateway 设计不作为正式产品 interface。

ADR 0020 将正式消息历史和跨任务长期记忆的物理所有权迁至独立 MemoryHub，但不改变本 ADR 的桌面 seam：桌面仍只与 Runtime 交换版本化 Exchange JSON，Runtime 负责访问 MemoryHub 并投影用户可见 timeline。
