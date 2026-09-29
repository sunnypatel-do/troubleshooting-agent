"""Tools for the troubleshooting agent / chatbot.

Data sources
  1. ExampleKart Dataset API  (cases, alerts, events, metrics, logs, traces, entities, KB)
  2. extra_data/ folder       (ANY .md/.txt/.json you drop in: runbooks, postmortems, notes)

Two modes
  case_bound=True   batch/benchmark mode: runner fixes the case_id (model never passes it)
  case_bound=False  chatbot mode: model passes case_id, plus list_cases to find cases
"""
import os, re, json, math, glob, collections
import requests

BASE = os.environ.get("EXAMPLEKART_BASE", "https://examplekart-dataset-ovp33.ondigitalocean.app")
EXTRA_DIR = os.environ.get("EXTRA_DATA_DIR", os.path.join(os.path.dirname(__file__), "extra_data"))

# ------------------------------------------------------------------ schemas
def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}

TIME = {"from": {"type": "string", "description": "RFC3339 UTC start (inside the case window)"},
        "to":   {"type": "string", "description": "RFC3339 UTC end"}}
CASE_ID = {"case_id": {"type": "string", "description": "e.g. CASE-001"}}

def _base_tools():
    return [
  _fn("get_case", "Customer report: description, error_message, affected_area, created_at, evidence window. "
      "affected_area is where the SYMPTOM shows, usually NOT the faulty service. Missing error_message = silent failure."),
  _fn("get_alerts", "Alerts in the window. Read BY TIME (order=asc): early warnings are often the cause, critical alerts "
      "often late symptoms. Each cites a runbook id.",
      {"service": {"type": "string"}, "severity": {"type": "string", "enum": ["warning", "critical"]},
       "state": {"type": "string", "enum": ["firing", "resolved"]},
       "order": {"type": "string", "enum": ["asc", "desc"], "default": "asc"}, **TIME}),
  _fn("get_events", "Change feed: deploys, config, flags, scaling, cert/secret rotation, migrations, maintenance. "
      "2-3 per case are decoys: test timing AND mechanism.",
      {"type": {"type": "string"}, "service": {"type": "string"},
       "order": {"type": "string", "enum": ["asc", "desc"], "default": "asc"}, **TIME}),
  _fn("get_metrics_catalog", "Which metric series exist for this case. Call before query_metric."),
  _fn("query_metric", "One metric as a time series (downsampled). Compare LEVELS before vs after onset; series are noisy.",
      {"metric": {"type": "string"}, "service": {"type": "string"}, "step": {"type": "string"}, **TIME}, ["metric"]),
  _fn("search_logs", "Structured logs, deduplicated by message with counts. Start narrow (service + level or q). "
      "No ERROR logs does not mean no fault.",
      {"service": {"type": "string"}, "level": {"type": "string", "enum": ["INFO", "WARN", "ERROR"]},
       "q": {"type": "string"}, "trace_id": {"type": "string"}, "host": {"type": "string"},
       "order": {"type": "string", "enum": ["asc", "desc"]}, "limit": {"type": "integer", "default": 50}, **TIME}),
  _fn("list_traces", "Trace summaries. Use status=error, then get_trace on one representative id.",
      {"service": {"type": "string"}, "status": {"type": "string"}, "name": {"type": "string"},
       "min_duration_ms": {"type": "integer"}, "limit": {"type": "integer", "default": 10}, **TIME}),
  _fn("get_trace", "Full span tree of one trace; the deepest failing span shows where the fault originates.",
      {"trace_id": {"type": "string"}}, ["trace_id"]),
    ]

