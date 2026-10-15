# Vallotton MCP 模块

以一套自定义的 MCP wire 协议暴露 Vallotton 的能力。
本模块是后端 internal API 的薄转发层：业务逻辑、对话落库、媒体归档全部发生在
Django 后端；MCP 会话在 Web 前端里同样可见（标记为 MCP 来源）。

## 协议

| 方法 | 路径           | 鉴权   | 用途 |
|------|----------------|--------|------|
| POST | `/tools/list`  | Bearer | 列出工具与 schema |
| POST | `/tools/call`  | Bearer | 调用工具，`{"name": ..., "arguments": {...}}` |
| GET  | `/health`      | 无     | 存活检查 |

- 鉴权：`Authorization: Bearer <MCP_TOKEN>`（`.env` 中的 `MCP_TOKEN`，常数时间比较）。
- 响应形状：`{"ok": bool, "result": ..., "error": "..."}`。
- 图片一律以 `{filename, content_type, data_base64}`（恰好三键）内嵌在 `result` 树中返回。
- 不支持 multipart / SSE / WebSocket / URL 回调。

## 工具

| 工具 | 说明 |
|------|------|
| `chat` | 与瓦洛东完整对话。判断是否绘图任务 → 可能返回图片。支持 `conversation_id` 续聊、`image1..3` 参考图 |
| `generate_image` | 跳过 grok，直接以三段式 prompt 调 NovelAI V5 |
| `quota` | NovelAI 订阅/用量 + Grok 本地累计花费 |

注意：`chat` 是同步阻塞调用，绘图任务可能耗时 1–5 分钟，客户端请配置足够的超时。

## 配置（都来自仓库根 `.env`，也可在本目录放独立 `.env` 覆盖）

| 键 | 用途 |
|----|------|
| `MCP_TOKEN` | 必填。Bearer 鉴权 token |
| `MCP_HOST` / `MCP_PORT` | 独立运行时的监听地址/端口（默认 `127.0.0.1:8766`） |
| `VALLOTTON_BACKEND_URL` | 后端地址（独立运行默认 `http://127.0.0.1:8000`；docker compose 内自动指向 `backend:8000`） |

## 启动

### 方式 A：随 docker compose（推荐，见根目录 DEPLOY.md）

```bash
docker compose --profile mcp up -d
# 宿主端口 = ${MCP_BIND:-127.0.0.1}:${MCP_PUBLIC_PORT:-8301}
# MCP_BIND=0.0.0.0 时可从外网 IP:端口 直连（不走反向代理）
```

### 方式 B：独立进程

```bash
cd mcp
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

独立运行时后端也要在跑（compose 里的 backend 默认不映射宿主端口——
如需独立 MCP 直连容器后端，在 docker-compose.override.yml 给 backend 加
`ports: ["127.0.0.1:8000:8000"]`，并设 `VALLOTTON_BACKEND_URL=http://127.0.0.1:8000`）。

## 自测

```bash
TOKEN=<MCP_TOKEN>
curl -X POST http://127.0.0.1:8301/tools/list \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'

curl -X POST http://127.0.0.1:8301/tools/call \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"quota","arguments":{}}'

curl -X POST http://127.0.0.1:8301/tools/call \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"chat","arguments":{"message":"画一只在飘窗上晒太阳的三花猫"}}' \
  --max-time 600
```

