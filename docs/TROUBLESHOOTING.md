# Troubleshooting

Common issues and solutions when running Heddle.

---

## Setup & Configuration

### `heddle setup` can't detect a local LLM runtime

**Symptom:** Setup wizard reports "LM Studio not detected" or "Ollama not detected"
even though one of them is running.

**Fix:**

- LM Studio: confirm the local server is started (LM Studio app → Developer
  tab → Start Server) and a model is loaded. Probe directly with
  `curl http://localhost:1234/v1/models`. When prompted by the wizard,
  enter the URL with the trailing `/v1` (e.g. `http://localhost:1234/v1`).
- Ollama: check it is running with `curl http://localhost:11434/api/tags`.
  If Ollama is on a different port or host, enter the URL when prompted.
  If using Docker Ollama: `docker run -p 11434:11434 ollama/ollama`.
- Both can coexist — the wizard probes each independently. Set
  `HEDDLE_LOCAL_BACKEND=lmstudio` (or `ollama`) to choose which serves
  the local tier when both are configured.

### Thinking model returns empty content (qwen3.5, deepseek-r1, …)

**Symptom:** A worker or council agent backed by an LM Studio
"thinking" model (qwen3.5-9b, qwen3.5-35b-a3b, deepseek-r1, etc.)
produces zero visible output, even though the request succeeds and
token counts are non-zero.

**Cause:** LM Studio splits these models' responses into
`message.content` (the visible answer) and `message.reasoning_content`
(the chain-of-thought). When the model dumps everything into
`reasoning_content` and leaves `content` empty, naive OpenAI parsers
see an empty string.

**Fix:** Heddle's `OpenAICompatibleBackend` and `OpenAIChatBridge`
fall back to `reasoning_content` when `content` is empty, so the
worker never receives a silent-empty response. The raw value is also
surfaced separately on `ChatResponse.reasoning_content` and on the
backend's response dict, so callers can log or strip it.

**Detecting it programmatically:** the rescue is logged at info
level (`backend.reasoning_content.rescue` /
`chatbridge.reasoning_content.rescue` with `model`,
`completion_tokens`, `max_tokens`, and `reasoning_chars`). The raw
trace is also on `response["reasoning_content"]` (backend) /
`ChatResponse.reasoning_content` (bridge), so callers can detect
"this turn used the fallback" with a single null check.

**Disabling the trace at request time** (preferred over rescue when
you control the model choice):

- **Qwen3 family via LM Studio / vLLM:** add
  `extra_body={"chat_template_kwargs": {"enable_thinking": false}}`
  to the request, OR append `/no_think` to the user/system prompt.
  Heddle does not yet pass `extra_body` through — see the
  `TODO(thinking-config)` markers in
  `src/heddle/worker/backends.py` and
  `src/heddle/contrib/chatbridge/openai.py` for the planned knob.
- **DeepSeek-R1 family:** the model's chat template emits the
  `<think>` block by default; some servers (vLLM with the
  `--reasoning-parser` flag) split it onto `reasoning_content`
  automatically. No first-class disable switch; pick a non-R1
  variant if you don't want reasoning.
- **OpenAI o-series (o1, o3, o4-mini):** set
  `reasoning={"effort": "low"}` to minimise the trace
  (cannot be fully disabled). Also not yet plumbed through the
  Heddle backend — same TODO.
- **Anthropic extended thinking:** off by default. Heddle does not
  enable it (`thinking={"type": "enabled", ...}`) on requests, so
  Claude responses won't have a thinking block unless future code
  opts in. See `TODO(anthropic-thinking)` in
  `src/heddle/worker/backends.py` for the planned hookup.
- **Ollama-served thinking models:** newer Ollama builds accept
  `options.think: false` in the chat request, OR
  `chat_template_kwargs={"enable_thinking": false}` for qwen3 GGUFs.
  `OllamaBackend` does not pass these through yet — see
  `TODO(ollama-think-tags)`. For now, the trace appears inline as
  `<think>...</think>` inside `content` (not split out).

**Other escape hatches:**

- Bump `max_tokens` — the model may be spending its whole budget on
  the reasoning trace before producing a final answer. In council
  configs this is `max_tokens_per_turn`; `CouncilRunner` now
  propagates it to bridges automatically.
- Pick a non-thinking model. The blind-taste-test example uses
  `gemma-3-4b`, `nemotron-3-nano-4b`, and `lfm2-24b-a2b` for that
  reason — they emit clean final answers without reasoning blocks.

### LM Studio request fails with "No models loaded"