def _shared_tools():
    return [
  _fn("search_kb", "Search the built-in KB (runbooks, postmortems, architecture, ownership). Flags stale=true docs: do not "
      "trust them without corroborating telemetry.",
      {"q": {"type": "string"}, "type": {"type": "string", "enum": ["runbook", "postmortem", "architecture", "ownership"]},
       "service": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, ["q"]),
  _fn("get_kb_doc", "Read one KB document by id (e.g. RB-002).", {"doc_id": {"type": "string"}}, ["doc_id"]),
  _fn("get_entities", "Inventory of services, infrastructure and third parties. Third parties have NO telemetry "
      "(only client spans in traces)."),
  _fn("search_extra_docs", "Search user-added documents in extra_data/ (custom runbooks, postmortems, notes, JSON). "
      "Use for anything not covered by the built-in KB, or for questions not tied to a case. Returns file + snippet.",
      {"q": {"type": "string"}, "limit": {"type": "integer", "default": 4}}, ["q"]),
    ]

SUBMIT = _fn("submit_diagnosis", "FINAL step. Call once you can defend a diagnosis (or must abstain). Cite only IDs you actually saw in tool results.",
  {"verdict": {"type": "string", "enum": ["resolve", "insufficient_evidence", "not_our_problem"]},
   "root_cause_service": {"type": "string"},
   "root_cause_category": {"type": "string", "enum": [
       "bad_deploy","config_change","resource_exhaustion","dependency_failure_external","data_schema",
       "capacity_traffic","cert_expiry","cache_behavior","network_dns","replication_lag","concurrency_race",
       "host_infrastructure","monitoring_defect","client_side","unknown"]},
   "evidence": {"type": "array", "items": {"type": "string"}},
   "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
   "explanation": {"type": "string", "description": "2-5 sentences: the causal chain from root cause to customer symptom"},
   "timeline": {"type": "array", "items": {"type": "string"}, "description": "3-6 short lines, earliest first, with timestamps and IDs"},
   "recommended_action": {"type": "string"},
   "decoys_ruled_out": {"type": "array", "items": {"type": "string"}, "description": "each: what was ruled out and why"}},
  ["verdict","root_cause_service","root_cause_category","confidence","evidence","explanation","timeline","recommended_action"])

CASE_SCOPED = {"get_case","get_alerts","get_events","get_metrics_catalog","query_metric","search_logs","list_traces","get_trace"}

def build_tools(case_bound=True, with_submit=True):
    tools = _base_tools() + _shared_tools()
    if not case_bound:
        for t in tools:                                   # chatbot: model supplies case_id
            f = t["function"]
            if f["name"] in CASE_SCOPED:
                f["parameters"]["properties"] = {**CASE_ID, **f["parameters"]["properties"]}
                f["parameters"]["required"] = ["case_id"] + f["parameters"]["required"]
        tools.insert(0, _fn("list_cases", "List all cases (id, title/description, affected_area). Use to match a user's symptom "
                            "to a case or when no case id is given."))
    if with_submit:
        tools.append(SUBMIT)
    return tools

# ------------------------------------------------------------------ tolerant response handling
LIST_KEYS = ("data","items","results","cases","alerts","events","logs","traces","spans","docs","entities","points","series")
def rows(resp):
    """Return the list inside a response regardless of wrapper key."""
    if isinstance(resp, list): return resp
    if isinstance(resp, dict):
        for k in LIST_KEYS:
            if isinstance(resp.get(k), list): return resp[k]
        for v in resp.values():
            if isinstance(v, list): return v
    return []

def _first(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None: return d[k]
    return default

def dedupe_logs(resp, keep=25):
    data = rows(resp)
    if not data: return resp
    groups = collections.OrderedDict()
    for l in data:
        msg = str(_first(l, ("msg","message","log","text"), ""))[:160]
        key = (_first(l, ("service","svc")), _first(l, ("level","severity")), msg)
        ts = _first(l, ("timestamp","ts","time","@timestamp"))
        g = groups.setdefault(key, {"service": key[0], "level": key[1], "msg": msg, "count": 0, "first": ts, "last": ts})
        g["count"] += 1; g["last"] = ts
    out = {"total_returned": len(data), "unique_messages": list(groups.values())[:keep]}
    if isinstance(resp, dict):
        for k in ("page","next","next_cursor","hint","total"):
            if k in resp: out[k] = resp[k]
    return out

def summarize_series(resp):
    data = resp
    if isinstance(resp, dict):
        data = _first(resp, ("points","data","values","series"), resp)
        if isinstance(data, dict): data = _first(data, ("points","values","data"), [])
    pts = data if isinstance(data, list) else []
    def val(p):
        if isinstance(p, dict): return _first(p, ("value","v","val"))
        if isinstance(p, (list, tuple)) and len(p) > 1: return p[1]
        return p if isinstance(p, (int, float)) else None
    vals = [v for v in (val(p) for p in pts) if isinstance(v, (int, float))]
    if not vals: return resp
    stride = max(1, len(pts) // 40)
    return {"n": len(vals), "min": min(vals), "max": max(vals), "mean": round(sum(vals)/len(vals), 2),
            "first": vals[0], "last": vals[-1], "points": pts[::stride]}

def slim_cases(resp):
    out = []
    for c in rows(resp):
        out.append({k: (str(v)[:140]) for k, v in c.items()
                    if k in ("id","case_id","title","summary","description","affected_area","created_at","severity")})
    return out or resp

# ------------------------------------------------------------------ extra data (add your own docs here)
class ExtraDocs:
    """Zero-dependency keyword index over extra_data/*.{md,txt,json}. Re-scans when files change."""
    def __init__(self, folder=EXTRA_DIR):
        self.folder, self.chunks, self._sig = folder, [], None

    def _load(self):
        files = sorted(glob.glob(os.path.join(self.folder, "**", "*"), recursive=True))
        sig = [(f, os.path.getmtime(f)) for f in files if os.path.isfile(f)]
        if sig == self._sig: return
        self._sig, self.chunks = sig, []
        for f, _ in sig:
            if not f.lower().endswith((".md",".txt",".json",".log",".csv")): continue
            text = open(f, errors="ignore").read()
            if f.lower().endswith(".json"):
                try: text = json.dumps(json.loads(text), indent=1)
                except Exception: pass
            name, buf = os.path.relpath(f, self.folder), ""
            for para in re.split(r"\n\s*\n", text):
                if len(buf) + len(para) > 900 and buf:
                    self.chunks.append((name, buf.strip())); buf = ""
                buf += para + "\n\n"
            if buf.strip(): self.chunks.append((name, buf.strip()))

    @staticmethod
    def _tok(s): return re.findall(r"[a-z0-9_\-\.]{2,}", s.lower())

    def search(self, q, limit=4):
        self._load()
        if not self.chunks: return {"results": [], "note": f"extra_data/ is empty ({self.folder})"}
        qt, N = set(self._tok(q)), len(self.chunks)
        df = collections.Counter(t for _, c in self.chunks for t in set(self._tok(c)))
        scored = []
        for name, c in self.chunks:
            toks = self._tok(c); tf = collections.Counter(toks)
            s = sum((1 + math.log(tf[t])) * math.log(1 + N / df[t]) for t in qt if t in tf)
            if s > 0: scored.append((s, name, c))
        scored.sort(reverse=True)
        return {"results": [{"file": n, "score": round(s, 2), "snippet": c[:900]} for s, n, c in scored[:limit]]}

_EXTRA = ExtraDocs()

# ------------------------------------------------------------------ executor
class Api:
    def __init__(self, case_id=None):
        self.case_id, self.calls = case_id, 0
        self.s = requests.Session()

    def _get(self, path, params=None):
        self.calls += 1
        try:
            r = self.s.get(f"{BASE}{path}", params={k: v for k, v in (params or {}).items() if v is not None}, timeout=30)
        except requests.RequestException as e:
            return {"error": "network", "detail": str(e)[:200]}
        if r.status_code >= 400:
            return {"error": r.status_code, "detail": r.text[:300]}
        try: return r.json()
        except ValueError: return {"text": r.text[:3000]}

    def run(self, name, a):
        a = dict(a or {})
        cid = a.pop("case_id", None) or self.case_id
        if name == "search_extra_docs": return _EXTRA.search(a["q"], a.get("limit", 4))
        if name == "search_kb":         return self._get("/v1/kb/search", a)
        if name == "get_kb_doc":        return self._get(f"/v1/kb/{a['doc_id']}")
        if name == "get_entities":      return self._get("/v1/entities")
        if name == "list_cases":        return slim_cases(self._get("/v1/cases"))
        if name in CASE_SCOPED and not cid: return {"error": "case_id required"}
        c = f"/v1/cases/{cid}"
        if name == "get_case":            return self._get(c)
        if name == "get_alerts":          return self._get(f"{c}/alerts", {"order": "asc", **a})
        if name == "get_events":          return self._get(f"{c}/events", {"order": "asc", **a})
        if name == "get_metrics_catalog": return self._get(f"{c}/metrics/catalog")
        if name == "query_metric":        return summarize_series(self._get(f"{c}/metrics/query", {"step": "5m", **a}))
        if name == "search_logs":         return dedupe_logs(self._get(f"{c}/logs", {"limit": 100, **a}))
        if name == "list_traces":         return self._get(f"{c}/traces", a)
        if name == "get_trace":           return self._get(f"{c}/traces/{a['trace_id']}")
        return {"error": f"unknown tool {name}"}

TOOLS = build_tools(case_bound=True, with_submit=True)   # backwards compatible with agent.py
