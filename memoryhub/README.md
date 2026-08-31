# Trading MemoryHub

MemoryHub 是 AI 交易伙伴正式消息与跨任务长期记忆的独立权威模块。它提供不可变 Episode Ledger、冻结双时间快照、确定性阶段访问政策、按需来源水合、可重建检索投影和恢复能力；它不形成交易判断，也不拥有持仓、日程或工作流状态。

## 运行

```bash
PYTHONPATH=src python3 -m trading_memory_hub.server \
  --host 0.0.0.0 --port 8820 \
  --database /data/services/memoryhub/data/ledger.sqlite3 \
  --backup-dir /data/services/memoryhub/backups
```

只有确认本机 Ollama 模型已经安装后才增加 `--ollama-model <model>`。Ollama 不可用只影响派生摘要，不影响 Ledger 和正式历史。

健康检查：`GET /health`。正式调用 interface 见 `openapi/memoryhub-v1.openapi.json`。

## 数据边界

- `data/ledger.sqlite3`：不可变权威 Ledger 与冻结快照。
- `backups/`：在线 SQLite 备份及校验 manifest。
- MarketHub/8815：只保存稳定引用、hash 与可重建索引，原文按需水合。
- WAG/普通网页：保存当时实际读取的正文快照。
