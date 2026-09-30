"""
Thin client over the ExampleKart Dataset API plus the tool definitions the
LLM is allowed to call. Every tool maps 1:1 to a GET endpoint on the dataset.

Dataset API: https://examplekart-dataset-ovp33.ondigitalocean.app/openapi.json
"""
from __future__ import annotations

import json
import os
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
# Tool implementations. Signature: fn(case_id, **args) -> JSON-serialisable
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

# OpenAI-style function tool schemas (works with Chat Completions on DO Serverless Inference)
TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "get_case",
        "description": "Get the customer complaint, error message, affected area and the 3-hour evidence window for the current case.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_entities",
        "description": "Inventory of the 10 services, 6 infra components and 4 third parties, with hosts and roles. Use to validate service names.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_alerts",
        "description": "Alerts that fired in the case window. Includes severity, state, observed value vs threshold and a runbook reference (RB-xxx).",
        "parameters": {"type": "object", "properties": {
            "service": _SERVICE,
            "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
            "state": {"type": "string", "enum": ["firing", "resolved"]},
            "from": _TIME, "to": _TIME, "order": _ORDER, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "get_events",
        "description": "Change events in the window: deploys, config changes, schema migrations, scaling, infra maintenance. Key for 'what changed?'.",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "description": "deploy | config_change | schema_migration | scaling | infra_maintenance"},
            "service": _SERVICE, "from": _TIME, "to": _TIME, "order": _ORDER, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "search_logs",
        "description": "Search application logs. Results are paginated and newest-first; narrow with level, service, substring q, trace_id or a time range.",
        "parameters": {"type": "object", "properties": {
            "service": _SERVICE,
            "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "DEBUG"]},
            "q": {"type": "string", "description": "Case-insensitive substring to match in the log message"},
            "trace_id": {"type": "string"},
            "host": {"type": "string"},
            "from": _TIME, "to": _TIME, "order": _ORDER, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "metrics_catalog",
        "description": "List metric names available for this case and which entities emit each one. Call before query_metric.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "query_metric",
        "description": "Time series for one metric (from metrics_catalog), optionally for one service. Returns points across the case window.",
        "parameters": {"type": "object", "properties": {
            "metric": {"type": "string", "description": "e.g. error_rate, http_request_duration_p99, disk_used_pct, kafka_consumer_lag"},
            "service": _SERVICE, "from": _TIME, "to": _TIME,
            "step": {"type": "integer", "description": "Bucket size in seconds. Default 300."}},
            "required": ["metric"]}}},
    {"type": "function", "function": {
        "name": "search_traces",
        "description": "Find distributed traces. Filter by root service, status (ok|error), operation name or min duration to find slow/failed requests.",
        "parameters": {"type": "object", "properties": {
            "service": _SERVICE,
            "status": {"type": "string", "enum": ["ok", "error"]},
            "name": {"type": "string", "description": "Operation name substring, e.g. POST /api/v2/checkout"},
            "min_duration_ms": {"type": "integer"},
            "from": _TIME, "to": _TIME, "order": _ORDER, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "get_trace",
        "description": "Full span tree for one trace_id, showing which downstream service failed or was slow.",
        "parameters": {"type": "object", "properties": {"trace_id": {"type": "string"}}, "required": ["trace_id"]}}},
    {"type": "function", "function": {
        "name": "kb_search",
        "description": "Search the knowledge base: runbooks (RB-xxx), postmortems (PM-xxxx-xx), architecture docs (ARCH-xxx), ownership/escalation (OWN-xxx). Check the `stale` flag.",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "Free-text query"},
            "type": {"type": "string", "enum": ["runbook", "postmortem", "architecture", "ownership"]},
            "service": _SERVICE, "limit": _LIMIT}}}},
    {"type": "function", "function": {
        "name": "kb_doc",
        "description": "Read a full knowledge-base document by doc_id (e.g. RB-002, PM-2026-01, OWN-001).",
        "parameters": {"type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"]}}},
]


def run_tool(name: str, case_id: str, args: dict[str, Any]) -> str:
    """Execute a tool and return a string for the model, truncated if huge."""
    fn = TOOL_IMPLS.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        result = fn(case_id, **args)
    except TypeError as e:
        return json.dumps({"error": f"bad arguments for {name}: {e}"})
    except httpx.HTTPError as e:
        return json.dumps({"error": f"dataset API unreachable: {e}"})
    text = json.dumps(result, separators=(",", ":"))
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + '..."[TRUNCATED: narrow the query with filters or a smaller limit]'
    return text
