# caiji-mvp

QQ 群消息采集 MVP：廖总 Win 跑 NapCat → ngrok 公网 → 本机 FastAPI 接收 → SQLite + JSONL 落地。

## 架构

```
廖总 Win (NapCat + QQ 小号) → HTTP POST → ngrok (公网映射) → FastAPI (本机 8090) → SQLite + JSONL
```

## 启动（开发期）

```bash
cd /Users/tkag/Projects/caiji-mvp
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn server:app --host 0.0.0.0 --port 8090 --reload
```

另开终端起 ngrok：
```bash
ngrok http 8090
# 拿到 https://abcd-xx-xx.ngrok-free.app 这种公网 URL，填进廖总的 NapCat 配置
```

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET  | `/`              | 健康检查 |
| POST | `/onebot/event`  | NapCat OneBot 11 HTTP 上报入口（廖总 NapCat 配置这个 URL） |
| GET  | `/recent?limit=20` | 查询最近 N 条解析成功的发单 |
| GET  | `/stats`         | 总条数、解析率、Top 10 群 |

## 数据落点

- `data/messages.db` — SQLite，含 `messages`（原始消息）+ `feeds`（解析后发单）两张表
- `data/messages.jsonl` — JSON Lines 全量备份，便于回溯

## 测试

```bash
pytest tests/ -v
```

## 给廖总的 Windows 安装指南

`docs/liaozong-windows-napcat-install.md`
