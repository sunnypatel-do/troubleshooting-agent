# ExampleKart Troubleshooting Bot — Step-by-Step Launch Guide

This guide takes you from an empty DigitalOcean account to a live troubleshooting bot on App
Platform, then through a demo run. It reflects the working setup as of 30 Sep 2026, including the
two issues hit during the first deployment and how to avoid them.

**What you are launching:** a chat app where a Claude model (served by DigitalOcean Serverless
Inference) investigates one of the 20 ExampleKart support cases by calling the dataset's alerts,
events, logs, metrics, traces and knowledge-base endpoints as tools, then answers with citations.

```
Browser ──▶ App Platform (FastAPI, port 8080) ──▶ inference.do-ai.run/v1  (Claude Sonnet 5.5, tool calling)
                        │
                        └──▶ examplekart-dataset-ovp33.ondigitalocean.app  (GET-only dataset API)
```

---

## Part 1 — Prerequisites (5 min)

| You need | Where |
|---|---|
| DigitalOcean account with App Platform access | cloud.digitalocean.com |
| Serverless Inference prepaid balance | Control panel → **Gradient AI Platform → Serverless Inference → Manage prepayment** |
| A **model access key** | Control panel → **Gradient AI Platform → Serverless Inference → Model Access Keys → Create key**. Copy it once; it is not shown again. |
| GitHub account | To host the repo App Platform builds from |
| Python 3.11+ (optional, for local run) | python.org |

Sanity-check the key from your laptop before going further:

```bash
export DO_MODEL_ACCESS_KEY="sk-do-..."
curl -s https://inference.do-ai.run/v1/models \
  -H "Authorization: Bearer $DO_MODEL_ACCESS_KEY" | grep -o '"anthropic-claude-sonnet-5.5"'
```
If that prints the model ID, the key and the model are good.

---

## Part 2 — Get the code into GitHub (5 min)

1. Create a new **empty** GitHub repository, e.g. `examplekart-troubleshooting-bot`.
2. Copy the project folder into it. Required files:
   ```
   app/__init__.py  app/dataset.py  app/agent.py  app/main.py
   static/index.html
   cli.py  requirements.txt  Procfile  Dockerfile  .do/app.yaml  .env.example  .gitignore
   ```
3. Commit and push:
   ```bash
   git init && git add . && git commit -m "ExampleKart troubleshooting bot"
   git branch -M main
   git remote add origin git@github.com:<you>/examplekart-troubleshooting-bot.git
   git push -u origin main
   ```
   `.gitignore` already excludes `.env`, so your key never lands in the repo.

---

## Part 3 — (Optional) Run locally first (5 min)

```bash
cd examplekart-troubleshooting-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # paste your key into DO_MODEL_ACCESS_KEY
set -a; source .env; set +a

python cli.py                      # lists the 20 cases
python cli.py CASE-001 "What is the root cause?"

uvicorn app.main:app --reload --port 8080    # then open http://localhost:8080
```

---

## Part 4 — Deploy on App Platform (10 min)

### 4.1 Create the app

1. Control panel → **App Platform → Create App**.
2. **Source:** GitHub → authorise if asked → choose the repo and branch `main`. Leave *Source
   Directory* as `/`. Keep **Autodeploy** on. Click **Next**.
3. **Resources:** App Platform shows one detected component (a *Web Service*). Click the
   **pencil / Edit** icon on it and set:

   | Field | Value |
   |---|---|
   | Resource type | Web Service |
   | Build command | *(leave empty)* |
   | **Run command** | `uvicorn app.main:app --host 0.0.0.0 --port 8080` |
   | **HTTP port** | `8080` |
   | HTTP request route | `/` |
   | Instance | Basic · 1 vCPU / 1 GB (plenty) |

   > **Why this matters.** App Platform uses its Python buildpack, and without a run command it
   > guesses `python app.py`, which fails with `can't open file '/workspace/app.py'` and the health
   > check never passes. The repo also ships a `Procfile` with the same command, so if the Run
   > command field is greyed out or pre-filled from the Procfile, that is fine.

   Save and click **Next**.

