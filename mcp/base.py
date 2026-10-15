"""
样板部分：env 加载 + 鉴权 + wire 协议辅助（照搬 example-app 结构）。

# wire 协议（钉死，全双工）

请求 / 响应均为 application/json。文件双向都是普通对象
{filename, content_type, data_base64}：
- 请求侧：作为 arguments 里某个参数的值。
- 响应侧：嵌在 result 树里任意语义字段。
不接受 multipart；不接受任何 data_url / download_url 字段。

# 鉴权

环境变量 MCP_TOKEN 必须设；未设直接拒绝启动并打印建议的随机 token。
所有 /tools/* 要求 Authorization: Bearer 头匹配（常数时间比较）。
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
import sys
from pathlib import Path

from fastapi import Header, HTTPException

TOKEN_ENV_VAR = "MCP_TOKEN"

# 单文件硬上限（解码后字节数）。
MAX_FILE_BYTES = 100 * 1024 * 1024


# ----------------------------------------------------------------------
# .env 加载：优先本目录 .env，其次仓库根 .env（本模块与主应用共用配置）
# ----------------------------------------------------------------------

def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_HERE = Path(__file__).resolve().parent
_load_env_file(_HERE / ".env")
_load_env_file(_HERE.parent / ".env")


# ----------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------

EXPECTED_TOKEN = os.environ.get(TOKEN_ENV_VAR, "").strip()

if not EXPECTED_TOKEN:
    _suggested = secrets.token_urlsafe(32)
    print(
        f"\n[vallotton-mcp] 拒绝启动：环境变量 {TOKEN_ENV_VAR} 未设置。\n"
        f"  在 .env 中加入：\n\n    {TOKEN_ENV_VAR}={_suggested}\n\n"
        f"  并把同一个值配置到 MCP 客户端的 bearer_token。\n",
        file=sys.stderr,
    )
    raise SystemExit(2)


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """FastAPI 依赖。token 不匹配 → 401。常数时间比较避免计时旁路。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization[len("Bearer "):].strip()
    if not hmac.compare_digest(provided.encode("utf-8"), EXPECTED_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid bearer token")


# ----------------------------------------------------------------------
# wire 协议辅助
# ----------------------------------------------------------------------

def encode_file(filename: str, data: bytes,
                content_type: str = "application/octet-stream") -> dict:
    """构造一个嵌进 result 树的文件对象（恰好三个键）。"""
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=500,
            detail=f"产出文件超过 {MAX_FILE_BYTES} 字节硬上限：{len(data)}",
        )
    return {
        "filename": filename,
        "content_type": content_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
