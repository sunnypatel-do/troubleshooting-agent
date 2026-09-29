"""Tool-calling investigation engine (no web framework code in here).

Uses DigitalOcean Serverless Inference through the OpenAI-compatible Chat Completions API.
"""
import os, re, json, time
from tools import build_tools, Api, rows

BASE_URL   = os.environ.get("LLM_BASE_URL", "https://inference.do-ai.run/v1")
MODEL      = os.environ.get("LLM_MODEL", "anthropic-claude-4.6-sonnet")
MAX_STEPS  = int(os.environ.get("MAX_STEPS", 20))
MAX_TOOL_CHARS = int(os.environ.get("MAX_TOOL_CHARS", 6000))
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "system.md")

_client = None
def get_client():
    """Created lazily so the app can boot (and report a clear error) even if the key is missing."""
    global _client
    if _client is None:
        from openai import OpenAI
        key = os.environ.get("MODEL_ACCESS_KEY") or os.environ.get("LLM_API_KEY")
        if not key:
            raise RuntimeError("MODEL_ACCESS_KEY is not set (create a model access key in the DigitalOcean control panel)")
        _client = OpenAI(base_url=BASE_URL, api_key=key, timeout=120, max_retries=3)
    return _client

def brief(result):
    """One-line summary of a tool result, shown in the UI step list."""
    if isinstance(result, dict) and "error" in result:
        return f"error {result['error']}: {str(result.get('detail', ''))[:80]}"
    r = rows(result) if not isinstance(result, dict) or "unique_messages" not in result else result["unique_messages"]
    if r: return f"{len(r)} records"
    if isinstance(result, dict):
        if "results" in result: return f"{len(result['results'])} results"
        return ", ".join(list(result)[:6])[:100]
    return str(result)[:100]

ID_LIKE = re.compile(r"^(?:[A-Za-z]{2,5}-[\w-]*\d[\w-]*|[0-9a-f]{12,})$")

class Investigation:
    def __init__(self, case_id, emit=None, client=None, model=None):
        self.case_id, self.emit = case_id, emit or (lambda e: None)
        self.client, self.model = client, model or MODEL
        self.api = Api(case_id)
        self.system = open(PROMPT_PATH).read().replace("{max_steps}", str(MAX_STEPS))
        self.msgs = [{"role": "system", "content": self.system},
                     {"role": "user", "content": f"Investigate {case_id}. Start with get_case."}]
        self.m = dict(in_tok=0, out_tok=0, llm_calls=0, tool_calls=0, llm_s=0.0)
        self.seen_text, self.diagnosis, self.t0 = "", None, None
        self.n = 0

    # ---------------------------------------------------------------- core loop
    def _call_llm(self, tools):
        t = time.time()
        r = (self.client or get_client()).chat.completions.create(model=self.model, messages=self.msgs, tools=tools)
        self.m["llm_calls"] += 1; self.m["llm_s"] += time.time() - t
        if getattr(r, "usage", None):
            self.m["in_tok"] += r.usage.prompt_tokens or 0
            self.m["out_tok"] += r.usage.completion_tokens or 0
        return r.choices[0].message

    def _loop(self, tools, allow_submit):
        for step in range(MAX_STEPS + 1):
            if step == MAX_STEPS:
                self.msgs.append({"role": "user", "content":
                    "Tool budget used up. " + ("Call submit_diagnosis now with the evidence you have." if allow_submit
                     else "Answer now with the evidence you have and say what is missing.")})
            m = self._call_llm(tools)
            calls = m.tool_calls or []
            msg = {"role": "assistant", "content": m.content or ""}
            extra = getattr(m, "model_extra", None) or {}
            if extra.get("reasoning_content"): msg["reasoning_content"] = extra["reasoning_content"]  # some models need it echoed back
            if calls:
                msg["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}} for c in calls]
            self.msgs.append(msg)
            if not calls:
                return (m.content or "").strip()
            if m.content and m.content.strip():
                self.emit({"type": "note", "text": m.content.strip()[:300]})
            for c in calls:
                name = c.function.name
                try: args = json.loads(c.function.arguments or "{}")
                except ValueError: args = {}; bad = True
                else: bad = False
                if name == "submit_diagnosis" and allow_submit:
                    self.diagnosis = args
                    self.msgs.append({"role": "tool", "tool_call_id": c.id, "content": "received"})
                    return None
                t = time.time()
                if bad: result = {"error": "bad_arguments", "detail": "arguments were not valid JSON"}
                else:
                    try: result = self.api.run(name, args)
                    except Exception as e: result = {"error": "tool_failed", "detail": f"{type(e).__name__}: {e}"[:200]}
                text = json.dumps(result)
                sent = text[:MAX_TOOL_CHARS] + ("...[truncated]" if len(text) > MAX_TOOL_CHARS else "")
                self.seen_text += sent
                self.m["tool_calls"] += 1; self.n += 1
                self.emit({"type": "step", "n": self.n, "tool": name, "args": args, "summary": brief(result),
                           "ms": int((time.time() - t) * 1000), "chars": len(text)})
                self.msgs.append({"role": "tool", "tool_call_id": c.id, "content": sent})
        return ""

    # ---------------------------------------------------------------- public
    def run(self):
        """Initial investigation. Returns the diagnosis dict."""
        self.t0 = self.t0 or time.time()
        text = self._loop(build_tools(case_bound=True, with_submit=True), allow_submit=True)
        if self.diagnosis is None:      # model answered in prose instead of calling submit_diagnosis
            self.diagnosis = {"verdict": "unstructured", "root_cause_service": "unknown", "root_cause_category": "unknown",
                              "confidence": "low", "evidence": [], "explanation": text or "No answer produced.",
                              "timeline": [], "recommended_action": "", "decoys_ruled_out": []}
        self.diagnosis["unverified_ids"] = self.unverified()
        return self.diagnosis

    def ask(self, question):
        """Follow-up question about the same case. Returns answer text."""
        self.msgs.append({"role": "user", "content": question})
        return self._loop(build_tools(case_bound=True, with_submit=False), allow_submit=False) or "(no answer)"

    def unverified(self):
        """Cited ID-like evidence that never appeared in any tool result (possible hallucination)."""
        return [e for e in (self.diagnosis or {}).get("evidence", [])
                if isinstance(e, str) and ID_LIKE.match(e.strip()) and e.strip() not in self.seen_text]

    def metrics(self):
        return {**self.m, "llm_s": round(self.m["llm_s"], 1), "api_requests": self.api.calls,
                "elapsed_s": round(time.time() - (self.t0 or time.time()), 1), "model": self.model}
