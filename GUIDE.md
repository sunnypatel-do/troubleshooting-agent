# ExampleKart Troubleshooting Agent: Build, Deploy and Use Guide

A web app where you type a **case ID**, and an AI agent investigates it using the data at
`https://examplekart-dataset-ovp33.ondigitalocean.app/`. It runs on **DigitalOcean App Platform** and calls
**DigitalOcean Serverless Inference** for the model.

---

## 1. What you get

- A web page with a case ID box. The agent's tool calls appear live while it works.
- A structured diagnosis: verdict, root-cause service and category, confidence, explanation, timeline, evidence, ruled-out changes, recommended action.
- **Evidence checking:** any cited ID (alert, event, trace, runbook) that no tool ever returned is shown in red as unverified.
- A follow-up box ("Could the gateway deploy have caused this?") that reuses the evidence already gathered.
- A `extra_data/` folder: drop in your own runbooks or notes and the agent can search them.
- Usage numbers per run: time, model calls, tool calls, tokens.

## 2. How it works

```
Browser (index.html)
   | POST /api/investigations {case_id}        poll GET /api/investigations/{id} every ~1s
   v
FastAPI (app.py) --> JobManager (jobs.py) --> background thread --> Investigation (agent.py)
                                                                        |
                                              loop: model picks a tool  |  tools.py runs it
                                                                        v
        DigitalOcean Serverless Inference  <----- messages + tool results ------>  ExampleKart Dataset API
        https://inference.do-ai.run/v1                                              (cases, alerts, events, metrics,
        model access key                                                             logs, traces, KB, entities)
                                                                                   + extra_data/ documents
```

1. You submit a case ID. The server checks the case exists, then starts a background job.
2. The model receives the system prompt and a set of tools. It calls `get_case`, then chooses further tools (alerts, events, metrics, logs, traces, knowledge base) based on what it learns.
3. `tools.py` calls the dataset API and shrinks large results before the model sees them (logs grouped by message, metrics summarized, 6000 characters max per result).
4. When the model has enough evidence it calls `submit_diagnosis`. The page renders it.
5. The browser polls for progress, so long investigations never hit a proxy timeout.

## 3. Files

| File | Purpose |
| :- | :- |
| `app.py` | Web routes: UI, API, `/healthz`, `/api/diagnostics`, optional login |
| `agent.py` | The investigation loop and model calls |
| `tools.py` | Tool definitions, dataset API calls, result shrinking, `extra_data` search |
| `jobs.py` | Background jobs (in memory) |
| `prompts/system.md` | The agent's instructions. Edit this to change behavior |
| `static/index.html` | The whole UI (no build step) |
| `extra_data/` | Your own documents |
| `tests/test_offline.py` | Runs the whole engine against a mock dataset and fake model, no internet or key needed |
| `.do/app.yaml` | Optional App Platform spec |
| `requirements.txt`, `.env.example`, `.gitignore` | Standard project files |

---

## 4. Prerequisites

- A DigitalOcean account.
- A GitHub account (App Platform deploys from a Git repository).
- Python 3.10+ if you want to run it locally first (optional but recommended).

## 5. Step 1: Create a model access key

1. In the DigitalOcean Control Panel, open **Inference** (Serverless Inference).
2. Go to **Model Access Keys** and create a new key. Copy it now; you may not see it again.
3. Keep it secret. It will be used as the `MODEL_ACCESS_KEY` environment variable and must never be committed to Git.

The API base URL is `https://inference.do-ai.run/v1`, and it is OpenAI-compatible.

**Choosing a model.** The agent needs a model with tool (function) calling on the Chat Completions API. Set it with `LLM_MODEL`. Good starting points from DigitalOcean's model list (verified 25 Sep 2026):

| Model ID | Notes |
| :- | :- |
| `anthropic-claude-4.6-sonnet` | Default here. Tool calling, strong reasoning |
| `anthropic-claude-5-sonnet` | Newer Sonnet, tool calling, adaptive thinking on by default |
| `openai-gpt-5.6-terra` | Chat Completions plus tool calling |
| `glm-5.3-flash` or `deepseek-v4.1-flash` | Cheaper open models, worth comparing |

