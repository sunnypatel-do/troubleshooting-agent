"""
Thin client over the ExampleKart Dataset API plus the tool definitions the
LLM is allowed to call. Every tool maps 1:1 to a GET endpoint on the dataset.

Dataset API: https://examplekart-dataset-ovp33.ondigitalocean.app/openapi.json
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

DATASET_BASE_URL = os.getenv(
    "DATASET_BASE_URL", "https://examplekart-dataset-ovp33.ondigitalocean.app"
).rstrip("/")

# Hard cap on how much of a tool result we hand back to the model. Logs in
# particular can return hundreds of rows; the API itself paginates and gives a
# `hint`, so the model can narrow its query instead of drowning in output.
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "14000"))

_client = httpx.Client(base_url=DATASET_BASE_URL, timeout=20.0)


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    r = _client.get(path, params=clean)
    if r.status_code >= 400:
        # The API returns RFC 7807 problem documents; pass them through so the
        # model can self-correct (e.g. a misspelled service name).
        try:
            return {"error": r.json()}
        except Exception:
            return {"error": {"status": r.status_code, "detail": r.text[:500]}}
    return r.json()


# --------------------------------------------------------------------------- #
# Public helpers used by the web app itself (not only via the model)
# --------------------------------------------------------------------------- #

def list_cases() -> list[dict[str, Any]]:
    return _get("/v1/cases").get("data", [])


def get_case(case_id: str) -> dict[str, Any]:
    return _get(f"/v1/cases/{case_id}").get("data", {})


def get_entities() -> dict[str, Any]:
    return _get("/v1/entities").get("data", {})


# --------------------------------------------------------------------------- #
# Cross-case tools (used when no case is pre-selected)
# --------------------------------------------------------------------------- #

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "my", "i", "me", "for",
         "with", "at", "this", "that", "not", "no", "but", "so", "its", "im", "be", "was", "are", "we",
         "our", "they", "have", "has", "just", "then", "when", "what", "why", "how", "does", "did",
         "please", "again", "still", "every", "time", "try", "tried", "which", "case", "cases", "customer"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in _STOP}


def tool_list_cases(**_) -> Any:
    """All 20 cases: id, opened, affected_area, error_message, description, window."""
    return _get("/v1/cases")


def tool_find_cases(q: str, limit: int = 5, **_) -> Any:
    """Rank cases by keyword overlap between q and description/error/affected_area."""
    qt = _tokens(q)
    scored = []
    for c in list_cases():
        hay = f"{c.get('description','')} {c.get('error_message') or ''} {c.get('affected_area','')}"
        ht = _tokens(hay)
        overlap = qt & ht
        # also credit substring hits on the raw text (e.g. '503', 'uk')
        raw_hits = sum(1 for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) >= 2 and w in hay.lower())
        score = len(overlap) * 2 + raw_hits
        if score:
            scored.append({**c, "score": score, "matched_terms": sorted(overlap)})
    scored.sort(key=lambda x: -x["score"])
    return {"data": scored[:limit], "query": q,
            "hint": "Confirm the match by calling get_case and get_alerts for the top candidates. "
                    "If nothing scores, call list_cases and read all descriptions."}


def _fan_out(path_suffix: str, params: dict[str, Any], per_case_limit: int) -> Any:
    cases = list_cases()
    params = {**params, "limit": per_case_limit}
    out = []

    def one(c):
        r = _get(f"/v1/cases/{c['case_id']}{path_suffix}", params)
        rows = r.get("data", []) if isinstance(r, dict) else []
        return {"case_id": c["case_id"], "affected_area": c["affected_area"],
                "matched": (r.get("page") or {}).get("total_matched", len(rows)), "sample": rows}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(one, cases):
            if res["matched"]:
                out.append(res)
    out.sort(key=lambda x: -x["matched"])
    return {"data": out, "cases_with_matches": len(out), "cases_searched": len(cases)}


def tool_search_alerts_all_cases(name: str | None = None, service: str | None = None,
                                 severity: str | None = None, **_) -> Any:
    """Which cases had an alert with this name/service/severity? Name is matched client-side."""
    res = _fan_out("/alerts", {"service": service, "severity": severity}, per_case_limit=20)
    if name:
        n = name.lower()
        for r in res["data"]:
            r["sample"] = [a for a in r["sample"] if n in a.get("name", "").lower()]
            r["matched"] = len(r["sample"])
        res["data"] = [r for r in res["data"] if r["matched"]]
        res["cases_with_matches"] = len(res["data"])
    for r in res["data"]:
        r["sample"] = [{k: a.get(k) for k in ("alert_id", "name", "service", "severity", "state", "started_at")} for a in r["sample"][:5]]
    return res


def tool_search_logs_all_cases(q: str, service: str | None = None, level: str | None = None, **_) -> Any:
    """Which cases contain log lines matching substring q? Returns per-case counts and 3 samples."""
    res = _fan_out("/logs", {"q": q, "service": service, "level": level}, per_case_limit=3)
    for r in res["data"]:
        r["sample"] = [{k: l.get(k) for k in ("ts", "service", "level", "msg")} for l in r["sample"]]
    return res


def tool_search_events_all_cases(type: str | None = None, service: str | None = None, **_) -> Any:
    """Which cases had a change event of this type/service (e.g. all deploys to checkout-service)?"""
    res = _fan_out("/events", {"type": type, "service": service}, per_case_limit=5)
    for r in res["data"]:
        r["sample"] = [{k: e.get(k) for k in ("event_id", "ts", "type", "service", "summary")} for e in r["sample"]]
    return res


# --------------------------------------------------------------------------- #
# Case-scoped tool implementations. Signature: fn(case_id, **args)
# --------------------------------------------------------------------------- #

def tool_get_case(case_id: str) -> Any:
    return _get(f"/v1/cases/{case_id}")


def tool_get_entities(case_id: str) -> Any:
    return _get("/v1/entities")


def tool_get_alerts(case_id: str, **kw) -> Any:
    return _get(f"/v1/cases/{case_id}/alerts", kw)


def tool_get_events(case_id: str, **kw) -> Any:
    return _get(f"/v1/cases/{case_id}/events", kw)


def tool_search_logs(case_id: str, **kw) -> Any:
    kw.setdefault("limit", 25)
    return _get(f"/v1/cases/{case_id}/logs", kw)


def tool_metrics_catalog(case_id: str) -> Any:
    return _get(f"/v1/cases/{case_id}/metrics/catalog")


def tool_query_metric(case_id: str, **kw) -> Any:
    kw.setdefault("step", 300)  # 5-minute buckets keeps a 3h window to ~36 points
    return _get(f"/v1/cases/{case_id}/metrics/query", kw)


def tool_search_traces(case_id: str, **kw) -> Any:
    kw.setdefault("limit", 15)
    return _get(f"/v1/cases/{case_id}/traces", kw)


def tool_get_trace(case_id: str, trace_id: str) -> Any:
    return _get(f"/v1/cases/{case_id}/traces/{trace_id}")


def tool_kb_search(case_id: str, **kw) -> Any:
    kw.setdefault("limit", 8)
    return _get("/v1/kb/search", kw)


def tool_kb_doc(case_id: str, doc_id: str) -> Any:
    return _get(f"/v1/kb/{doc_id}")


CROSS_CASE_IMPLS = {
    "list_cases": tool_list_cases,
    "find_cases": tool_find_cases,
    "search_alerts_all_cases": tool_search_alerts_all_cases,
    "search_logs_all_cases": tool_search_logs_all_cases,
    "search_events_all_cases": tool_search_events_all_cases,
}

TOOL_IMPLS = {
    "get_case": tool_get_case,
    "get_entities": tool_get_entities,
    "get_alerts": tool_get_alerts,
    "get_events": tool_get_events,
    "search_logs": tool_search_logs,
    "metrics_catalog": tool_metrics_catalog,
    "query_metric": tool_query_metric,
    "search_traces": tool_search_traces,
    "get_trace": tool_get_trace,
    "kb_search": tool_kb_search,
    "kb_doc": tool_kb_doc,
}

_TIME = {"type": "string", "description": "RFC3339 timestamp with timezone, e.g. 2026-03-14T02:00:00Z"}
_SERVICE = {"type": "string", "description": "Service or infra entity name, e.g. checkout-service, postgres-primary"}
_ORDER = {"type": "string", "enum": ["asc", "desc"], "description": "Sort order by time. Default desc (newest first)."}
_LIMIT = {"type": "integer", "description": "Max rows to return"}

_CASE_ID = {"type": "string", "description": "Case to query, e.g. CASE-007. Required unless a case is pre-selected."}

# Cross-case tools, only offered when no case is pre-selected.
CROSS_CASE_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "list_cases",
        "description": "List all 20 support cases with id, opened time, affected area, customer error and description. Use to see the whole request set.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "find_cases",
        "description": "Rank cases whose customer description/error/affected area match a question or complaint. First step when the user has not named a case.",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "The user's complaint or key symptoms, e.g. 'charged twice no confirmation email'"},
            "limit": {"type": "integer", "description": "Default 5"}},
            "required": ["q"]}}},
    {"type": "function", "function": {
        "name": "search_alerts_all_cases",
        "description": "Across ALL cases: which ones had an alert matching a name substring, service or severity? Returns per-case counts and samples.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Alert name substring, e.g. KafkaConsumerLag"},
            "service": _SERVICE,
            "severity": {"type": "string", "enum": ["critical", "warning", "info"]}}}}},
    {"type": "function", "function": {
        "name": "search_logs_all_cases",
        "description": "Across ALL cases: which ones contain log lines matching substring q (optionally filtered by service/level)? Per-case counts plus 3 samples each.",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string"}, "service": _SERVICE,
            "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "DEBUG"]}},
            "required": ["q"]}}},
    {"type": "function", "function": {
        "name": "search_events_all_cases",
        "description": "Across ALL cases: which ones had a change event of this type and/or service (e.g. every deploy of checkout-service)?",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "description": "deploy | config_change | schema_migration | scaling | infra_maintenance"},
            "service": _SERVICE}}}},
]

# OpenAI-style function tool schemas (works with Chat Completions on DO Serverless Inference)
TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "get_case",
        "description": "Get the customer complaint, error message, affected area and the 3-hour evidence window for the current case.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID}}}},
    {"type": "function", "function": {
        "name": "get_entities",
        "description": "Inventory of the 10 services, 6 infra components and 4 third parties, with hosts and roles. Use to validate service names.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID}}}},
    {"type": "function", "function": {
        "name": "get_alerts",
        "description": "Alerts that fired in the case window. Includes severity, state, observed value vs threshold and a runbook reference (RB-xxx).",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID,
            "service": _SERVICE,
            "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
            "state": {"type": "string", "enum": ["firing", "resolved"]},
            "from": _TIME, "to": _TIME, "order": _ORDER, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "get_events",
        "description": "Change events in the window: deploys, config changes, schema migrations, scaling, infra maintenance. Key for 'what changed?'.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID,
            "type": {"type": "string", "description": "deploy | config_change | schema_migration | scaling | infra_maintenance"},
            "service": _SERVICE, "from": _TIME, "to": _TIME, "order": _ORDER, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "search_logs",
        "description": "Search application logs. Results are paginated and newest-first; narrow with level, service, substring q, trace_id or a time range.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID,
            "service": _SERVICE,
            "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "DEBUG"]},
            "q": {"type": "string", "description": "Case-insensitive substring to match in the log message"},
            "trace_id": {"type": "string"},
            "host": {"type": "string"},
            "from": _TIME, "to": _TIME, "order": _ORDER, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "metrics_catalog",
        "description": "List metric names available for this case and which entities emit each one. Call before query_metric.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID}}}},
    {"type": "function", "function": {
        "name": "query_metric",
        "description": "Time series for one metric (from metrics_catalog), optionally for one service. Returns points across the case window.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID,
            "metric": {"type": "string", "description": "e.g. error_rate, http_request_duration_p99, disk_used_pct, kafka_consumer_lag"},
            "service": _SERVICE, "from": _TIME, "to": _TIME,
            "step": {"type": "integer", "description": "Bucket size in seconds. Default 300."}},
            "required": ["metric"]}}},
    {"type": "function", "function": {
        "name": "search_traces",
        "description": "Find distributed traces. Filter by root service, status (ok|error), operation name or min duration to find slow/failed requests.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID,
            "service": _SERVICE,
            "status": {"type": "string", "enum": ["ok", "error"]},
            "name": {"type": "string", "description": "Operation name substring, e.g. POST /api/v2/checkout"},
            "min_duration_ms": {"type": "integer"},
            "from": _TIME, "to": _TIME, "order": _ORDER, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "get_trace",
        "description": "Full span tree for one trace_id, showing which downstream service failed or was slow.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID, "trace_id": {"type": "string"}}, "required": ["trace_id"]}}},
    {"type": "function", "function": {
        "name": "kb_search",
        "description": "Search the knowledge base: runbooks (RB-xxx), postmortems (PM-xxxx-xx), architecture docs (ARCH-xxx), ownership/escalation (OWN-xxx). Check the `stale` flag.",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID,
            "q": {"type": "string", "description": "Free-text query"},
            "type": {"type": "string", "enum": ["runbook", "postmortem", "architecture", "ownership"]},
            "service": _SERVICE, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "kb_doc",
        "description": "Read a full knowledge-base document by doc_id (e.g. RB-002, PM-2026-01, OWN-001).",
        "parameters": {"type": "object", "properties": {"case_id": _CASE_ID, "doc_id": {"type": "string"}}, "required": ["doc_id"]}}},
]


def run_tool(name: str, case_id: str | None, args: dict[str, Any]) -> str:
    """Execute a tool and return a string for the model, truncated if huge.
    `case_id` is the pre-selected case (may be None in cross-case mode); an explicit
    args["case_id"] from the model always wins."""
    args = dict(args)
    explicit = args.pop("case_id", None)
    try:
        if name in CROSS_CASE_IMPLS:
            result = CROSS_CASE_IMPLS[name](**args)
        elif name in TOOL_IMPLS:
            cid = explicit or case_id
            if not cid:
                return json.dumps({"error": "case_id is required. Call find_cases or list_cases first to identify the case."})
            result = TOOL_IMPLS[name](cid, **args)
        else:
            return json.dumps({"error": f"unknown tool {name}"})
    except TypeError as e:
        return json.dumps({"error": f"bad arguments for {name}: {e}"})
    except httpx.HTTPError as e:
        return json.dumps({"error": f"dataset API unreachable: {e}"})
    text = json.dumps(result, separators=(",", ":"))
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '..."[TRUNCATED: narrow the query with filters or a smaller limit]'
    return text
