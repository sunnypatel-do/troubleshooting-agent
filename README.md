# ExampleKart Troubleshooting Bot — demo + launch guide

An agentic troubleshooting assistant that investigates the 20 synthetic support cases in the
[ExampleKart dataset](https://examplekart-dataset-ovp33.ondigitalocean.app/) by calling its
alerts / logs / metrics / traces / events / knowledge-base endpoints as tools, reasoning over the
evidence with a model served by **DigitalOcean Serverless Inference**, and answering the user's
question with citations. Runs on **DigitalOcean App Platform**.

```
┌──────────────┐   POST /api/chat (SSE)   ┌─────────────────────────┐   OpenAI-compatible    ┌────────────────────────────┐
│  Chat UI     │ ───────────────────────▶ │  FastAPI app (App       │ ─────────────────────▶ │ DO Serverless Inference    │
│  static/     │ ◀─── tool trail + answer │  Platform, 1 container) │ ◀── tool_calls/answer  │ inference.do-ai.run        │
└──────────────┘                          │   agent loop            │                        │ anthropic-claude-sonnet-5.5│
                                          │   11 read-only tools    │                        └────────────────────────────┘
                                          └───────────┬─────────────┘
                                                      │ GET /v1/cases/{id}/{alerts,events,logs,metrics,traces}, /v1/kb/*
                                                      ▼
                                          ┌─────────────────────────┐
                                          │ ExampleKart Dataset API │  (no auth, read-only, 3h window per case)
                                          └─────────────────────────┘
```

---

## 1. What the dataset gives you (learned from the API)

The web page at the URL is a thin explorer; the real surface is a REST API documented at
`/openapi.json`. Everything is GET, unauthenticated, paginated with `cursor`, and returns RFC 7807
problem documents on bad input (e.g. a misspelled service name → 400 with the valid list hint).

| UI section | Endpoint | Useful filters |
|---|---|---|
| Overview | `GET /v1/cases`, `GET /v1/cases/{id}` | — |
| Alerts | `GET /v1/cases/{id}/alerts` | `service, severity, state, from, to` |
| Events | `GET /v1/cases/{id}/events` | `type` (deploy, config_change, schema_migration, scaling, infra_maintenance), `service` |
| Logs | `GET /v1/cases/{id}/logs` | `service, level, q, trace_id, host, from, to, order, limit` |
| Metrics | `GET /v1/cases/{id}/metrics/catalog`, `GET .../metrics/query?metric=` | `service, from, to, step` |
| Traces | `GET /v1/cases/{id}/traces`, `GET .../traces/{trace_id}` | `service, status, name, min_duration_ms` |
| Knowledge base | `GET /v1/kb/search`, `GET /v1/kb/{doc_id}`, `GET /v1/kb/export` | `q, type, service` |
| Console | (the explorer's request log — equivalent to the tool trail this bot shows) | |
| Entities | `GET /v1/entities` | 10 services, 6 infra, 4 uninstrumented third parties |

48 KB documents: architecture (ARCH-*), ownership/escalation (OWN-001), runbooks (RB-001…),
postmortems (PM-*). Some are deliberately marked `stale: true` (e.g. RB-011) — the bot is told to
flag that.

Case list: CASE-001 … CASE-020, each with `description`, `error_message`, `affected_area`, and a
3-hour `window`. Note that `affected_area` is *where the customer felt it*, not the root cause
(CASE-001 is tagged `checkout-service` but the failure is a full disk on `order-service`).

---

## 2. How the bot works

* **Agentic tool-calling, not prompt stuffing.** The model receives the case context plus 11
  function tools (`app/dataset.py`). It decides what to query, we execute the GET, return the JSON,
  and loop until it answers (max 12 steps, `app/agent.py`). This mirrors how an SRE investigates
  and keeps context small — a 3h log window can be hundreds of rows, so the model narrows by
  `service + level + q + time` instead of dumping.
* **Grounding rules in the system prompt.** Every claim must cite alert IDs, event IDs, log
  lines, metric values, trace IDs or KB doc IDs; hypotheses are separated from facts; missing
  evidence is stated. Root-cause answers follow a fixed structure (Summary · Evidence · Root cause ·
  Impact · Fix · Escalation · Confidence).
* **Observable.** The UI streams the tool trail (which tool, which args, how many bytes came back,
  a peek of the payload) above the final answer, so reviewers can audit the investigation.
* **Model-agnostic.** Anything on `GET https://inference.do-ai.run/v1/models` that supports tool
  calling works; set `MODEL`. Default `anthropic-claude-sonnet-5.5`.

---

## 3. Step-by-step: run it locally (10 minutes)

1. **Create a model access key** in the DigitalOcean control panel:
   *Gradient AI Platform → Serverless Inference → Model Access Keys → Create key*. Copy it once.
   (Docs: https://docs.digitalocean.com/products/inference/how-to/manage-model-access-keys/)
2. Serverless inference is pay-as-you-go against a prepaid balance; make sure the team/account has
   credit (*Serverless Inference → Manage prepayment*).
3. Clone / copy this folder, then:
   ```bash
   cd examplekart-troubleshooting-bot
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env            # paste your key into DO_MODEL_ACCESS_KEY
   set -a; source .env; set +a
   ```
4. Sanity-check inference from your machine:
   ```bash
   curl -s https://inference.do-ai.run/v1/models -H "Authorization: Bearer $DO_MODEL_ACCESS_KEY" | head -c 600
   ```
5. Try the CLI first (fastest feedback):
   ```bash
   python cli.py                                   # lists the 20 cases
   python cli.py CASE-001 "What is the root cause?"
   python cli.py CASE-013                          # interactive multi-turn
   ```
6. Run the web app:
   ```bash
   uvicorn app.main:app --reload --port 8080
   # open http://localhost:8080
   ```
   `GET /healthz` shows the model in use and whether the key is set; `GET /api/tools` shows the
   tool schemas the model sees.

---

## 4. Step-by-step: deploy to DigitalOcean App Platform

**Option A — Control panel (no CLI)**

1. Push this folder to a GitHub repo (keep `.env` out; `.gitignore` already excludes it).
2. Control panel → *App Platform → Create App* → pick the repo/branch.
3. App Platform may pick the **Python buildpack** rather than the Dockerfile. That is fine — the
   included `Procfile` gives it the start command. If you see `can't open file '/workspace/app.py'`,
   the run command was auto-guessed: open the component → *Settings → Commands → Run command* and set
   `uvicorn app.main:app --host 0.0.0.0 --port 8080`. Set **HTTP port 8080**, instance *Basic 1 vCPU / 1 GB*.
4. *Environment variables* (Run time):
   * `DO_MODEL_ACCESS_KEY` = your key → tick **Encrypt**
   * `MODEL` = `anthropic-claude-sonnet-5.5`
   * `INFERENCE_BASE_URL` = `https://inference.do-ai.run/v1`
   * `DATASET_BASE_URL` = `https://examplekart-dataset-ovp33.ondigitalocean.app`
5. Health check path `/healthz`. Create → wait for the build → open the `*.ondigitalocean.app` URL.

**Option B — `doctl` with the included spec**

```bash
# edit .do/app.yaml: set github.repo to your fork; put the key in DO_MODEL_ACCESS_KEY.value
doctl auth init
doctl apps create --spec .do/app.yaml
doctl apps list                     # grab the APP_ID and live URL
doctl apps logs <APP_ID> --type run --follow
```
After the first apply DO stores the secret encrypted; you can blank the value in the file.

**Option C — Container only (Droplet / local Docker)**

```bash
docker build -t ek-bot .
docker run -p 8080:8080 --env-file .env ek-bot
```

---

## 5. Demo script (what to show)

Pick **CASE-001** ("card charged twice, no confirmation email"). Ask, in order:

1. *"What is the root cause? Give me the full investigation."*
   Expected trail: `get_case → get_alerts → get_events → query_metric(disk_used_pct, order-service)
   → search_logs(order-service, ERROR, q="space") → search_traces(status=error) → get_trace →
   kb_search(disk) → kb_doc(RB-002) → kb_doc(OWN-001)`.
   Expected answer (verified by hand against the data):
   * `disk_used_pct` on `drop-ord-01` climbs 94% → 100% between 00:55 and 02:10 UTC (ALT-0102 fired
     at 01:31 at 97.1%).
   * EVT-0101 at 01:20: `logrotate.timer` failed — *"No space left on device"*.
   * From 02:18 order-service returns 500 (`java.io.IOException: No space left on device` in
     `Journal.append`), ALT-0101 error_rate 55%; checkout-service propagates it (ALT-0105).
   * Trace `9953b01e27682978`: api-gateway → checkout-service.place_order → order-service.persist_order
     fails at the last span.
   * Kafka `order-events` consumer lag 2841 (ALT-0103) → confirmation emails not sent.
   * Decoys the bot should dismiss: api-gateway deploy v6.1.0 at 02:04 (status passthrough, not a cause);
     migration 0094 at 01:57; resolved SearchLatencyWarning (RB-011 is stale).
   * Runbook RB-002; near-identical postmortem PM-2026-01; escalate to the order-service owner in OWN-001.
2. *"Was the customer actually charged twice?"* — tests whether the bot separates what the data
   shows (payment step succeeded, order persist failed, so a retry could double-charge) from what it
   cannot see (paygate is uninstrumented).
3. *"Draft a reply to the customer."* — shows a non-SRE use of the same evidence.
4. Switch to **CASE-009** (only one category spins → `postgres-primary`) or **CASE-017**
   (`ERR_CONNECTION_TIMED_OUT` for one B2B customer → `lb-public`) to show a different reasoning path.

Other good questions: *"What changed in the window and is any change to blame?"*, *"Which alerts
are noise?"*, *"Is there a runbook and is it current?"*, *"Who do we page?"*

---

## 6. Feedback for Rahul on the dataset (observations from exploring)

* The API is well designed for agents: `hint` fields on truncated pages, RFC 7807 errors that list
  valid values, and a `stale` flag on KB docs. Keep these — they let the model self-correct.
* The metrics catalog is per case (CASE-001 exposes 8 metrics). Several runbooks reference metrics
  (`db_cpu_pct`, `cache_hit_ratio`, `external_call_error_rate`, `db_connections_active`) — worth
  confirming they appear in the catalog of the cases where they matter, or the bot will correctly
  report "not observable".
* Third parties (paygate, taxcalc, mailsend, addrverify) are uninstrumented by design; payment-adapter
  logs are the only window into paygate. Good realism; consider documenting that in ARCH-001 so the
  bot can explain the blind spot.
* Would help evaluation: a hidden `/v1/cases/{id}/solution` (or an offline answer key) with root
  cause, key evidence IDs, and owner team, so we can score the agent automatically across all 20.

---

## 7. Roadmap / next steps (maps to the meeting notes)

| Meeting item | How this demo helps |
|---|---|
| Explore data (Sunny, Ashok) | Section 1 + `GET /api/tools`; run `python cli.py` over all 20 cases |
| Provide feedback to Rahul | Section 6 |
| Example investigation (Rahul) | Compare his CASE-001 write-up with the bot's answer in Section 5 |
| RAG over knowledge bases | Phase 2 below |

**Phase 2 — DO Knowledge Base for the KB corpus.** `GET /v1/kb/export` returns all 48 docs. Load them
into a *Gradient AI Knowledge Base* (Spaces bucket → KB → index) and either attach it to a *Gradient
Agent* or add a `kb_semantic_search` tool that queries the KB retrieval endpoint. Keyword search
via `/v1/kb/search` stays as a fallback.

**Phase 3 — Evaluation harness.** A script that runs a fixed question set over all 20 cases, stores
the tool trail + answer, and grades against the answer key (root-cause service, key evidence IDs,
owner). Use it to compare models (`MODEL=` Claude vs Llama vs GPT) and prompt changes.

**Phase 4 — Productionising.** Move the agent loop to a *Gradient Agent* (managed tool routing +
KB), add per-user sessions in Managed Valkey, stream tokens instead of whole answers, and add the
code-review use case as a second tool set.

---

## Project layout

```
app/dataset.py   dataset API client + 11 tool schemas/impls (the only place that knows the API)
app/agent.py     system prompt + agentic loop against DO Serverless Inference (OpenAI SDK)
app/main.py      FastAPI: /, /healthz, /api/cases, /api/tools, /api/chat (SSE)
static/index.html chat UI with case picker, suggested questions and live tool trail
cli.py           terminal client
Dockerfile, .do/app.yaml, requirements.txt, .env.example
```

Environment variables: `DO_MODEL_ACCESS_KEY` (required), `MODEL`, `INFERENCE_BASE_URL`,
`DATASET_BASE_URL`, `MAX_STEPS` (default 12), `MAX_TOOL_RESULT_CHARS` (default 14000)
