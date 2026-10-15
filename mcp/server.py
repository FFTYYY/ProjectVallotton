"""
Vallotton MCP server —— FastAPI 胶水：路由 + 入口。

协议契约（与 example-app 完全一致）：
POST /tools/list  -> {"tools": [{name, description, inputSchema, outputSchema}, ...]}
POST /tools/call  body = {"name": ..., "arguments": {...}}
                  -> {"ok": bool, "result": ..., "error": "..."}
GET  /health      -> {"ok": true, ...}

文件双向都嵌在 arguments / result 树里作为 {filename, content_type, data_base64}。
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

# base 必须最先 import：加载 .env、校验 token，未设 token 直接拒绝启动。
from base import require_bearer
from tools import TOOLS, dispatch

LISTEN_HOST = os.environ.get("MCP_HOST", "127.0.0.1").strip()
LISTEN_PORT = int(os.environ.get("MCP_PORT", "8766"))

app = FastAPI(title="Vallotton MCP", version="1.0.0")


@app.post("/tools/list")
def list_tools(_: None = Depends(require_bearer)) -> dict:
    return {"tools": TOOLS}


@app.post("/tools/call")
async def call_tool(request: Request, _: None = Depends(require_bearer)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body 不是合法 JSON")

    name = (body.get("name") or "").strip()
    arguments = body.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="arguments 必须是对象")

    # dispatch is fully synchronous and a drawing call can take minutes —
    # run it off the event loop or /health and every other tool call would
    # block for the whole generation
    return await run_in_threadpool(dispatch, name, arguments)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "vallotton-mcp"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)
