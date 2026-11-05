"""
Benchmark harness for the troubleshooting agent.

Runs a fixed question set over chosen cases (optionally several models, several
repeats), records latency / model calls / tool calls / dataset API requests /
tokens / cost / answer quality per run, and prints p50/p95 summaries.

  export DO_MODEL_ACCESS_KEY=...
  python bench.py                                    # 3 cases x 3 questions x 1 run
  python bench.py --repeats 3                        # 3 runs each -> real p50/p95
  python bench.py --cases CASE-001,CASE-009,CASE-017 --questions root_cause,changes
  python bench.py --models anthropic-claude-sonnet-5.5,llama3.3-70b-instruct   # compare models
  python bench.py --cases all --questions find_case  # cross-case (auto-detect) mode

Outputs go to bench_results/<timestamp>/ : runs.csv, runs.json, summary.md
Runs are sequential on purpose, so latencies are not distorted by self-inflicted load.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from app import agent

QUESTIONS = {
    "root_cause": "What is the root cause? Give me the full investigation.",
    "changes": "What changed in the window (deploys, config, migrations) and is any change to blame?",
    "escalate": "Which team should we escalate to and what should the mitigation be?",
    "narrow": "Was the customer actually charged twice?",          # narrow question: should be cheap
    "find_case": "my card got charged twice and i never got an order confirmation email. what happened?",
}
DEFAULT_QUESTIONS = ["root_cause", "changes", "escalate"]
DEFAULT_CASES = ["CASE-001", "CASE-009", "CASE-017"]

# Optional quality check: keywords the final answer should contain, keyed by (case, question).
# Seeded from README section 5 (CASE-001). Extend, or pass --golden file.json with the same shape:
#   {"CASE-009": {"root_cause": ["postgres-primary", "RB-0xx"]}}
GOLDEN: dict[str, dict[str, list[str]]] = {
    "CASE-001": {"root_cause": ["order-service", "disk", "RB-002", "OWN-001", "ALT-0101", "EVT-0101", "PM-2026-01"]},
}


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def run_one(case_id: str | None, qid: str, model: str, golden: dict) -> dict:
    agent.MODEL = model
    t0 = time.perf_counter()
    first_tool_ms = None
    seen: set[tuple[str, str]] = set()
    dup_calls = 0
    answer, err, m = "", None, {}
    for ev in agent.investigate(case_id, QUESTIONS[qid], []):
        t = ev["type"]
        if t == "tool_call":
            if first_tool_ms is None:
                first_tool_ms = round((time.perf_counter() - t0) * 1000)
            sig = (ev["name"], json.dumps(ev["args"], sort_keys=True))
            dup_calls += sig in seen          # identical tool+args asked again = wasted round-trip
            seen.add(sig)
        elif t == "answer":
            answer = ev["content"]
        elif t == "error":
            err = ev["message"]
        elif t == "metrics":
            m = ev
    detail = m.get("model_calls_detail", [])
    want = golden.get(case_id or "", {}).get(qid)
    score = round(sum(k.lower() in answer.lower() for k in want) / len(want), 2) if want else None
    total = m.get("total_latency_ms", round((time.perf_counter() - t0) * 1000))
    return {
        "model": model, "case": case_id or "ALL", "question": qid, "ok": bool(answer) and not err,
        "error": (err or "")[:200],
        "total_ms": total, "llm_ms": m.get("llm_latency_ms", 0), "tool_ms": m.get("tool_latency_ms", 0),
        "model_share_pct": round(100 * m.get("llm_latency_ms", 0) / total, 1) if total else 0,
        "first_tool_ms": first_tool_ms,
        "llm_calls": m.get("llm_calls", 0), "tool_calls": m.get("tool_calls", 0),
        "dup_tool_calls": dup_calls, "dataset_requests": m.get("dataset_requests", 0),
        "dataset_errors": m.get("dataset_errors", 0),
        "prompt_tokens": m.get("prompt_tokens", 0), "completion_tokens": m.get("completion_tokens", 0),
        "total_tokens": m.get("total_tokens", 0),
        "first_prompt_tokens": detail[0]["prompt_tokens"] if detail else 0,
        "max_prompt_tokens": max((d["prompt_tokens"] for d in detail), default=0),
        "avg_ms_per_model_call": round(m["llm_latency_ms"] / m["llm_calls"]) if m.get("llm_calls") else 0,
        "cost_usd": m.get("estimated_cost_usd", 0.0),
        "hit_step_cap": m.get("llm_calls", 0) > agent.MAX_STEPS,
        "answer_chars": len(answer), "quality_score": score,
    }


def summarise(rows: list[dict]) -> str:
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["model"], r["question"]), []).append(r)

    def avg(rs, k):
        v = [r[k] for r in rs if r[k] is not None]
        return statistics.mean(v) if v else float("nan")

    out = ["| model | question | n | ok% | p50 s | p95 s | model calls | tool calls | dup | API req | tokens | $/query | model time % | quality |",
           "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for (model, q), rs in sorted(groups.items()):
        lat = [r["total_ms"] / 1000 for r in rs]
        qs = avg(rs, "quality_score")
        out.append(f"| {model} | {q} | {len(rs)} | {100*sum(r['ok'] for r in rs)/len(rs):.0f} | "
                   f"{pct(lat,50):.1f} | {pct(lat,95):.1f} | {avg(rs,'llm_calls'):.1f} | {avg(rs,'tool_calls'):.1f} | "
                   f"{avg(rs,'dup_tool_calls'):.1f} | {avg(rs,'dataset_requests'):.1f} | {avg(rs,'total_tokens'):,.0f} | "
                   f"{avg(rs,'cost_usd'):.4f} | {avg(rs,'model_share_pct'):.0f} | "
                   f"{'' if qs != qs else f'{qs:.2f}'} |")
    lat = [r["total_ms"] / 1000 for r in rows]
    out += ["", f"**Overall** ({len(rows)} runs): p50 {pct(lat,50):.1f}s · p95 {pct(lat,95):.1f}s · "
            f"mean {avg(rows,'llm_calls'):.1f} model calls · mean ${avg(rows,'cost_usd'):.4f}/query · "
            f"total ${sum(r['cost_usd'] for r in rows):.3f} · "
            f"{sum(r['hit_step_cap'] for r in rows)} runs hit the step cap · "
            f"{sum(not r['ok'] for r in rows)} failed"]
    out += ["", "How to read: *model time %* high → latency is the LLM (fewer/smaller calls, faster model); "
            "low → tools/dataset API dominate. *dup* = identical tool call repeated. "
            "Cost uses PRICE_INPUT_PER_1M / PRICE_OUTPUT_PER_1M (estimate)."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=",".join(DEFAULT_CASES), help="comma list of CASE-0NN, or 'all' for cross-case mode")
    ap.add_argument("--questions", default=",".join(DEFAULT_QUESTIONS), help=f"comma list from: {', '.join(QUESTIONS)}")
    ap.add_argument("--models", default=agent.MODEL, help="comma list of model IDs to compare")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--golden", help="JSON file of expected keywords per case/question")
    ap.add_argument("--out", default="bench_results")
    a = ap.parse_args()

    cases = [None if c.strip().lower() in ("all", "auto") else c.strip().upper() for c in a.cases.split(",")]
    qids = [q.strip() for q in a.questions.split(",")]
    bad = [q for q in qids if q not in QUESTIONS]
    if bad:
        sys.exit(f"unknown question(s): {bad}. choose from {list(QUESTIONS)}")
    golden = {**GOLDEN}
    if a.golden:
        golden.update(json.loads(Path(a.golden).read_text()))

    plan = [(m.strip(), c, q, i) for m in a.models.split(",") for c in cases for q in qids for i in range(a.repeats)]
    out_dir = Path(a.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for n, (model, case_id, qid, i) in enumerate(plan, 1):
        print(f"[{n}/{len(plan)}] {model} · {case_id or 'ALL'} · {qid} · run {i+1}", end=" … ", flush=True)
        r = run_one(case_id, qid, model, golden)
        r["run"] = i + 1
        rows.append(r)
        print(f"{r['total_ms']/1000:.1f}s · {r['llm_calls']} model calls · {r['tool_calls']} tools · "
              f"{r['total_tokens']:,} tok · ${r['cost_usd']:.4f}" + ("" if r["ok"] else f"  FAILED {r['error']}"), flush=True)

    (out_dir / "runs.json").write_text(json.dumps(rows, indent=2))
    with open(out_dir / "runs.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    md = summarise(rows)
    (out_dir / "summary.md").write_text(md)
    print("\n" + md + f"\n\nSaved to {out_dir}/")


if __name__ == "__main__":
    main()