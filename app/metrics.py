"""
Per-query metrics: latency, token usage, estimated cost, model calls and
dataset API requests.

Prices are USD per 1M tokens. They are ESTIMATES — check the DigitalOcean
Serverless Inference pricing page and override via env vars:
    PRICE_INPUT_PER_1M=3.00  PRICE_OUTPUT_PER_1M=15.00
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

PRICE_INPUT_PER_1M = float(os.getenv("PRICE_INPUT_PER_1M", "3.00"))
PRICE_OUTPUT_PER_1M = float(os.getenv("PRICE_OUTPUT_PER_1M", "15.00"))


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * PRICE_INPUT_PER_1M + completion_tokens * PRICE_OUTPUT_PER_1M) / 1_000_000


@dataclass
class QueryMetrics:
    started: float = field(default_factory=time.perf_counter)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    api_requests: list[dict[str, Any]] = field(default_factory=list)

    def _offset_ms(self) -> int:
        return round((time.perf_counter() - self.started) * 1000)

    def add_llm(self, resp: Any, seconds: float, step: int, model: str = "") -> dict[str, Any]:
        """Record one model call. `resp.usage` is the OpenAI-style usage block."""
        u = getattr(resp, "usage", None)
        p = int(getattr(u, "prompt_tokens", 0) or 0)
        c = int(getattr(u, "completion_tokens", 0) or 0)
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        msg = getattr(choice, "message", None)
        requested = [tc.function.name for tc in (getattr(msg, "tool_calls", None) or [])]
        rec = {"step": step, "model": getattr(resp, "model", None) or model,
               "request_id": getattr(resp, "id", None),
               "finish_reason": getattr(choice, "finish_reason", None),
               "requested_tools": requested,
               "latency_ms": round(seconds * 1000),
               "prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c,
               "cost_usd": round(estimate_cost(p, c), 6)}
        self.llm_calls.append(rec)
        return rec

    def add_tool(self, name: str, seconds: float, chars: int,
                 api_requests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        reqs = api_requests or []
        for r in reqs:
            self.api_requests.append({**r, "tool": name})
        rec = {"name": name, "latency_ms": round(seconds * 1000), "chars": chars,
               "api_requests": len(reqs)}
        self.tool_calls.append(rec)
        return rec

    def summary(self) -> dict[str, Any]:
        p = sum(c["prompt_tokens"] for c in self.llm_calls)
        c = sum(x["completion_tokens"] for x in self.llm_calls)
        return {
            "total_latency_ms": self._offset_ms(),
            "llm_latency_ms": sum(x["latency_ms"] for x in self.llm_calls),
            "tool_latency_ms": sum(x["latency_ms"] for x in self.tool_calls),
            "llm_calls": len(self.llm_calls),
            "tool_calls": len(self.tool_calls),
            "dataset_requests": len(self.api_requests),
            "dataset_errors": sum(1 for r in self.api_requests if r["status"] >= 400 or r["status"] < 0),
            "dataset_bytes": sum(r["bytes"] for r in self.api_requests),
            # Sum of request durations; can exceed wall time when fan-out runs in parallel.
            "dataset_latency_ms": sum(r["latency_ms"] for r in self.api_requests),
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": p + c,
            "estimated_cost_usd": round(estimate_cost(p, c), 6),
            "price_per_1m": {"input": PRICE_INPUT_PER_1M, "output": PRICE_OUTPUT_PER_1M},
            "model_calls_detail": self.llm_calls,
            "tool_calls_detail": self.tool_calls,
            "dataset_requests_detail": self.api_requests,
        }