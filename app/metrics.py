"""
Per-query metrics: latency, token usage and estimated cost.

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

    def add_llm(self, resp: Any, seconds: float, step: int) -> dict[str, Any]:
        """Record one model call. `resp.usage` is the OpenAI-style usage block."""
        u = getattr(resp, "usage", None)
        p = int(getattr(u, "prompt_tokens", 0) or 0)
        c = int(getattr(u, "completion_tokens", 0) or 0)
        rec = {"step": step, "latency_ms": round(seconds * 1000),
               "prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c,
               "cost_usd": round(estimate_cost(p, c), 6)}
        self.llm_calls.append(rec)
        return rec

    def add_tool(self, name: str, seconds: float, chars: int) -> dict[str, Any]:
        rec = {"name": name, "latency_ms": round(seconds * 1000), "chars": chars}
        self.tool_calls.append(rec)
        return rec

    def summary(self) -> dict[str, Any]:
        p = sum(c["prompt_tokens"] for c in self.llm_calls)
        c = sum(x["completion_tokens"] for x in self.llm_calls)
        return {
            "total_latency_ms": round((time.perf_counter() - self.started) * 1000),
            "llm_latency_ms": sum(x["latency_ms"] for x in self.llm_calls),
            "tool_latency_ms": sum(x["latency_ms"] for x in self.tool_calls),
            "llm_calls": len(self.llm_calls),
            "tool_calls": len(self.tool_calls),
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": p + c,
            "estimated_cost_usd": round(estimate_cost(p, c), 6),
            "price_per_1m": {"input": PRICE_INPUT_PER_1M, "output": PRICE_OUTPUT_PER_1M},
            "model_calls_detail": self.llm_calls,
            "tool_calls_detail": self.tool_calls,
        }