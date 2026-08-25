# 稳定事实源与可替换记忆检索

原始消息、证据、判断和结果以本地不可变事实记录及发生时间/获知时间为长期依据，Markdown/CSV和记忆索引均为可重建投影。SQLite FTS先作为生产检索 adapter，未来 Memory Palace、向量库或知识图谱只能通过同一 MemoryRetriever seam接入并在回放评测后替换，避免检索技术成为不可追溯的唯一记忆。