Avoid `openai-gpt-5.5`, `-5.4` and `openai-gpt-6-*` for now: the docs list them as Responses-API-only or restricted for function calling over Chat Completions, which this app uses. Model availability changes, so the diagnostics page (Step 4) lists what your key can actually use.

## 6. Step 2: Get the code onto your machine or Droplet

Download the project zip (`troubleshooting-agent-ui.zip`) from this chat and unzip it. Downloading the zip avoids the copy-and-paste indentation errors you hit earlier.

```bash
scp troubleshooting-agent-ui.zip root@<your-droplet-ip>:~/
ssh root@<your-droplet-ip>
apt update && apt install -y unzip python3-full python3-venv
unzip troubleshooting-agent-ui.zip -d ~/troubleshooting-agent && cd ~/troubleshooting-agent
```

## 7. Step 3: Run it locally first

```bash
python3 -m venv .venv
source .venv/bin/activate          # run this again in every new terminal
pip install -r requirements.txt

# prove the engine works without any key or internet
python tests/test_offline.py       # should end with: ALL OFFLINE TESTS PASSED

# real run
export MODEL_ACCESS_KEY="<your model access key>"
export LLM_MODEL="anthropic-claude-4.6-sonnet"
uvicorn app:app --host 0.0.0.0 --port 8080
```

Open `http://<droplet-ip>:8080` (open port 8080 in your firewall) or `http://localhost:8080`.

## 8. Step 4: Verify the connections

Open `/api/diagnostics` in the browser. It reports:

| Field | What good looks like |
| :- | :- |
| `dataset_healthz` | `200` |
| `dataset_paths` | A list of the dataset's real endpoints (from its `/openapi.json`) |
| `cases_found` | `20` (or however many cases exist) |
| `inference_key` | `ok` |
| `model_available` | `true` |

**Important:** I could not call the live dataset API while writing this, so the endpoint paths in `tools.py` come from the assignment doc (`/v1/cases`, `/v1/cases/{id}/alerts`, `/events`, `/logs`, `/traces`, `/metrics/catalog`, `/metrics/query`, `/v1/kb/search`, `/v1/entities`). Compare them with `dataset_paths`. If one differs, edit the matching line in `Api.run` in `tools.py`. The response parsing already tolerates different wrapper keys and field names.

Then try a real case: enter `CASE-001`. A typical run makes 6 to 12 tool calls and takes under a minute or two, depending on the model. The expected answer is documented in your assignment doc (order-service, resource exhaustion, ALT-0102 and EVT-0101, migration and gateway deploy ruled out).

## 9. Step 5: Push to GitHub

```bash
git init && git add . && git commit -m "Troubleshooting agent"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Check that `.env` is not in the repo (`.gitignore` already excludes it). Never commit the access key.

## 10. Step 6: Deploy on App Platform

**Control panel route (easiest):**

1. **Create > Apps**, choose **GitHub**, authorize, pick your repo and the `main` branch.
2. App Platform detects Python from `requirements.txt`. Keep the resource type as a **Web Service**.
3. Edit the service settings:
   - **Run command:** `uvicorn app:app --host 0.0.0.0 --port 8080`
   - **HTTP port:** `8080`
   - **Instances:** `1` (jobs are kept in memory, so more than one instance would lose track of running investigations)
   - **Health check path:** `/healthz`
4. Add **environment variables** (mark the secrets as **Encrypt**):

   | Key | Value |
   | :- | :- |
   | `MODEL_ACCESS_KEY` | your access key (secret) |
   | `LLM_MODEL` | `anthropic-claude-4.6-sonnet` |
   | `BASIC_AUTH_USER` | a username (e.g. `admin`) |
   | `BASIC_AUTH_PASS` | a long password (secret) |

5. Choose a plan (a 1 GB instance is plenty), create the app, and wait for the build.
6. Open the app URL (`https://<name>.ondigitalocean.app`). Your browser will ask for the username and password.

**Spec-file route:** edit `.do/app.yaml` (repo name), then run `doctl apps create --spec .do/app.yaml` and add the secret values in the control panel. Console labels may change over time; the settings above are what matter.