4. **Environment variables:** click **Edit** on the component (or use *Global*), then add:

   | Key | Value | Encrypt |
   |---|---|---|
   | `DO_MODEL_ACCESS_KEY` | your key | **Yes** |
   | `MODEL` | `anthropic-claude-sonnet-5.5` | no |
   | `INFERENCE_BASE_URL` | `https://inference.do-ai.run/v1` | no |
   | `DATASET_BASE_URL` | `https://examplekart-dataset-ovp33.ondigitalocean.app` | no |

   > **Why `/v1` matters.** DigitalOcean's docs quote the endpoint as `https://inference.do-ai.run`,
   > but the OpenAI SDK appends `/chat/completions` to whatever you give it. Without `/v1` every
   > call fails with `404 {'detail': 'Not Found'}`. The current code normalises this automatically,
   > but set it correctly anyway.

   Click **Save**, then **Next**.

5. **Info:** name the app (e.g. `examplekart-bot`), pick a region close to you. **Next**.
6. **Review → Create Resources.** The first build takes 2–4 minutes.

### 4.2 Verify the deployment

Open the app URL (`https://<name>-xxxxx.ondigitalocean.app`) and check, in this order:

| URL | Expect | If not |
|---|---|---|
| `/healthz` | `{"ok":true,"model":"anthropic-claude-sonnet-5.5","inference_key_set":true,...}` | `inference_key_set:false` → the env var name is wrong or not saved |
| `/api/models` | `"configured_model_available": true` plus a list of model IDs | 401 → bad key · 404 → base URL missing `/v1` · `false` → typo in `MODEL` |
| `/api/cases` | 20 cases | dataset host unreachable — check `DATASET_BASE_URL` |
| `/` | chat UI with CASE-001…020 in the left rail | — |

### 4.3 Alternative: deploy from the spec with `doctl`

```bash
# edit .do/app.yaml → set github.repo to your fork, put the key in DO_MODEL_ACCESS_KEY.value
doctl auth init
doctl apps create --spec .do/app.yaml
doctl apps list                                 # note APP_ID and the live URL
doctl apps logs <APP_ID> --type run --follow    # tail runtime logs
```
After the first apply, DO stores the secret encrypted; you can blank the value in the file.

---

## Part 5 — Run the demo (5 min)

### 5.1 Cross-case mode (default): the bot finds the case itself

The left rail opens on **🔎 All cases — auto-detect**. Type a question without naming a case:

| You type | What the bot does |
|---|---|
| *"my card got charged twice and i never got a confirmation email. what happened?"* | `find_cases` → ranks CASE-001 top → confirms with `get_case` + `get_alerts` → full investigation. Answer opens with "Matched: CASE-001 …" |
| *"Which incidents involved Kafka consumer lag, and were they related?"* | `search_alerts_all_cases(name="Kafka")` → lists every case with that alert → drills into each with case-scoped tools |
| *"List every case where a deploy happened in the window and say whether it was to blame."* | `search_events_all_cases(type="deploy")` → per-case follow-ups (`get_alerts`, `query_metric`) → table of case / deploy / verdict |
| *"A UK customer cannot get past the address form."* | `find_cases` → CASE-010 → logs from checkout-service / addrverify → answer |
| *"CASE-017 — why couldn't the B2B customer connect?"* | Case ID recognised, no search needed |

Five extra tools are available only in this mode: `list_cases`, `find_cases`,
`search_alerts_all_cases`, `search_logs_all_cases`, `search_events_all_cases`. The fan-out tools
query all 20 cases in parallel and return per-case counts plus a few samples, so the model can pick
which cases to open. Every case-scoped tool then takes an explicit `case_id`.

CLI equivalent: `python cli.py all "my card got charged twice…"`.

### 5.2 Single-case mode: pick a case first

