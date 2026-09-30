"""
Agentic investigation loop.

The model (served by DigitalOcean Serverless Inference, OpenAI-compatible)
decides which dataset tools to call, we execute them, feed results back, and
repeat until it produces a final answer. Each step is yielded as an event so
the UI can render the investigation trail live.
"""
from __future__ import annotations

import json
import os
from typing import Any, Generator

from openai import OpenAI

from . import dataset

def _normalise_base_url(url: str) -> str:
    """DO docs quote https://inference.do-ai.run; the OpenAI SDK needs the /v1 suffix.
    Without it every call 404s with {'detail': 'Not Found'}."""
    url = url.strip().rstrip("/")
    return url if url.endswith("/v1") else url + "/v1"


INFERENCE_BASE_URL = _normalise_base_url(os.getenv("INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"))
MODEL = os.getenv("MODEL", "anthropic-claude-sonnet-5.5").strip()
MAX_STEPS = int(os.getenv("MAX_STEPS", "16"))  # cross-case runs need a few extra steps

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        key = os.getenv("DO_MODEL_ACCESS_KEY") or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Set DO_MODEL_ACCESS_KEY (DigitalOcean model access key).")
        _client = OpenAI(base_url=INFERENCE_BASE_URL, api_key=key)
    return _client


SYSTEM_PROMPT = """You are the ExampleKart on-call troubleshooting assistant. ExampleKart is an
e-commerce platform with 10 services (web-frontend, api-gateway, auth-service, catalog-service,
search-service, cart-service, checkout-service, payment-adapter, order-service, notification-worker),
6 infra components (postgres-primary, postgres-replica, valkey, kafka, spaces-cdn, lb-public) and
4 uninstrumented third parties (paygate, taxcalc, mailsend, addrverify).

Each support case is a 3-hour evidence window ending when the case was opened. You have read-only
tools over alerts, change events, logs, metrics, traces and a knowledge base (runbooks, postmortems,
architecture, ownership).

Method — work like a senior SRE:
1. Start with get_case, then get_alerts and get_events to see what fired and what changed.
2. Form 1-2 hypotheses. Test each with targeted search_logs / query_metric / search_traces calls.
   Prefer narrow queries (service + level/q + time range) over broad dumps. Follow a failing trace
   into get_trace to find the *first* failing span.
3. Correlate timing: onset of the customer symptom vs. alert start vs. deploy/config/migration time.
4. Consult kb_search for the matching runbook/postmortem; if a KB doc is marked stale, say so.
5. Use OWN-001 for who to escalate to.

Answer rules:
- Every claim must cite the evidence: alert IDs, event IDs, log lines (service, time, message),
  metric values, trace IDs, KB doc IDs. Never invent data that a tool did not return.
- Distinguish confirmed facts from hypotheses. If evidence is missing, say what you would check next.
- Answer the user's actual question. If they ask a narrow question ("did the customer get charged twice?"),
  answer that first, then add context.
- When asked for root cause, structure the answer as: Summary · Evidence · Root cause · Customer impact ·
  Recommended fix / mitigation · Escalation (owner team) · Confidence.
- Be concise. Use short headings and bullets. Timestamps in UTC.
"""


CROSS_CASE_PROMPT = """
MODE: no case is pre-selected. The user may paste a customer complaint, describe symptoms, name a
case ID, or ask a question that spans several cases ("which incidents involved Kafka?").

Locating the right case(s):
1. If the message names a CASE-0NN, use it directly.
2. Otherwise call find_cases with the key symptoms (and/or list_cases to read all 20). Confirm the
   top candidates with get_case + get_alerts before committing; say which case you matched and why.
   If two cases plausibly match, say so, pick the best one, and mention the alternative.
3. For fleet-wide questions use search_alerts_all_cases / search_logs_all_cases /
   search_events_all_cases, then drill into individual cases with the case-scoped tools.
4. Every case-scoped tool call MUST include case_id.
Always open your answer with the case(s) you investigated, e.g. "Matched: CASE-001 (checkout-service, opened 2026-03-14)".
"""


def _case_context(case: dict[str, Any]) -> str:
    return (
        f"MODE: one case is pre-selected; case-scoped tools default to it.\n"
        f"Current case: {case.get('case_id')}\n"
        f"Opened: {case.get('created_at')}\n"
        f"Evidence window: {case['window']['from']} -> {case['window']['to']}\n"
        f"Affected area (as tagged by support): {case.get('affected_area')}\n"
        f"Customer error message: {case.get('error_message')}\n"
        f"Customer description: {case.get('description')}\n"
    )


def investigate(
    case_id: str | None,
    user_message: str,
    history: list[dict[str, str]] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """
    case_id=None → cross-case mode: the model locates the relevant case(s) itself.
    Yields events:
      {"type":"tool_call","name":..,"args":{..}}
      {"type":"tool_result","name":..,"chars":N,"preview":".."}
      {"type":"answer","content":".."}
      {"type":"error","message":".."}
    `history` is a list of prior {"role":"user"|"assistant","content":...} turns.
    """
    if case_id:
        case = dataset.get_case(case_id)
        if not case:
            yield {"type": "error", "message": f"Unknown case {case_id}"}
            return
        system = SYSTEM_PROMPT + "\n" + _case_context(case)
        tools = dataset.TOOLS
    else:
        system = SYSTEM_PROMPT + CROSS_CASE_PROMPT
        tools = dataset.CROSS_CASE_TOOLS + dataset.TOOLS

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_message})

    for _ in range(MAX_STEPS):
        try:
            resp = client().chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools,
                # NB: no tool_choice — Claude Sonnet/Opus 5.x on DO reject that parameter.
                temperature=0.1,
                max_tokens=2500,
            )
        except Exception as e:  # network / auth / quota errors
            yield {"type": "error", "message":
                   f"Inference call failed: {e} (base_url={INFERENCE_BASE_URL}, model={MODEL}). "
                   "Check GET /api/models to confirm the key works and the model ID exists."}
            return

        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            yield {"type": "answer", "content": msg.content or "(no answer)"}
            return

        # Append assistant turn with tool calls, then one tool message per call.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_call", "name": name, "args": args}
            result = dataset.run_tool(name, case_id, args)
            yield {"type": "tool_result", "name": name, "chars": len(result), "preview": result[:300]}
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": name, "content": result})

    # Out of steps: force a final answer without tools.
    messages.append({"role": "user", "content":
                     "Stop investigating. Summarise what you found so far, what remains unconfirmed, and the most likely root cause."})
    try:
        resp = client().chat.completions.create(model=MODEL, messages=messages, temperature=0.1, max_tokens=2000)
        yield {"type": "answer", "content": resp.choices[0].message.content or ""}
    except Exception as e:
        yield {"type": "error", "message": f"Inference call failed: {e}"}
