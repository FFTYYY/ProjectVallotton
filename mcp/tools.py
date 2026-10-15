"""
Vallotton MCP 工具：声明 + dispatch。

三个工具都是对后端 internal API 的薄转发（业务逻辑、落库、媒体归档全在后端）：
- chat            与瓦洛东完整对话（grok 编排 → 可能触发 NovelAI 绘图）
- generate_image  跳过 grok，直接用三段 prompt 调 NovelAI V5
- quota           查询 NovelAI 订阅状态 + Grok 本地记账

图片一律以 {filename, content_type, data_base64} 文件对象内嵌在 result 树返回。
"""

from __future__ import annotations

import os

import httpx

BACKEND_URL = os.environ.get("VALLOTTON_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
MCP_TOKEN = os.environ.get("MCP_TOKEN", "").strip()

# 生成可能耗时数分钟（grok 高推理 + 多张图）
BACKEND_TIMEOUT = httpx.Timeout(900.0, connect=10.0)


FILE_SCHEMA = {
    "type": "object",
    "description": (
        "一个图片附件对象（参考图）。调用方通常用 {\"$ref\": \"<artifact_id>\"} "
        "引用已有文件，由框架展开为完整对象。"
    ),
    "required": ["filename", "content_type", "data_base64"],
    "properties": {
        "filename": {"type": "string"},
        "content_type": {"type": "string"},
        "data_base64": {"type": "string"},
    },
}

_IMAGE_FILE_OUT = {
    "type": "object",
    "description": "生成的图片（PNG，base64）",
    "properties": {
        "filename": {"type": "string"},
        "content_type": {"type": "string"},
        "data_base64": {"type": "string"},
    },
}


TOOLS = [
    {
        "name": "chat",
        "display_name": "Vallotton·对话绘图",
        "description": (
            "【Vallotton 绘图应用】向驻场 AI 绘师瓦洛东发送一条消息。她会判断这是否是绘图请求："
            "是则调用 NovelAI 生成图片并随文字回复一起返回；否则只返回文字回复。"
            "可附带最多 3 张参考图（参考图会被转述为文字提示词，永不做图生图）。"
            "传入上一次返回的 conversation_id 可以延续同一对话上下文（追问、修改上一张图等）。"
        ),
        "inputSchema": {
            "type": "object",
            "required": ["message"],
            "properties": {
                "message": {"type": "string", "description": "用户消息（自然语言）。"},
                "conversation_id": {
                    "type": "string",
                    "description": "（可选）延续既有对话的 ID；省略则新建对话。",
                },
                "n_images": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "description": "这一轮如果画图，生成几张，默认 1。张数由调用方决定，瓦洛东不会自己改。",
                },
                "image1": FILE_SCHEMA,
                "image2": FILE_SCHEMA,
                "image3": FILE_SCHEMA,
            },
        },
        "outputSchema": {
            "type": "object",
            "required": ["conversation_id", "reply"],
            "properties": {
                "conversation_id": {"type": "string"},
                "reply": {"type": "string", "description": "瓦洛东的文字回复。"},
                "is_image_task": {"type": "boolean"},
                "images": {"type": "array", "items": _IMAGE_FILE_OUT},
                "nai_prompts": {
                    "type": "object",
                    "description": "绘图任务时实际使用的 NovelAI prompts。",
                },
            },
        },
    },
    {
        "name": "generate_image",
        "display_name": "Vallotton·直接生图",
        "description": (
            "【Vallotton 绘图应用】跳过对话编排，直接以 NovelAI V5 的三段式 prompt 生成图片。"
            "prompt 使用 danbooru 风格英文标签，逗号分隔。适合已经有明确 prompt 的场景。"
        ),
        "inputSchema": {
            "type": "object",
            "required": ["base_prompt"],
            "properties": {
                "base_prompt": {
                    "type": "string",
                    "description": (
                        "主 prompt：人数 tag + 场景/构图/光线/画风 + 质量词"
                        "（如 'best quality, amazing quality, very aesthetic, absurdres'）。"
                    ),
                },
                "character_prompts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "每个角色一条：外貌/表情/姿势/服装 tags。",
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "内容性负面词（标准质量负面词会自动附加）。",
                },
                "size": {
                    "type": "string",
                    "enum": ["portrait", "landscape", "square"],
                    "description": "画幅，默认 portrait（832×1216）。",
                },
                "n_images": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "description": "生成张数，默认 1。",
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "required": ["images"],
            "properties": {
                "images": {"type": "array", "items": _IMAGE_FILE_OUT},
                "seeds": {"type": "array", "items": {"type": "integer"}},
                "size": {"type": "string"},
            },
        },
    },
    {
        "name": "frontend_url",
        "display_name": "Vallotton·前端地址",
        "description": (
            "【Vallotton 绘图应用】返回 Vallotton Web 前端的访问地址（https 域名）。"
            "用户想在浏览器里打开 Vallotton、查看完整对话记录或图片时，调用此工具获取链接。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "required": ["url"],
            "properties": {"url": {"type": "string", "description": "前端地址"}},
        },
    },
    {
        "name": "quota",
        "display_name": "Vallotton·额度查询",
        "description": (
            "【Vallotton 绘图应用】查询本绘图服务的额度：NovelAI 订阅/用量状态、"
            "Grok API 的本地累计花费、已生成图片数。与其他应用的额度无关。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "novelai": {"type": "object"},
                "grok": {"type": "object"},
                "images": {"type": "object"},
            },
        },
    },
]


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def _backend(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
    try:
        with httpx.Client(timeout=BACKEND_TIMEOUT) as client:
            if method == "GET":
                resp = client.get(f"{BACKEND_URL}{path}", headers=headers)
            else:
                resp = client.post(f"{BACKEND_URL}{path}", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"无法连接 Vallotton 后端：{exc}"}
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", resp.text[:300])
        except Exception:  # noqa: BLE001
            detail = resp.text[:300]
        return {"ok": False, "error": f"后端返回 {resp.status_code}：{detail}"}
    try:
        return {"ok": True, "data": resp.json()}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "后端返回了非 JSON 响应"}