**Symptom:** A worker or `heddle rag` call against LM Studio returns
HTTP 400 with `{"error": {"message": "No models loaded ...", ...}}`.

**Fix:**

- Open LM Studio's UI and explicitly load the model you want to use,
  or run `lms load <model-id>` from the command line.
- `/v1/models` lists *available* models (downloaded), not loaded ones.
  The chat-completions endpoint refuses to route until a model is
  actively in memory.
- Set `LM_STUDIO_MODEL` to a real id from `/v1/models` (e.g.
  `LM_STUDIO_MODEL=google/gemma-3-4b`) — the literal `default` is only
  routed when LM Studio is configured to auto-load.

### `heddle setup` Anthropic key validation fails

**Symptom:** Setup reports "Key validation failed" after entering an API key.

**Fix:**

- Double-check the key starts with `sk-ant-`
- Verify network connectivity to `api.anthropic.com`
- The key is saved anyway — validation is best-effort
- Test manually: `curl -H "x-api-key: sk-ant-..." https://api.anthropic.com/v1/models`

### Config file not picked up

**Symptom:** Settings from `~/.heddle/config.yaml` don't take effect.

**Fix:**

- Check the file exists: `cat ~/.heddle/config.yaml`
- Env vars override config file values — check for conflicting `LM_STUDIO_URL`, `OLLAMA_URL`, `HEDDLE_LOCAL_BACKEND`, `ANTHROPIC_API_KEY`
- Priority: CLI flags > env vars > config.yaml > defaults
- See [Configuration](CONFIG.md) for the full priority chain

---

## RAG Pipeline

### `heddle rag ingest` fails with "No valid exports found"

**Symptom:** Ingest exits immediately without processing any files.

**Fix:**

- Verify files exist: `ls /path/to/exports/result*.json`
- Telegram exports must be JSON format (not HTML)
- Use Telegram Desktop → Export Chat → JSON format
- File paths are passed as arguments: `heddle rag ingest file1.json file2.json`

### Embedding fails during `heddle rag ingest`

**Symptom:** Ingest hangs or errors at the "Storing chunks" step.

**Fix:**

- Identify which embedding backend Heddle is using:
  `heddle rag --help` (look for the active config) or check
  `cat ~/.heddle/config.yaml`. By default, embeddings follow the
  local-tier choice (LM Studio first, else Ollama).
- LM Studio: confirm an embedding model is loaded
  (`curl http://localhost:1234/v1/models | jq` and look for an `embed`
  in the id, e.g. `text-embedding-nomic-embed-text-v1.5`). Load it
  via the LM Studio UI or `lms load`.
- Ollama: confirm it is running and the model is installed:
  `ollama list | grep nomic-embed-text`; pull with
  `ollama pull nomic-embed-text`.
- Use `--no-embed` to skip embeddings entirely:
  `heddle rag ingest --no-embed files...`
- Force a specific backend on the command line:
  `heddle rag --embedding-backend openai-compatible --lm-studio-url http://localhost:1234/v1 ingest ...`

### `heddle rag search` returns no results

**Symptom:** Search returns "No results found" even after ingesting data.

**Fix:**

- Check the store has data: `heddle rag stats`
- If you ingested with `--no-embed`, search won't work (embeddings required)
- Re-ingest with embeddings: `heddle rag ingest --embed files...`
- Lower the score threshold: `heddle rag search "query" --min-score 0.0`
- Check you're using the same store path: `heddle rag --db-path /path/to/store.duckdb search "query"`

### LanceDB import errors

**Symptom:** `ImportError: No module named 'lancedb'` when using `--store lancedb`.

**Fix:**

```bash
uv sync --extra lancedb
```

---

## NATS Connection

### Cannot connect to NATS

**Symptom:** Actor exits immediately with `bus.connected` never appearing in logs, or error `Could not connect to server`.

**Fix:**

```bash
# Check if NATS is running
nats-server --version  # Should print version
curl -s http://localhost:8222/varz | head -5  # NATS monitoring endpoint

# Start NATS via Docker (quickest)
docker run -d --name nats -p 4222:4222 -p 8222:8222 nats:latest

# Or via Homebrew (macOS)
brew install nats-server
nats-server &

# Or via Docker Compose (full stack)
docker compose up -d
```

### NATS connection drops intermittently

**Symptom:** Log shows `bus.disconnected` followed by `bus.reconnected` (or actor crash after 60s of retries).

**Fix:**

