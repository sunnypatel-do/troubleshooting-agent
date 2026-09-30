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
    case_id: str
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


@app.get("/api/tools")
def tools():
    """Expose tool schemas so you can inspect what the model can call."""
    return {"data": dataset.TOOLS}


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Server-Sent Events: one JSON event per line, `data: {...}\\n\\n`."""
    def gen():
        for ev in agent.investigate(req.case_id, req.message, req.history):
            yield f"data: {json.dumps(ev)}\n\n"
        yield "data: {\"type\":\"done\"}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
