"""FastAPI 后端。

端点：
  GET    /api/documents
  POST   /api/documents
  GET    /api/documents/{id}
  PUT    /api/documents/{id}            正文替换（乐观锁 expectedRevision）
  GET    /api/documents/{id}/diagnostics
  POST   /api/documents/{id}/analyze    触发绑定 revision 的异步分析
  POST   /api/rename/preview
  POST   /api/rename/commit
  WS     /ws                             工作区修订广播

WebSocket 广播消息：
  {"type":"document_changed", "document": {...精简...}, "revision": n}
  {"type":"diagnostics", "docId": ..., "revision": n, "diagnostics": [...]}
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .store import ConflictError, Store, StoreError, apply_edits_preview

DB_PATH = os.environ.get("WORKSPACE_DB", str(Path(__file__).parent / "workspace.db"))

app = FastAPI(title="Mini-language workspace")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

store = Store(DB_PATH)


# ---------- WebSocket 广播 ----------


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message, ensure_ascii=False)
        async with self._lock:
            dead = []
            for ws in self._clients:
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)


hub = Hub()


def _brief(doc: dict) -> dict:
    return {
        "id": doc["id"],
        "title": doc["title"],
        "revision": doc["revision"],
        "updatedAt": doc["updatedAt"],
    }


async def _notify_change(doc: dict) -> None:
    await hub.broadcast(
        {
            "type": "document_changed",
            "document": _brief(doc),
            "revision": doc["revision"],
        }
    )


# ---------- 模型 ----------


class CreateDoc(BaseModel):
    title: str
    content: str = ""


class ReplaceDoc(BaseModel):
    content: str
    expectedRevision: int | None = None


class AnalyzeReq(BaseModel):
    content: str
    revision: int


class RenameReq(BaseModel):
    oldName: str = Field(..., min_length=1)
    newName: str = Field(..., min_length=1)


class CommitReq(BaseModel):
    planId: str


# ---------- 文档端点 ----------


@app.get("/api/documents")
def list_documents() -> dict:
    return {"documents": store.list_documents(), "maxDocuments": 20}


@app.post("/api/documents", status_code=201)
async def create_document(req: CreateDoc) -> dict:
    try:
        doc = store.create_document(req.title, req.content)
    except StoreError as e:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"detail": str(e)})
    await _notify_change(doc)
    return doc


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str) -> dict:
    try:
        return store.get_document(doc_id)
    except StoreError as e:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(e)})


@app.put("/api/documents/{doc_id}")
async def replace_document(doc_id: str, req: ReplaceDoc) -> dict:
    try:
        doc = store.replace_document(doc_id, req.content, req.expectedRevision)
    except ConflictError as e:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(e)})
    except StoreError as e:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": str(e)})
    await _notify_change(doc)
    return doc


@app.get("/api/documents/{doc_id}/diagnostics")
def get_diagnostics(doc_id: str) -> dict:
    return store.get_diagnostics(doc_id)


@app.post("/api/documents/{doc_id}/analyze")
async def analyze(doc_id: str, req: AnalyzeReq) -> dict:
    """绑定修订的异步分析。

    解析放到线程池（模拟分析开销）；回写时 store 以 revision 为门闩——
    若文档在分析期间被改过，迟到的旧结果直接丢弃。
    """
    await asyncio.sleep(0.05)  # 放大竞态，便于观察旧诊断被丢弃
    result = await asyncio.to_thread(store.analyze, doc_id, req.content, req.revision)
    if result is None:
        return {
            "stale": True,
            "docId": doc_id,
            "revision": req.revision,
            "diagnostics": [],
        }
    await hub.broadcast({"type": "diagnostics", **result})
    return {"stale": False, **result}


# ---------- 重命名 ----------


@app.post("/api/rename/preview")
def rename_preview(req: RenameReq) -> dict:
    try:
        plan = store.build_rename_plan(req.oldName, req.newName)
    except StoreError as e:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"detail": str(e)})

    # 附带每个文档的改前/改后片段预览
    docs = {d["id"]: d for d in store.list_documents()}
    previews = []
    edits_by_doc: dict[str, list[dict]] = {}
    for ed in plan["edits"]:
        edits_by_doc.setdefault(ed["docId"], []).append(ed)
    for doc_id, eds in edits_by_doc.items():
        d = docs[doc_id]
        before = d["content"]
        after = apply_edits_preview(before, eds)
        previews.append(
            {
                "docId": doc_id,
                "title": d["title"],
                "baseRevision": plan["baselines"][doc_id],
                "editCount": len(eds),
                "before": before,
                "after": after,
            }
        )
    return {**plan, "previews": previews}


@app.post("/api/rename/commit")
async def rename_commit(req: CommitReq) -> dict:
    try:
        result = await asyncio.to_thread(store.commit_rename, req.planId)
    except ConflictError as e:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(e)})
    # 每个被改的文档都广播一次（同一 revision）
    docs = {d["id"]: d for d in store.list_documents()}
    for ref in result["documents"]:
        await _notify_change(docs[ref["id"]])
    return result


# ---------- WebSocket ----------


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        # 连上时先推一份当前工作区快照，便于会话对齐
        await ws.send_text(
            json.dumps(
                {
                    "type": "snapshot",
                    "documents": [_brief(d) for d in store.list_documents()],
                },
                ensure_ascii=False,
            )
        )
        while True:
            # 客户端暂不需要发消息；保持连接、探测断开
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)


class RenameGroupReq(BaseModel):
    pairs: list[RenameReq]


@app.post("/api/rename/group/preview")
def rename_group_preview(req: RenameGroupReq):
    from .rename_group import preview
    from fastapi.responses import JSONResponse

    try:
        return preview(store, [p.model_dump() for p in req.pairs])
    except StoreError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
