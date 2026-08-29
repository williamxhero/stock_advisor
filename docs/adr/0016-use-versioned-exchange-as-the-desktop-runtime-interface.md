# 使用版本化 Exchange 作为桌面与运行时 interface

状态：accepted

桌面端与本地运行时只通过 `%LOCALAPPDATA%\AITradingCompanion\exchange` 的版本化 JSON 交换只读投影、幂等命令和回执，桌面端不直接读写业务 SQLite。该选择保留文件交换的可恢复性与独立进程隔离，并要求运行时成为事实与策略的唯一写入者；其他本机 Gateway 设计不作为正式产品 interface。