- Check NATS server resource usage (`nats-server` memory, disk, connections)
- Increase NATS max payload if sending large messages: `nats-server --max_payload 4MB`
- If behind a load balancer, ensure idle timeout exceeds NATS ping interval (default 2 min)
- Check network stability between client and NATS server

### Messages silently dropped

**Symptom:** Tasks published but no worker picks them up. No errors in logs.

**Cause:** NATS uses at-most-once delivery. If no subscriber is listening when a message is published, it is silently dropped.

**Fix:**

- Ensure workers are running *before* publishing tasks
- Start actors in the right order: workers → router → orchestrator/pipeline
- Check that `worker_type` in the task matches the worker's subscription (case-sensitive)
- Check `heddle.tasks.dead_letter` for unroutable tasks: `heddle dead-letter monitor`

---

## Workers

### Worker produces empty or invalid output

**Symptom:** Worker completes but output doesn't match `output_schema`. Downstream stages fail with validation errors.

**Fix:**

- Check the worker's system prompt — it must instruct the LLM to output valid JSON matching the schema
- Use the Workshop test bench to test the worker in isolation: `heddle workshop --port 8080`
- Enable full payload logging in the pipeline: `HEDDLE_PIPELINE_VERBOSE=1 heddle worker --config ...` (legacy alias: `HEDDLE_TRACE`, deprecated)
- Verify the LLM backend is responding correctly (try a direct API call)

### ANTHROPIC_API_KEY not set

**Symptom:** Workers using `standard` or `frontier` tier fail with authentication errors.

**Fix:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# Or add to shell profile:
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc
```

### Local LLM URL not set / runtime not running

**Symptom:** Workers using `local` tier fail to connect.

**Fix:**

```bash
# Option A: LM Studio (download from https://lmstudio.ai, load a model,
# then start the server in the Developer tab)
export LM_STUDIO_URL=http://localhost:1234/v1
export LM_STUDIO_MODEL=google/gemma-3-4b   # any id from /v1/models

# Option B: Install and start Ollama
brew install ollama  # macOS
ollama serve &

# Set URL (default is http://localhost:11434)
export OLLAMA_URL=http://localhost:11434

# Pull a model
ollama pull llama3.2
```

### Worker hangs or times out

**Symptom:** Worker never completes. Pipeline shows `PipelineTimeoutError`.

**Fix:**

- Check LLM backend is responsive (try a direct API call)
- Increase `timeout_seconds` in the stage config if the task is legitimately slow
- For Ollama, check if the model is still loading (`ollama ps`)
- Check if the worker is stuck in a tool-use loop (max 10 rounds by default)

---

## Pipelines

### PipelineMappingError: key not found in context

**Symptom:** `Stage 'X' mapping error: Path 'Y.output.Z': key 'Z' not found in context`

**Cause:** A stage's `input_mapping` references a field that the previous stage didn't produce.

**Fix:**

- Check the upstream stage's `output_schema` — does it include the field?
- Test the upstream worker in Workshop to see its actual output
- If the field is optional, add a `condition` to skip the downstream stage when it's missing

### PipelineValidationError: input/output validation failed

**Symptom:** Stage fails before or after execution with schema validation errors.

**Fix:**

- Check `input_schema` / `output_schema` in the stage config
- Use Workshop test bench to verify the worker's actual output format
- Common issue: schema says `"type": "integer"` but worker outputs a string number

### Circular dependency detected

**Symptom:** Pipeline fails to start with `ValueError: Circular dependency detected among stages`.

**Fix:**

- Check `input_mapping` paths — stage A referencing stage B *and* B referencing A creates a cycle
- Use `depends_on` to override automatic dependency inference if needed
- Visualize the dependency graph in Workshop's pipeline editor

---

## Router

### Tasks going to dead letter

**Symptom:** Tasks appear in `heddle.tasks.dead_letter` instead of reaching workers.

**Cause:** Router can't find a matching route for the `worker_type` + `model_tier` combination.

**Fix:**

- Check `configs/router_rules.yaml` for tier overrides
- Verify the `worker_type` in the task matches a running worker's config `name`
- Check rate limits — rate-limited tasks may be dead-lettered
- Monitor dead letters: `heddle dead-letter monitor --nats-url nats://localhost:4222`

---

## Workshop

### Workshop won't start

**Symptom:** `heddle workshop` fails with import errors.

**Fix:**

```bash
# Install workshop dependencies
uv sync --extra workshop

# Or all extras
uv sync --all-extras
```

### App deployment fails

**Symptom:** ZIP upload returns error during app deployment.

**Fix:**

