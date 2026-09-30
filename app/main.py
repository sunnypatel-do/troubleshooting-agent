"""
FastAPI web app: chat UI + streaming investigation endpoint.

Run locally:  uvicorn app.main:app --reload --port 8080
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import agent, dataset

app = FastAPI(title="ExampleKart Troubleshooting Bot", version="0.1.0")
STATIC = Path(__file__).resolve().parent.parent / "static"


class ChatRequest(BaseModel):
    case_id: str | None = None   # None → cross-case mode: the bot finds the relevant case(s)
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = []


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": agent.MODEL, "dataset": dataset.DATASET_BASE_URL,
            "inference_key_set": bool(os.getenv("DO_MODEL_ACCESS_KEY") or os.getenv("OPENAI_API_KEY"))}


@app.get("/api/cases")
def cases():
    return {"data": dataset.list_cases()}


@app.get("/api/cases/{case_id}")
def case(case_id: str):
    c = dataset.get_case(case_id)
    if not c:
        raise HTTPException(404, "unknown case")
    return {"data": c}


@app.get("/api/models")
def models():
    """Diagnostic: lists models visible to your key via the inference endpoint.
    If this 401s the key is wrong; if it 404s the base URL is wrong."""
    try:
        ids = sorted(m.id for m in agent.client().models.list().data)
        return {"base_url": agent.INFERENCE_BASE_URL, "configured_model": agent.MODEL,
                "configured_model_available": agent.MODEL in ids, "models": ids}
    except Exception as e:
        raise HTTPException(502, f"{e} (base_url={agent.INFERENCE_BASE_URL})")


@app.get("/api/tools")
def tools():
    """Expose tool schemas so you can inspect what the model can call."""
    return {"case_scoped": dataset.TOOLS, "cross_case": dataset.CROSS_CASE_TOOLS}


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Server-Sent Events: one JSON event per line, `data: {...}\\n\\n`."""
    case_id = (req.case_id or "").strip().upper() or None
    if case_id in ("ALL", "AUTO", "NONE"):
        case_id = None
    def gen():
        for ev in agent.investigate(case_id, req.message, req.history):
            yield f"data: {json.dumps(ev)}\n\n"
        yield "data: {\"type\":\"done\"}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
