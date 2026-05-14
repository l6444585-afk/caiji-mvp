# caiji-mvp

QQ 群消息采集 MVP：廖总 Win 跑 NapCat → HTTPS POST 阿里云 ECS → FastAPI 接收 → SQLite + JSONL 落地。

## 架构

```
廖总 Win (NapCat + QQ 小号)
  → HTTPS POST
  → https://caiji.tianku.com/onebot/event  (阿里云杭州 ECS, nginx + Let's Encrypt)
  → FastAPI (127.0.0.1:8090, systemd: caiji-mvp.service)
  → SQLite (data/messages.db) + JSONL (data/messages.jsonl)
```

## 生产环境

- 域名：`https://caiji.tianku.com`（NapCat 上报入口固定 URL）
- 服务器：阿里云 ECS `i-bp18zrqcsw2yuxmy6yd2` (47.99.195.159, cn-hangzhou)
- 部署目录：`/opt/fengyun/caiji-mvp`
- systemd: `caiji-mvp.service`（监听 127.0.0.1:8090）
- nginx: `/etc/nginx/sites-available/caiji`（443 + 80→443 重定向 + HSTS）
- 证书：Let's Encrypt，certbot.timer 自动续期

部署/更新（无 SSH，走阿里云 RunCommand）：
```bash
bash infra/scripts/provision.sh code      # git pull + 装依赖
bash infra/scripts/provision.sh service   # 重启 systemd
bash infra/scripts/provision.sh verify    # 端到端验证
bash infra/scripts/provision.sh all       # 全流程（首次部署）
```

## 本地开发

```bash
cd /Users/tkag/Projects/caiji-mvp
source .venv/bin/activate
pip install -r requirements.txt
# 生成本地 secret (一次)
python3 -c "import secrets; print('CAIJI_SECRET=' + secrets.token_urlsafe(32))" > .env
# 加载 env 后启动
export $(grep -v '^#' .env | xargs) && python -m uvicorn server:app --host 127.0.0.1 --port 8090 --reload
```

环境变量（参考 `.env.example`）：

| 变量 | 用途 | 是否必填 |
|---|---|---|
| `CAIJI_SECRET` | OneBot 11 X-Signature HMAC-SHA1 验签密钥，与 NapCat 客户端 Secret 字段对应 | **生产必填**；本地开发未设为空时跳过验签 |

## 安全

- 接收端验签：`/onebot/event` 强制要求 `X-Signature: sha1=<HMAC-SHA1(secret, body)>`，错误/缺失返回 401（仅生产模式）
- 协议参考：[OneBot 11 HTTP](https://github.com/botuniverse/onebot-11/blob/master/communication/http.md)
- Secret 存放：本地 `.env`（gitignored），服务器 `/etc/caiji-mvp.env`（chmod 600 root-only，systemd `EnvironmentFile=` 加载）
- NEVER 把 secret 写进代码/commit/截图/聊天群

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
