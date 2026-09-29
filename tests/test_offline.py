"""Offline end-to-end test: mock dataset server + scripted fake model. No internet, no API key.
   python tests/test_offline.py
"""
import os, sys, json, time, threading, types
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------- mock ExampleKart dataset API ----------------
DATA = {
  "/v1/cases": {"data": [{"id": "CASE-001", "title": "Checkout failing", "affected_area": "checkout"}]},
  "/v1/cases/CASE-001": {"id": "CASE-001", "description": "Orders failing", "affected_area": "checkout", "created_at": "2026-03-14T04:00:00Z"},
  "/v1/cases/CASE-001/alerts": {"data": [{"id": "ALT-0102", "service": "order-service", "severity": "warning", "ts": "2026-03-14T01:10:00Z"}]},
  "/v1/cases/CASE-001/events": {"data": [{"id": "EVT-0101", "type": "config_change", "service": "order-service"}]},
  "/v1/cases/CASE-001/logs": {"data": [{"service": "order-service", "level": "ERROR", "message": "pool exhausted", "ts": "t1"},
                                        {"service": "order-service", "level": "ERROR", "message": "pool exhausted", "ts": "t2"}]},
}
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path).path
        body, code = (DATA[p], 200) if p in DATA else ({"detail": "not found"}, 404)
        self.send_response(code); self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(body).encode())
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["EXAMPLEKART_BASE"] = f"http://127.0.0.1:{srv.server_port}"

from agent import Investigation
from jobs import JobManager

# ---------------- scripted fake LLM (OpenAI-shaped objects) ----------------
def call(i, name, args):
    return types.SimpleNamespace(id=f"c{i}", function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))
def msg(content=None, calls=None):
    return types.SimpleNamespace(content=content, tool_calls=calls, model_extra={})
class FakeClient:
    def __init__(self):
        self.script = [
            msg("Reading the case.", [call(1, "get_case", {})]),
            msg(None, [call(2, "get_alerts", {}), call(3, "get_events", {})]),      # parallel tool calls
            msg(None, [call(4, "search_logs", {"level": "ERROR"})]),
            msg(None, [call(5, "submit_diagnosis", {
                "verdict": "resolve", "root_cause_service": "order-service", "root_cause_category": "resource_exhaustion",
                "confidence": "high", "evidence": ["ALT-0102", "EVT-0101", "ALT-9999"], "explanation": "Pool exhausted.",
                "timeline": ["01:10 warning ALT-0102"], "recommended_action": "Raise pool size.", "decoys_ruled_out": ["gateway deploy"]})]),
            msg("The gateway deploy came later, so it is not the cause."),           # follow-up answer
        ]
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
    def create(self, model, messages, tools=None, **kw):
        m = self.script.pop(0)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=m)],
                                     usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=20))

fake = FakeClient()
mgr = JobManager(investigation_cls=lambda cid, emit: Investigation(cid, emit=emit, client=fake, model="fake"))

def wait(job, want="done", t=10):
    end = time.time() + t
    while time.time() < end and job.status != want: time.sleep(0.05)
    assert job.status == want, (job.status, job.error)

# 1. validation + unknown case
for bad, exc in (("../etc", ValueError), ("CASE-404", LookupError)):
    try: mgr.start(bad); raise SystemExit(f"expected {exc.__name__} for {bad}")
    except exc as e: print("OK rejects", bad, "->", e)

# 2. full investigation
job = mgr.start("case-001"); wait(job)
v = job.view(0)
tools_used = [s["tool"] for s in v["steps"] if s.get("type") == "step"]
assert tools_used == ["get_case", "get_alerts", "get_events", "search_logs"], tools_used
assert any(s.get("type") == "note" for s in v["steps"])
assert v["result"]["verdict"] == "resolve"
assert v["result"]["unverified_ids"] == ["ALT-9999"], v["result"]["unverified_ids"]    # hallucinated id caught
assert v["metrics"]["tool_calls"] == 4 and v["metrics"]["api_requests"] == 4 and v["metrics"]["in_tok"] == 400
print("OK investigation:", tools_used, "| unverified:", v["result"]["unverified_ids"])
print("OK step summaries:", [s["summary"] for s in v["steps"] if s.get("type") == "step"])

# 3. incremental polling
assert len(job.view(v["next"])["steps"]) == 0 and len(job.view(2)["steps"]) == v["next"] - 2
print("OK incremental polling")

# 4. follow-up
mgr.ask(job.id, "Could the gateway deploy be the cause?"); wait(job)
assert job.answers and "not the cause" in job.answers[0]["a"]
print("OK follow-up:", job.answers[0]["a"])

# 5. error path (LLM failure surfaces as job error, not a crash)
class Boom:
    chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("401 bad key"))))
mgr2 = JobManager(investigation_cls=lambda cid, emit: Investigation(cid, emit=emit, client=Boom(), model="x"))
j2 = mgr2.start("CASE-001"); wait(j2, "error")
print("OK error surfaced:", j2.error)

# 6. extra docs tool
from tools import Api
r = Api("CASE-001").run("search_extra_docs", {"q": "connection pool timeout"})
assert r["results"], r
print("OK extra_data search ->", r["results"][0]["file"])
print("\nALL OFFLINE TESTS PASSED")