1. Open the app, click **CASE-001** ("my card got charged twice and i never got an order
   confirmation email").
2. Click the suggested chip **"What is the root cause? Give me the full investigation."** and press
   *Investigate*. Watch the tool trail appear above the answer:
   ```
   ▸ get_case        ▸ get_alerts       ▸ get_events
   ▸ query_metric(disk_used_pct, order-service)
   ▸ search_logs(order-service, ERROR, q="space")
   ▸ search_traces(status=error)  ▸ get_trace(9953b01e27682978)
   ▸ kb_search(disk, runbook)  ▸ kb_doc(RB-002)  ▸ kb_doc(OWN-001)
   ```
   Expected conclusion: disk on `drop-ord-01` climbed 94 → 100 % (ALT-0102 at 01:31), `logrotate.timer`
   failed at 01:20 (EVT-0101), order-service returned 500 "No space left on device" from 02:18
   (ALT-0101), checkout propagated it (ALT-0105), Kafka lag on `order-events` (ALT-0103) explains the
   missing email. Runbook RB-002, prior postmortem PM-2026-01, escalate per OWN-001. The api-gateway
   deploy at 02:04 and the migration at 01:57 are red herrings.
3. Follow up in the same thread: **"Was the customer actually charged twice?"** — the bot should say
   payment succeeded, order persist failed, a retry could double-charge, and paygate is not
   instrumented so it cannot confirm from this data.
4. Try **"Draft a reply to the customer."**
5. Switch cases to show different reasoning paths: **CASE-009** (one category spins →
   `postgres-primary`), **CASE-013** (logged out repeatedly → `api-gateway`/auth), **CASE-017**
   (B2B customer cannot connect at all → `lb-public`).

Good generic questions for any case: *What changed in the window and is any change to blame?* ·
*Which alerts are noise?* · *Is there a runbook and is it current?* · *Who do we page?*

---

## Part 6 — Troubleshooting reference

| Symptom | Cause | Fix |
|---|---|---|
| `python: can't open file '/workspace/app.py'`, health check fails | No run command; buildpack guessed | Run command `uvicorn app.main:app --host 0.0.0.0 --port 8080` (or keep the `Procfile`) |
| `Inference call failed: Error code: 404 - {'detail': 'Not Found'}` | `INFERENCE_BASE_URL` missing `/v1`, or wrong `MODEL` ID | Set `https://inference.do-ai.run/v1`; check `/api/models` |
| `401 Unauthorized` on `/api/models` | Bad or missing key | Re-create the model access key; make sure the env var is `DO_MODEL_ACCESS_KEY` |
| `400 ... tool_choice` | Claude 5.x on DO rejects `tool_choice` | Already removed in `app/agent.py`; pull latest |
| `402` / quota error | Prepaid balance exhausted | Top up under *Serverless Inference → Manage prepayment* |
| Answer says "TRUNCATED" a lot | Model is issuing broad log queries | Normal; it narrows on the next step. Raise `MAX_TOOL_RESULT_CHARS` if needed |
| Investigation stops with a partial summary | Hit `MAX_STEPS` (12) | Raise `MAX_STEPS` env var to 16–20 |
| Dataset 400 "no service named …" in trail | Model misspelled a service | Self-corrects; the error lists valid names |

---

## Part 7 — Configuration reference

| Env var | Default | Notes |
|---|---|---|
| `DO_MODEL_ACCESS_KEY` | — (required) | Encrypt in App Platform |
| `MODEL` | `anthropic-claude-sonnet-5.5` | Any tool-calling model from `/api/models`, e.g. `anthropic-claude-haiku-4.5` (cheaper), `openai-gpt-5.2`, `glm-5.3` |
| `INFERENCE_BASE_URL` | `https://inference.do-ai.run/v1` | `/v1` is appended if missing |
| `DATASET_BASE_URL` | `https://examplekart-dataset-ovp33.ondigitalocean.app` | Point at Rahul's next dataset revision here |
| `MAX_STEPS` | `16` | Tool-call rounds per question (cross-case runs use a few more) |
| `MAX_TOOL_RESULT_CHARS` | `14000` | Per-tool payload cap handed to the model |

Endpoints: `/` UI · `/healthz` · `/api/cases` · `/api/cases/{id}` · `/api/tools` · `/api/models` ·
`POST /api/chat` (SSE stream of `tool_call` / `tool_result` / `answer` events; body
`{"case_id": "CASE-001" | null, "message": "...", "history": []}` — `null` or `"ALL"` selects cross-case mode).

---

## Part 8 — Where to take it next

1. **Knowledge base RAG.** `GET /v1/kb/export` returns all 48 docs. Load into a Gradient AI
   Knowledge Base (Spaces → KB → index with the BGE M3 embedder) and add a semantic-search tool
   alongside the keyword `kb_search`.
2. **Evaluation harness.** Script a fixed question set over all 20 cases, save trail + answer, and
   grade against an answer key (root-cause service, key evidence IDs, owner team). Swap `MODEL` to
   compare Claude vs open-weights on accuracy and cost.
3. **Move the loop into a Gradient Agent** for managed tool routing, KB attachment and evaluation
   metrics; keep this FastAPI app as the UI.
4. **Second use case:** code review, reusing the same agent loop with a different tool set.