- Verify ZIP contains `manifest.yaml` at the root (not in a subdirectory)
- Check manifest fields: `name`, `version`, `description` are required
- Ensure all config files referenced in `entry_configs` exist in the ZIP
- ZIP must not contain symlinks or paths with `..`
- Build the ZIP using the app's `scripts/build-app.sh` for correct structure

---

## Docker / Kubernetes

### Container can't reach NATS

**Symptom:** Containers fail to connect to `nats://nats:4222`.

**Fix:**

- In Docker Compose: services use the service name as hostname (`nats`)
- Standalone Docker: use `--network host` or link containers
- In Kubernetes: verify the NATS service is in the same namespace
- Check: `docker exec <container> nslookup nats`

### Workshop not accessible from host

**Symptom:** Workshop runs but browser can't reach it.

**Fix:**

- Bind to `0.0.0.0` not `127.0.0.1`: `heddle workshop --host 0.0.0.0 --port 8080`
- Docker: expose the port: `-p 8080:8080`
- Kubernetes: use NodePort (30080) or port-forward: `kubectl port-forward svc/heddle-workshop 8080:8080`

---

## macOS Service (launchd)

### Services not starting after install

**Fix:**

```bash
# Check service status
launchctl list | grep heddle

# Check logs
cat ~/Library/Logs/heddle/workshop.err
cat ~/Library/Logs/heddle/router.err

# Reload services
launchctl unload ~/Library/LaunchAgents/com.heddle.workshop.plist
launchctl load ~/Library/LaunchAgents/com.heddle.workshop.plist
```

### Permission denied

**Fix:**

- launchd user agents don't need sudo — run as your user
- If `heddle` binary is in a restricted path, move it or adjust the plist

---

## Windows Service (NSSM)

### Services not starting

**Fix:**

```powershell
# Check service status
nssm status HeddleWorkshop
nssm status HeddleRouter

# Check logs
Get-Content "$env:LOCALAPPDATA\heddle\logs\workshop.err"

# Restart
nssm restart HeddleWorkshop
```

### NSSM not found

**Fix:**

```powershell
# Install via Chocolatey
choco install nssm

# Or download from https://nssm.cc/download
```

---

## Performance

### Pipeline is slow

**Fix:**

- Design stages with independent dependencies so they run in parallel
- Scale workers horizontally via NATS queue groups (run multiple instances)
- Set `max_concurrent_goals` in pipeline config for concurrent goal processing
- Check token usage logs (`worker.llm_usage`) for expensive stages

### High memory usage

**Fix:**

- Workers are stateless and `reset()` between tasks — check for leaked references
- DuckDB stores can grow large — monitor disk usage
- Dead-letter consumer has a bounded store (default 1000 entries) — adjust `max_size` if needed
- Valkey checkpoint store: check TTL settings for expired entries

---

## Workshop Evaluation

### LLM judge gives inconsistent scores

**Symptom:** Same test case produces different scores across eval runs.

**Fix:**

- Set `temperature=0.0` for the judge backend (this is the default)
- Use a more capable model for judging (the Workshop prefers the `standard` tier)
- Provide a more specific `judge_prompt` tailored to your domain
- Check `score_details.reasoning` in eval results to understand scoring rationale

### Baseline comparison shows no data

**Symptom:** Eval detail page shows no baseline comparison.

**Fix:**

- Promote a successful eval run as baseline first: click "Promote as Baseline" on the eval detail page
- Each worker has at most one baseline; promoting a new one replaces the previous
- Baseline comparison is only shown when viewing a run that is *not* the baseline itself

### Dead-letter replay keeps failing

**Symptom:** Replayed tasks end up back in the dead-letter queue.

**Fix:**

- Check the original reason in the replay log — if "unroutable", ensure a worker for that `worker_type` + `tier` is running
- If "rate_limited", wait for the rate limiter bucket to refill before replaying
- If "malformed", the task data itself is invalid and needs to be fixed at the source

---

## TUI Dashboard

### TUI won't start

**Symptom:** `heddle ui` fails with import errors.

**Fix:**

```bash
# Install TUI dependencies
uv sync --extra tui

# Or all extras
uv sync --all-extras
```

### TUI shows "NATS connection failed"

**Symptom:** Dashboard starts but shows a red "disconnected" status and an error in the Events log.

**Fix:**

- Check that NATS is running: `nats-server --version` or `docker ps | grep nats`
- Verify the URL: `heddle ui --nats-url nats://localhost:4222` (default)
- Check for firewall rules blocking port 4222
- The TUI needs NATS running — it subscribes to `heddle.>` to observe traffic