Every push to `main` redeploys automatically.

**Alternative: keep it on your Droplet.** Run it as a service so it survives logouts:

```bash
cat > /etc/systemd/system/agent.service << 'EOF'
[Unit]
Description=Troubleshooting agent
After=network.target
[Service]
WorkingDirectory=/root/troubleshooting-agent
Environment=MODEL_ACCESS_KEY=<key>
Environment=LLM_MODEL=anthropic-claude-4.6-sonnet
Environment=BASIC_AUTH_USER=admin
Environment=BASIC_AUTH_PASS=<password>
ExecStart=/root/troubleshooting-agent/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now agent && systemctl status agent
```

For public use put HTTPS in front (nginx or Caddy). App Platform gives you HTTPS automatically.

## 11. Using the app

1. Type or pick a case ID and press **Investigate**.
2. Watch the left panel: each row is a tool call, its arguments and a one-line result summary. Short italic notes are the model saying what it is checking.
3. Read the diagnosis on the right. Red evidence chips are IDs the model cited that no tool returned; do not trust those.
4. Use the follow-up box for questions about the same case.
5. The footer shows the model, elapsed time, calls and token counts. Use it to compare models and estimate cost.

## 12. Adding more data

- **Documents:** put `.md`, `.txt`, `.json`, `.log` or `.csv` files in `extra_data/`, commit, and push. The agent finds them through the `search_extra_docs` tool and names the file when it uses one.
- **Dataset growth:** new cases or telemetry appear automatically because the app reads the live API on every run.
- **A new data source** (for example Datadog): add a tool schema and a branch in `tools.py`, and mention it in `prompts/system.md`.
- **Behavior changes:** edit `prompts/system.md` (investigation method, answer style, what to do when evidence is missing).

## 13. Troubleshooting

| Symptom | Likely cause and fix |
| :- | :- |
| `externally-managed-environment` when installing | Use a venv: `python3 -m venv .venv && source .venv/bin/activate` |
| `IndentationError` at line 1 | File was pasted with extra indent. Use the zip instead, or `python -c "import textwrap,sys;f=sys.argv[1];open(f,'w').write(textwrap.dedent(open(f).read()))" file.py` |
| "MODEL_ACCESS_KEY is not set" in the result panel | Set the env var (App Platform: Settings > Environment variables) and redeploy |
| `401` from inference | Wrong or revoked key, or you used a DigitalOcean API token where a model access key is needed |
| `404` model not found | Model ID not in your catalog. See `tool_calling_models_sample` in `/api/diagnostics` |
| "Case ... not found" | Case does not exist in the dataset. Check the suggestions list |
| Tool results show `error 404` | A dataset path in `tools.py` differs from the real API. Compare with `dataset_paths` |
| Agent loops or wastes calls | Try a stronger model, or tighten tool descriptions in `tools.py` |
| Diagnosis says `unstructured` | Model answered in prose instead of calling `submit_diagnosis`. Try another model |
| "Unknown investigation" after a while | App restarted, so in-memory jobs were lost. Run the case again |
| Build fails on App Platform | Check the build log; ensure `requirements.txt` is at the repo root |

## 14. Security and limits

- Always set `BASIC_AUTH_USER` and `BASIC_AUTH_PASS` on a public URL. Without them anyone with the link can spend your inference credits.
- Keys live only in environment variables, never in the repo.
- The agent is read-only. It cannot change anything in ExampleKart.
- One instance, in-memory jobs: fine for a demo or team tool. For scale, store jobs in Postgres or Valkey and run several instances.
- Each run uses model tokens. The footer shows exact counts, and `MAX_STEPS` (default 20) caps tool calls per run.
- Dynamic tool choice varies between runs. Check important conclusions against the evidence list.

## 15. Next steps

- Compare models (run the same cases with two or three `LLM_MODEL` values) and record time, tokens and correctness for your benchmark.
- Add a batch runner that loops over `CASE-002` to `CASE-020` using `Investigation` directly.
- Store finished investigations in Postgres so history survives restarts.
- Replace keyword search in `extra_data/` with embeddings when the document set grows large.
- Add a Slack front end that posts to the same API.
