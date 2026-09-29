"""FastAPI app: serves the UI and a small JSON API.

  uvicorn app:app --host 0.0.0.0 --port 8080
"""
import os, secrets
import requests
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import agent, tools
from jobs import JobManager

app = FastAPI(title="ExampleKart Troubleshooting Agent")
manager = JobManager()
security = HTTPBasic(auto_error=False)
HERE = os.path.dirname(__file__)

def auth(creds: HTTPBasicCredentials = Depends(security)):
    """Optional HTTP Basic auth. Enabled only if BASIC_AUTH_USER and BASIC_AUTH_PASS are set."""
    u, p = os.environ.get("BASIC_AUTH_USER"), os.environ.get("BASIC_AUTH_PASS")
    if not (u and p): return
    if not creds or not (secrets.compare_digest(creds.username, u) and secrets.compare_digest(creds.password, p)):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": 'Basic realm="agent"'})

AUTH = [Depends(auth)]

class NewInvestigation(BaseModel):
    case_id: str

class Question(BaseModel):
    question: str

@app.get("/healthz")                       # open on purpose: used by App Platform health checks
def healthz(): return {"ok": True}

@app.get("/", dependencies=AUTH)
def index(): return FileResponse(os.path.join(HERE, "static", "index.html"))

@app.get("/api/cases", dependencies=AUTH)
def cases(): return tools.Api().run("list_cases", {})

@app.post("/api/investigations", dependencies=AUTH)
def start(body: NewInvestigation):
    try: job = manager.start(body.case_id)
    except ValueError as e: raise HTTPException(400, str(e))
    except LookupError as e: raise HTTPException(404, str(e))
    return {"id": job.id}

@app.get("/api/investigations/{job_id}", dependencies=AUTH)
def status(job_id: str, since: int = 0):
    job = manager.get(job_id)
    if not job: raise HTTPException(404, "Unknown investigation (the server may have restarted)")
    return job.view(since)

@app.post("/api/investigations/{job_id}/ask", dependencies=AUTH)
def ask(job_id: str, body: Question):
    q = body.question.strip()[:1000]
    if not q: raise HTTPException(400, "Empty question")
    try: manager.ask(job_id, q)
    except KeyError: raise HTTPException(404, "Unknown investigation")
    except RuntimeError as e: raise HTTPException(409, str(e))
    return {"ok": True}

@app.get("/api/diagnostics", dependencies=AUTH)
def diagnostics():
    """Self-check: is the dataset reachable, which paths does it expose, does the inference key/model work?"""
    out = {"model": agent.MODEL, "llm_base_url": agent.BASE_URL, "dataset_base": tools.BASE}
    try:
        out["dataset_healthz"] = requests.get(f"{tools.BASE}/healthz", timeout=10).status_code
    except Exception as e: out["dataset_healthz"] = f"unreachable: {e}"
    try:
        spec = requests.get(f"{tools.BASE}/openapi.json", timeout=10).json()
        out["dataset_paths"] = sorted(spec.get("paths", {}))
    except Exception as e: out["dataset_paths"] = f"could not read /openapi.json: {e}"
    try:
        n = tools.Api().run("list_cases", {})
        out["cases_found"] = len(n) if isinstance(n, list) else n
    except Exception as e: out["cases_found"] = f"error: {e}"
    try:
        ids = [m.id for m in agent.get_client().models.list().data]
        out["inference_key"] = "ok"; out["model_available"] = agent.MODEL in ids
        if agent.MODEL not in ids: out["hint"] = "MODEL not in your catalog; pick one from tool_calling_models_sample"
        out["tool_calling_models_sample"] = [i for i in ids if any(k in i for k in ("claude", "gpt-5", "glm", "deepseek", "kimi"))][:25]
    except Exception as e: out["inference_key"] = f"problem: {type(e).__name__}: {str(e)[:200]}"
    return out