def dispatch(name: str, arguments: dict) -> dict:
    if name == "chat":
        return _handle_chat(arguments)
    if name == "generate_image":
        return _handle_generate(arguments)
    if name == "frontend_url":
        return _handle_frontend_url(arguments)
    if name == "quota":
        return _handle_quota(arguments)
    return {"ok": False, "error": f"未知工具：{name}"}


def _handle_chat(arguments: dict) -> dict:
    message = str(arguments.get("message") or "").strip()
    images = []
    for key in ("image1", "image2", "image3"):
        obj = arguments.get(key)
        if isinstance(obj, dict) and obj.get("data_base64"):
            images.append(
                {
                    "filename": obj.get("filename") or f"{key}.png",
                    "content_type": obj.get("content_type") or "image/png",
                    "data_base64": obj["data_base64"],
                }
            )
    if not message and not images:
        return {"ok": False, "error": "message 与参考图至少要提供一个"}

    payload: dict = {
        "message": message,
        "images": images,
        "n_images": arguments.get("n_images") or 1,
    }
    conv_id = arguments.get("conversation_id")
    if conv_id:
        payload["conversation_id"] = str(conv_id)

    resp = _backend("POST", "/api/internal/chat", payload)
    if not resp["ok"]:
        return {"ok": False, "error": resp["error"]}
    data = resp["data"]
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "生成失败")}

    result = {
        "conversation_id": data["conversation_id"],
        "reply": data.get("reply", ""),
        "is_image_task": bool(data.get("is_image_task")),
        # 后端已经返回 {filename, content_type, data_base64} 形状，原样内嵌
        "images": data.get("images") or [],
    }
    if data.get("nai_prompts"):
        result["nai_prompts"] = data["nai_prompts"]
    return {"ok": True, "result": result}


def _handle_generate(arguments: dict) -> dict:
    base_prompt = str(arguments.get("base_prompt") or "").strip()
    if not base_prompt:
        return {"ok": False, "error": "base_prompt 不能为空"}

    payload = {
        "base_prompt": base_prompt,
        "character_prompts": [
            str(c) for c in (arguments.get("character_prompts") or []) if str(c).strip()
        ],
        "negative_prompt": str(arguments.get("negative_prompt") or ""),
        "size": arguments.get("size") or "portrait",
        "n_images": arguments.get("n_images") or 1,
    }
    resp = _backend("POST", "/api/internal/generate", payload)
    if not resp["ok"]:
        return {"ok": False, "error": resp["error"]}
    data = resp["data"]
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "生成失败")}
    return {
        "ok": True,
        "result": {
            "images": data.get("images") or [],
            "seeds": data.get("seeds") or [],
            "size": data.get("size"),
        },
    }


def _handle_frontend_url(_arguments: dict) -> dict:
    domain = os.environ.get("DOMAIN", "").strip()
    if not domain:
        return {"ok": False, "error": "DOMAIN 未在 .env 中配置"}
    return {"ok": True, "result": {"url": f"https://{domain}"}}


def _handle_quota(_arguments: dict) -> dict:
    resp = _backend("GET", "/api/internal/quota")
    if not resp["ok"]:
        return {"ok": False, "error": resp["error"]}
    return {"ok": True, "result": resp["data"]}
