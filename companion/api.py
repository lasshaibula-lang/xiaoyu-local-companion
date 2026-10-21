from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import persona, public_settings, save_persona, save_settings, settings
from .knowledge import KnowledgeStore
from .llm import ModelError, usage_stats
from .memory import MemoryStore
from .service import SessionChanged, chat, context_lock, enrich_memory, messages, reset_context, search_knowledge, seed_demo
from .storage import ROOT, get_connection, get_state, init_db, set_state


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="栖语 · 本地陪伴", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_access(request: Request, call_next):
    local_names = {"127.0.0.1", "localhost", "::1"}
    if request.client and request.client.host == "testclient":
        local_names.add("testserver")
    if request.url.hostname not in local_names:
        return JSONResponse({"detail": "此调试台只接受本机访问。"}, status_code=403)
    origin = request.headers.get("origin")
    if origin:
        url = urlparse(origin)
        if url.scheme not in {"http", "https"} or url.hostname not in local_names or url.netloc != request.url.netloc:
            return JSONResponse({"detail": "不允许其他网站访问本地数据。"}, status_code=403)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不允许跨网站修改本地数据。"}, status_code=403)
        if request.method != "DELETE" and "application/json" not in request.headers.get("content-type", ""):
            return JSONResponse({"detail": "请使用 JSON 请求。"}, status_code=415)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def bad_value(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(SessionChanged)
async def old_session(request: Request, exc: SessionChanged):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.get("/api/status")
def status():
    config = settings()
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    warnings = []
    if config["mode"] == "demo":
        warnings.append("当前为规则演示，回复不来自大模型。")
    if config["mode"] == "live" and not config["api_key"]:
        warnings.append("尚未配置 API Key。")
    if get_state("enrichment_error", ""):
        warnings.append(get_state("enrichment_error"))
    usage = usage_stats()
    return {"mode": config["mode"], "provider": config["provider"], "model": config["model"],
            "stats": {"messages": count, "memories": len(MemoryStore().list()), "documents": len(KnowledgeStore().list())},
            "budget": {"monthly_limit": usage["monthly_limit"], "estimated_spend": usage["estimated_spend"]},
            "warnings": warnings}


@app.get("/api/messages")
def get_messages():
    return {"messages": messages(), "summary": get_state("summary", "")}


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(default_factory=lambda: str(uuid4()), max_length=128)


@app.post("/api/chat")
async def send_chat(body: ChatBody, background: BackgroundTasks):
    result = await chat(body.message, body.request_id)
    background.add_task(enrich_memory)
    return result


@app.get("/api/memories")
def list_memories():
    return {"memories": MemoryStore().list()}


@app.post("/api/memories")
def add_memory(body: dict):
    return MemoryStore().add(body.get("content", ""), kind=body.get("kind", "fact"))


@app.patch("/api/memories/{memory_id}")
def update_memory(memory_id: str, body: dict):
    with context_lock:
        result = MemoryStore().update(memory_id, content=body.get("content"), pinned=body.get("pinned"))
        if body.get("content") is not None:
            reset_context()
        return result


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str):
    with context_lock:
        if not MemoryStore().delete(memory_id):
            return JSONResponse({"detail": "记忆不存在或已删除。"}, status_code=404)
        reset_context()
        return {"ok": True}


@app.get("/api/knowledge")
def list_knowledge():
    return {"documents": KnowledgeStore().list(), "vault_path": get_state("vault_path", "")}


@app.post("/api/knowledge/import")
def import_knowledge(body: dict):
    return KnowledgeStore().import_text(body.get("title", ""), body.get("content", ""))


@app.post("/api/knowledge/sync")
def sync_knowledge(body: dict):
    with context_lock:
        path = body.get("vault_path", "")
        result = KnowledgeStore().sync(path)
        set_state("vault_path", path)
        if result["indexed"] or result["deleted"]:
            reset_context()
        return result


@app.post("/api/knowledge/search")
async def find_knowledge(body: dict):
    query = body.get("query", "")
    if not isinstance(query, str) or not 0 < len(query.strip()) <= 2000:
        raise ValueError("请输入 1 到 2000 字的检索问题。")
    return await search_knowledge(query.strip())


@app.delete("/api/knowledge/{document_id}")
def delete_knowledge(document_id: str):
    with context_lock:
        if not KnowledgeStore().delete(document_id):
            return JSONResponse({"detail": "笔记不存在。"}, status_code=404)
        reset_context()
        return {"ok": True}


@app.get("/api/persona")
def get_persona():
    return persona()


@app.put("/api/persona")
def put_persona(body: dict):
    with context_lock:
        result = save_persona(body)
        reset_context()
        return result


@app.get("/api/settings")
def get_settings():
    return public_settings()


@app.put("/api/settings")
def put_settings(body: dict):
    with context_lock:
        before = settings()
        result = save_settings(body)
        after = settings()
        if any(before[key] != after[key] for key in ("mode", "model", "summary_model", "provider", "api_key", "base_url", "embedding_model", "embedding_enabled")):
            reset_context()
        return result


@app.get("/api/usage")
def get_usage():
    return usage_stats()


@app.post("/api/session/reset")
def new_session(body: dict):
    if body.get("delete_messages"):
        raise ValueError("此操作只开启新会话，不删除原始聊天历史。")
    reset_context()
    return {"ok": True}


@app.post("/api/demo/seed")
def add_samples(body: dict):
    return seed_demo()


app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True, check_dir=False), name="web")