### TUI shows no events

**Symptom:** Dashboard is connected (green status) but no goals, tasks, or events appear.

**Fix:**

- The TUI is a passive observer — it only shows traffic that occurs while it's running
- Submit a goal or run a pipeline to generate traffic
- Check that actors (router, workers, orchestrator/pipeline) are running
- The TUI subscribes to `heddle.>` which catches all Heddle NATS subjects

### Pipeline stages not appearing

**Symptom:** Goals and tasks appear but the Pipeline tab is empty.

**Fix:**

- Pipeline stage data comes from `_timeline` in result output — only pipeline orchestrators produce this
- Dynamic orchestrators (OrchestratorActor) don't produce timeline data; use pipeline orchestrators for stage visibility
- Check that the pipeline is producing results (look in the Events tab for `heddle.results.*` messages)

---

## Distributed Tracing (OpenTelemetry)

### Tracing not producing spans

**Symptom:** No spans appear in your tracing backend (Jaeger, Zipkin, Tempo).

**Fix:**

```bash
# Install OTel dependencies
uv sync --extra otel

# Verify installation
python -c "from opentelemetry import trace; print('OTel available')"
```

Then initialize tracing at startup:

```python
from heddle.tracing import init_tracing
init_tracing(service_name="heddle")
```

Or set the standard OTel environment variables:

```bash
export OTEL_SERVICE_NAME=heddle
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

### Spans not linking across actors

**Symptom:** You see separate root spans for each actor instead of a connected trace.

**Fix:**

- Heddle propagates trace context via a `_trace_context` key in NATS messages (W3C traceparent format)
- Both the sender and receiver must have OTel installed for propagation to work
- Check that `inject_trace_context()` and `extract_trace_context()` are being called (they are in `BaseActor._process_one()` and all publish methods)
- If using custom actors, ensure you call `inject_trace_context(data)` before publishing and `extract_trace_context(data)` when receiving

### OTel not installed but code imports it

**Symptom:** Worried about import errors when OTel is not installed.

**Fix:**

- This is handled automatically. The `heddle.tracing` module uses runtime feature detection — if OTel SDK is not installed, all functions become no-ops. No code changes needed. See Design Invariant #9.

### LLM spans missing prompt/completion text

**Symptom:** LLM call spans appear in your tracing backend but contain no prompt or completion content.

**Fix:**

Set the `HEDDLE_TRACE_CONTENT` environment variable to enable prompt and completion recording:

```bash
export HEDDLE_TRACE_CONTENT=1
```

With this enabled, LLM call spans include two span events:

- `gen_ai.content.prompt` — the full prompt sent to the model
- `gen_ai.content.completion` — the model's response text

This is disabled by default to avoid storing sensitive data in your tracing backend.

**Note:** Even without `HEDDLE_TRACE_CONTENT`, all LLM call spans include these `gen_ai.*` attributes per the OpenTelemetry GenAI semantic conventions:

| Attribute | Description |
|-----------|-------------|
| `gen_ai.system` | LLM provider (e.g., `anthropic`, `ollama`) |
| `gen_ai.request.model` | Model requested (e.g., `claude-sonnet-4-20250514`) |
| `gen_ai.response.model` | Model that served the request |
| `gen_ai.usage.input_tokens` | Prompt token count |
| `gen_ai.usage.output_tokens` | Completion token count |
| `gen_ai.request.temperature` | Sampling temperature |
| `gen_ai.request.max_tokens` | Max output tokens requested |

---

## ChatBridge horizontal scaling: split-brain sessions

**Symptom:** Multi-turn conversations through a ChatBridge worker
"lose" earlier turns under load — the next turn for the same
session appears to start from scratch, or the model gets confused
because half the history is missing.

**Cause:** ChatBridge sessions
(`heddle.contrib.chatbridge.base._Session`) live in the bridge
instance's in-memory `bridge._sessions` dict. Each worker replica
constructs its own bridge instance, so a follow-up turn that the
router sends to a different replica sees an empty session.
``Invariant 20`` exempts ChatBridge from worker statelessness for
exactly this reason — sessions are *intended* state — but routing
must keep the same session on the same replica.

**Fix:** Deploy ChatBridge workers with `replicas=1`, or front the
worker queue with a session-affinity load balancer that hashes on
`session_id`. See [`BLIND_AUDIT.md`](BLIND_AUDIT.md) for the
broader invariant and `Invariant 20` in
[`DESIGN_INVARIANTS.md`](DESIGN_INVARIANTS.md).
