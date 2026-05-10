# Building AI Workflows with Heddle

This guide walks you through building your own AI-powered workflows using
Heddle. By the end, you'll know how to create workers, test them in the
Workshop, chain them into pipelines, and optionally deploy to infrastructure.

## Core concepts

**Workers** are the building blocks. Each worker does exactly one thing —
summarize text, classify a document, extract fields, convert a PDF. Workers
are configured entirely through YAML files: a system prompt, input/output
schemas, and a model tier. They process one task and reset. No state
carries between tasks.

There are two kinds of workers:

- **LLM workers** call a language model (Ollama, Anthropic, OpenAI-compatible).
  They're defined by a system prompt and I/O schemas.
- **Processor workers** run non-LLM code (Docling for PDFs, ffmpeg for media,
  scikit-learn, custom Python). They implement a `ProcessingBackend` interface.

**Pipelines** chain workers together so data flows from one to the next.
Heddle figures out which stages can run in parallel from the input mappings —
you don't wire dependencies by hand.

```text
  Document ──► Extract ──► Classify ──► Summarize ──► Report
                              │            │
                              └── (run in parallel — both depend on Extract)
```

**Workshop** is the web UI for testing and evaluating workers. It calls
LLM backends directly — no NATS, no infrastructure. This is where you
develop and refine workers before deploying them.

> **Infrastructure (NATS)** connects workers, routers, and orchestrators
> for production use. You don't need it to get started — the Workshop and
> CLI work without it. See [Part 4](#part-4-deploy-to-infrastructure-nats)
> when you're ready to scale.

## Prerequisites

- Python 3.11+ with `pip install heddle-ai[workshop]` (or `uv sync --extra workshop`)
- At least one LLM backend (Ollama recommended: `ollama pull llama3.2:3b`)
- Run `heddle setup` if you haven't already

That's it. No NATS, no Valkey, no Docker — those are for production later.

## Part 1: Create an LLM worker

LLM workers are the simplest way to add AI capability. You define the
worker entirely in YAML — no Python code needed.

### Step 1: Create the worker config

Use the interactive scaffolding (generates YAML from your answers):

```bash
heddle new worker
```

Or copy the template and edit manually:

```bash
cp configs/workers/_template.yaml configs/workers/entity_extractor.yaml
```

### Step 2: Define the worker

The key sections are the system prompt and I/O schemas:

```yaml
name: "entity_extractor"
description: "Extracts named entities (people, places, orgs) from text."

system_prompt: |
  You are an entity extraction agent. Extract named entities from the input text.

  INPUT FORMAT:
  You will receive a JSON object:
  - text (string): The text to analyze

  OUTPUT FORMAT:
  Respond with ONLY a JSON object:
  {
    "people": ["name1", "name2"],
    "places": ["place1"],
    "organizations": ["org1"],
    "count": <total number of entities>
  }

  RULES:
  - If no entities found, return empty arrays and count: 0
  - Deduplicate entities (same name mentioned twice = one entry)
  - Never output anything except the JSON object

input_schema:
  type: object
  required: [text]
  properties:
    text:
      type: string
      minLength: 1

output_schema:
  type: object
  required: [people, places, organizations, count]
  properties:
    people:
      type: array
      items:
        type: string
    places:
      type: array
      items:
        type: string
    organizations:
      type: array
      items:
        type: string
    count:
      type: integer

default_model_tier: "local"
max_output_tokens: 1000
reset_after_task: true
timeout_seconds: 30
```

The schemas are enforced at runtime — if the LLM returns output that
doesn't match `output_schema`, the task fails with a validation error
rather than propagating garbage downstream.

### Step 3: Validate the config

```bash
heddle validate configs/workers/entity_extractor.yaml
```

This checks the config without starting any infrastructure. If there
are errors (missing fields, invalid schema, bad tier value), you'll see
them immediately.

### Step 4: Test in Workshop

```bash
heddle workshop
# Open http://localhost:8080 → Workers → entity_extractor → Test
```

Paste test input into the form:

```json
{
  "text": "The United Nations was founded in San Francisco in 1945 by Franklin Roosevelt."
}
```

Click **Run**. You'll see the structured JSON output, the raw LLM
response, token usage, and latency. Edit the input and run again to
iterate.

The Workshop calls the LLM backend directly — same validation, same
contract enforcement as production, but no NATS or infrastructure needed.

### Step 5: Evaluate systematically

Once the worker produces reasonable output, set up an evaluation suite
in Workshop:

1. Go to **Workers → entity_extractor → Eval**
2. Define test cases with inputs and expected outputs
3. Choose a scoring method (field match, exact match, or LLM judge)
4. Run the eval and review per-case scores
5. Set a golden baseline for regression detection

Now when you edit the system prompt, you can re-run the eval and see
whether the change helped or hurt. This is the compounding flywheel —
each iteration makes the worker measurably better.

### Worker config validation

Worker configs are validated at startup (and by `heddle validate`).
Validation checks:

- `name` is required (string)
- LLM workers require `system_prompt`; processor workers require
  `processing_backend` (a fully qualified Python class path)
- `default_model_tier` must be `local`, `standard`, or `frontier`
- `input_schema` and `output_schema` must be valid JSON Schema objects
  (with valid `type`, `required` as list, `properties` as dict-of-dicts)
- `timeout_seconds`, `max_input_tokens`, `max_output_tokens` must be
  positive numbers
- `reset_after_task` must be `true` (workers are stateless)
- `resolve_file_refs` requires `workspace_dir` to be set

See `configs/workers/_template.yaml` for the canonical reference with
all fields documented.

## Part 2: Create a processor worker (non-LLM)

Processor workers wrap Python libraries that aren't LLMs. Use these for
document conversion, media processing, data transformation, or any
deterministic computation.

### Step 1: Implement a ProcessingBackend

For async-native backends, implement `ProcessingBackend`:

```python
from heddle.worker.processor import ProcessingBackend

class MyAsyncBackend(ProcessingBackend):
    async def process(self, payload, config):
        # async work here (API calls, async I/O, etc.)
        result = await some_async_operation(payload["input"])
        return {
            "output": {"result": result},
            "model_used": "my-tool-v1",
        }
```

For synchronous/CPU-bound backends (the common case), use
`SyncProcessingBackend`:

```python
from heddle.worker.processor import SyncProcessingBackend

class DoclingBackend(SyncProcessingBackend):
    def process_sync(self, payload, config):
        # CPU-bound work — automatically runs in a thread pool
        # so the async event loop stays responsive
        result = heavy_computation(payload["file_ref"])
        return {
            "output": {"extracted_text": result},
            "model_used": "docling",
        }
```

`SyncProcessingBackend` handles the `run_in_executor` boilerplate for you.

### Step 2: Handle errors with BackendError

For structured error handling, subclass `BackendError`:

```python
from heddle.worker.processor import BackendError, SyncProcessingBackend

class ConversionError(BackendError):
    """Raised when document conversion fails."""

class MyBackend(SyncProcessingBackend):
    def process_sync(self, payload, config):
        try:
            result = convert(payload["file_ref"])
        except Exception as exc:
            raise ConversionError(f"Conversion failed: {exc}") from exc
        return {"output": result, "model_used": "my-converter"}
```

The `from exc` preserves the original exception chain for debugging.

### Step 3: Configure the worker

```yaml
name: "my_processor"
worker_kind: "processor"
processing_backend: "mypackage.backends.MyBackend"
backend_config:
  workspace_dir: "/tmp/my-workspace"

input_schema:
  type: object
  required: [file_ref]
  properties:
    file_ref:
      type: string

output_schema:
  type: object
  required: [extracted_text]
  properties:
    extracted_text:
      type: string

timeout_seconds: 120
```

### Step 4: Validate and test

```bash
# Validate the config
heddle validate configs/workers/my_processor.yaml

# Test in Workshop (processor workers are testable too)
heddle workshop
```

## Part 3: Chain workers into a pipeline

Pipelines run stages with automatic dependency-aware parallelism, wiring
outputs from one stage as inputs to the next. Stage dependencies are
inferred from `input_mapping` paths — stages that only reference `goal.*`
paths or shared earlier stages are independent and run concurrently.

### Step 1: Create a pipeline config

Use the interactive scaffolding:

```bash
heddle new pipeline
```

Or write the YAML manually:

```yaml
name: "analysis_pipeline"
timeout_seconds: 300

pipeline_stages:
  - name: "extract"
    worker_type: "doc_extractor"
    tier: "local"
    input_mapping:
      file_ref: "goal.context.file_ref"

  - name: "classify"
    worker_type: "doc_classifier"
    tier: "local"
    input_mapping:
      text_preview: "extract.output.text_preview"
      page_count: "extract.output.page_count"

  - name: "summarize"
    worker_type: "entity_extractor"
    tier: "local"
    input_mapping:
      text: "extract.output.text_preview"
```

The `input_mapping` wires data between stages:

- `"goal.context.file_ref"` — reads from the original goal's context
- `"extract.output.text_preview"` — reads from the `extract` stage's output

**Automatic parallelism:** In this example, both `classify` and `summarize`
depend only on `extract` (not on each other), so the pipeline automatically
runs them concurrently:

```text
Level 0: extract          (only goal.* deps)
Level 1: classify + summarize  (both depend on extract, run in parallel)
```

To override automatic inference, add explicit `depends_on` lists:

```yaml
  - name: "summarize"
    worker_type: "entity_extractor"
    depends_on: ["extract", "classify"]  # Force sequential after classify
    input_mapping:
      text: "extract.output.text_preview"
```

### Step 2: Validate the pipeline

```bash
heddle validate configs/orchestrators/analysis_pipeline.yaml
```

This checks for duplicate stage names, invalid tier values, malformed
input mappings, unknown `depends_on` references, invalid condition syntax,
and non-positive timeouts — all without starting any infrastructure.

### Step 3: Test individual stages in Workshop

Before running the full pipeline, test each worker in Workshop
individually. This lets you iterate on system prompts and validate I/O
contracts before wiring everything together.

```bash
heddle workshop
# Test each worker: entity_extractor, doc_classifier, doc_extractor
```

The Workshop Pipeline Editor also lets you visualize the dependency graph
and edit stages interactively.

### Conditional stages

Stages can be skipped based on earlier stage outputs:

```yaml
  - name: "ocr"
    worker_type: "ocr_worker"
    tier: "local"
    condition: "extract.output.needs_ocr == true"
    input_mapping:
      file_ref: "goal.context.file_ref"
```

Conditions support `==` and `!=` operators against `true`, `false`, `null`,
and string literals. If the path doesn't exist, the condition evaluates to
false (stage is skipped).

### Concurrent goal processing

Pipelines can process multiple goals simultaneously. Add
`max_concurrent_goals` to the pipeline config:

```yaml
name: "analysis_pipeline"
timeout_seconds: 300
max_concurrent_goals: 4   # Process up to 4 goals concurrently

pipeline_stages:
  # ...
```

Each goal runs in full isolation — its own context dict and result tracking.
This is safe because pipeline execution is stateless per-goal.

### Config validation

All pipeline configs are validated at startup before the actor begins
processing. Validation catches:

- Missing `name` or `pipeline_stages`
- Duplicate stage names
- Invalid tier values (must be `local`, `standard`, or `frontier`)
- Malformed `input_mapping` (must be dict with non-empty string paths)
- `depends_on` referencing unknown stage names
- Invalid `condition` syntax (must be `path op value` with `==` or `!=`)
- Non-positive timeout values

If validation fails, the CLI prints the errors and exits immediately. Fix
the config before restarting.

---

## Part 4: Deploy to infrastructure (NATS)

> **Everything above works without NATS.** The sections below are for
> when you need distributed infrastructure: multi-user, continuous
> processing, horizontal scaling, or production deployment.

### Prerequisites

1. NATS server running (e.g., `docker run -d -p 4222:4222 nats:2.10-alpine`)
2. Optional: Valkey for checkpoint persistence (`docker run -d -p 6379:6379 valkey/valkey:8-alpine`)

### Run workers on the mesh

```bash
# Terminal 1: Start the router
heddle router --nats-url nats://localhost:4222

# Terminal 2: Start a worker
heddle worker --config configs/workers/entity_extractor.yaml --tier local --nats-url nats://localhost:4222

# Terminal 3: Start the pipeline orchestrator
heddle pipeline --config configs/orchestrators/analysis_pipeline.yaml --nats-url nats://localhost:4222

# Terminal 4: Submit a goal
heddle submit "Extract entities from this text" \
  --context text="The United Nations was founded in San Francisco in 1945." \
  --nats-url nats://localhost:4222
```

The worker subscribes to `heddle.tasks.entity_extractor.local` and receives
dispatched work. Scale any component by running more copies — NATS
load-balances via queue groups automatically.

## Part 5: Add knowledge context

LLM workers can have domain-specific knowledge injected into their system prompt. This is useful for glossaries, style guides, domain rules, or few-shot examples.

> **Note:** For folder-based knowledge with write-back support, see Part 8 (Knowledge silos). `knowledge_sources` is simpler but read-only and file-level only.

### Step 1: Create a knowledge file

Knowledge files can be Markdown, YAML, or plain text:

```markdown
# configs/knowledge/legal_terms.md

## Key Legal Terms

- **Habeas corpus**: A court order requiring a person to be brought before a judge.
- **Amicus curiae**: A person or organization not party to a case who offers expertise.
- **Certiorari**: A writ seeking judicial review of a lower court's decision.
```

### Step 2: Reference it in the worker config

```yaml
name: "legal_summarizer"
system_prompt: |
  You are a legal document summarizer. Use the reference material
  provided to ensure accurate use of legal terminology.
  ...

knowledge_sources:
  - path: "configs/knowledge/legal_terms.md"
    inject_as: "reference"
```

Two injection modes:

- **`reference`** — Prepends the file contents to the system prompt with a header.
- **`few_shot`** — Formats YAML/JSONL content as numbered input/output examples.

For few-shot examples, structure your YAML like this:

```yaml
# configs/knowledge/classification_examples.yaml
- input: "Contract between Acme Corp and Beta Inc for software development"
  output: "contract"
- input: "Minutes from the Q3 board meeting held on September 15"
  output: "meeting_minutes"
```

## Part 6: Use file-ref resolution

When pipeline stages produce large data (extracted documents, analysis results), they write JSON files to a shared workspace directory rather than inlining everything in NATS messages. LLM workers can automatically resolve these file references and inject the content into their prompt.

### Step 1: Configure the workspace

Add two fields to your LLM worker config:

```yaml
workspace_dir: "/tmp/my-workspace"
resolve_file_refs: ["file_ref"]
```

### Step 2: How it works

When a task arrives with `payload.file_ref = "report_extracted.json"`:

1. The worker reads `/tmp/my-workspace/report_extracted.json`
2. Parses it as JSON
3. Adds `file_ref_content` to the payload with the parsed data
4. Builds the LLM prompt with both the reference and the content

The LLM sees the full extracted data without it traveling through NATS messages.

### Step 3: Use WorkspaceManager in processor backends

Processor backends can use `WorkspaceManager` directly for safe file I/O:

```python
from heddle.core.workspace import WorkspaceManager

class MyBackend(SyncProcessingBackend):
    def process_sync(self, payload, config):
        ws = WorkspaceManager(config["workspace_dir"])

        # Safe file resolution (blocks path traversal)
        source = ws.resolve(payload["file_ref"])

        # Process the file...
        result = do_work(source)

        # Write output to workspace
        ws.write_json("result.json", result)

        return {
            "output": {"file_ref": "result.json", "status": "done"},
            "model_used": "my-tool",
        }
```

`WorkspaceManager.resolve()` prevents path traversal attacks — any attempt to reference `../../etc/passwd` raises `ValueError`.

## Part 7: Configure routing rules

The router controls which model tier handles each worker type and enforces rate limits.

Edit `configs/router_rules.yaml`:

```yaml
# Force specific workers to specific tiers
tier_overrides:
  entity_extractor: "local"       # always use Ollama
  legal_summarizer: "standard"    # always use Claude Sonnet

# Per-tier rate limits (token-bucket)
rate_limits:
  local:
    max_concurrent: 4
    tokens_per_minute: 100000
  standard:
    max_concurrent: 10
    tokens_per_minute: 200000
  frontier:
    max_concurrent: 3
    tokens_per_minute: 50000
```

Tasks that exceed rate limits are sent to `heddle.tasks.dead_letter` rather than dropped.

## Worker config reference

Full list of fields available in worker YAML configs:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | yes | — | Worker type identifier (matches `worker_type` in task messages) |
| `description` | no | — | Human-readable description |
| `worker_kind` | no | `"llm"` | `"llm"` or `"processor"` |
| `system_prompt` | yes (LLM) | — | The system prompt sent to the LLM |
| `processing_backend` | yes (processor) | — | Fully qualified Python class path |
| `backend_config` | no | `{}` | Dict passed to backend constructor |
| `input_schema` | yes* | — | JSON Schema for input validation |
| `output_schema` | yes* | — | JSON Schema for output validation |
| `input_schema_ref` | no | — | Pydantic model import path for input schema (alternative to `input_schema`) |
| `output_schema_ref` | no | — | Pydantic model import path for output schema (alternative to `output_schema`) |
| `default_model_tier` | no | `"standard"` | `"local"`, `"standard"`, or `"frontier"` |
| `max_input_tokens` | no | `4000` | Max input token budget |
| `max_output_tokens` | no | `2000` | Max output token budget |
| `knowledge_sources` | no | `[]` | List of `{path, inject_as}` for context injection |
| `knowledge_silos` | no | `[]` | List of silo defs — see Part 8 below |
| `workspace_dir` | no | — | Workspace directory for file-ref resolution |
| `resolve_file_refs` | no | `[]` | Payload fields to resolve as workspace file refs |
| `reset_after_task` | no | `true` | Always `true` for workers (statelessness is enforced) |
| `timeout_seconds` | no | `60` | Per-task timeout |

\* Either `input_schema`/`output_schema` (inline JSON Schema) or
`input_schema_ref`/`output_schema_ref` (Pydantic model path) is required.
When using `_ref` fields, the Pydantic model's `.model_json_schema()` is
resolved at config load time via `config.resolve_schema_refs()`.

### Schema refs (Pydantic-driven I/O contracts)

Instead of writing JSON Schema inline, you can point to a Pydantic model:

```yaml
name: "my_worker"
input_schema_ref: "myapp.contracts.MyWorkerInput"
output_schema_ref: "myapp.contracts.MyWorkerOutput"
system_prompt: |
  ...
```

At load time, `resolve_schema_refs()` imports the model class and calls
`.model_json_schema()` to generate the equivalent JSON Schema. This gives
you:

- **Type safety** — Pydantic models are validated by mypy/pyright
- **Reusability** — share models between worker configs and application code
- **Auto-documentation** — field descriptions flow into the schema

The resolved schema replaces `input_schema`/`output_schema` at runtime, so
the rest of the system (contract validation, Workshop, MCP discovery) works
identically.

## NATS subject conventions

| Subject | Purpose |
|---------|---------|
| `heddle.goals.incoming` | Orchestrators subscribe here for new goals |
| `heddle.tasks.incoming` | Router subscribes here for task dispatch |
| `heddle.tasks.{worker_type}.{tier}` | Workers subscribe here (queue groups for competing consumers) |
| `heddle.tasks.dead_letter` | Unroutable or rate-limited tasks land here |
| `heddle.results.{goal_id}` | Results flow back to the orchestrator that owns the goal |

## Part 8: Knowledge silos (folder-based knowledge with write-back)

Knowledge silos are a more powerful alternative to `knowledge_sources`. They support folder-based loading (not just single files), `.siloignore` filtering, and **write-back** — the LLM can persist learned patterns by including `silo_updates` in its output.

### Required vs optional resources

Every silo and `knowledge_sources` entry accepts an optional `required` field
(default **`true`**). When `required: true`, a missing folder, missing source
file, oversize file, or failed tool-provider load fails the task with
`RequiredKnowledgeMissingError` — the runtime returns a `FAILED` `TaskResult`
naming the resource so the operator gets a loud signal instead of a silently
degraded prompt.

When `required: false`, the loader logs a warning, skips the resource, and
records a `{kind, name, reason}` entry on `TaskResult.metadata.degraded_modes`
so callers can detect "ran without resource X" without scraping logs. Use this
opt-out for resources that are genuinely optional — staging silos that may not
exist yet, accumulator folders the LLM creates over time, or tool providers
whose Python package is only installed in some deployments.

The strict-by-default policy is intentional: silent degradation is the most
common cause of "the model used to know about X, why doesn't it now?"
mysteries. Mark optional resources explicitly so the framework can tell them
apart from typos.

### Read-only silo

Load all files from a folder into the system prompt:

```yaml
knowledge_silos:
  - name: "domain_rules"
    type: "folder"
    path: "configs/knowledge/rules/"
    mode: "read"
```

All files in the folder (except those matching `.siloignore` patterns) are concatenated and prepended to the system prompt.

### Read-write silo

Allow the LLM to add, modify, or delete knowledge files:

```yaml
knowledge_silos:
  - name: "learned_patterns"
    type: "folder"
    path: "configs/knowledge/patterns/"
    mode: "readwrite"
```

The LLM can include a `silo_updates` field in its output:

```json
{
  "result": "...",
  "silo_updates": [
    {"silo": "learned_patterns", "action": "add", "filename": "new_pattern.md", "content": "..."},
    {"silo": "learned_patterns", "action": "modify", "filename": "existing.md", "content": "..."},
    {"silo": "learned_patterns", "action": "delete", "filename": "outdated.md"}
  ]
}
```

The `silo_updates` field is automatically stripped from the worker output before validation and downstream propagation.

### .siloignore

Place a `.siloignore` file in any silo folder. It uses gitignore-style patterns to exclude files:

```text
# .siloignore
*.pyc
__pycache__/
*.tmp
```

## Part 9: Tool-use (LLM function-calling)

LLM workers support multi-turn tool-use via the standard function-calling protocol. Tools are loaded dynamically from Python classes.

### Step 1: Implement a ToolProvider

For async tools, implement `ToolProvider`:

```python
from heddle.worker.tools import ToolProvider

class MyTool(ToolProvider):
    def get_definition(self) -> dict:
        return {
            "name": "lookup_record",
            "description": "Look up a record by ID",
            "input_schema": {
                "type": "object",
                "required": ["record_id"],
                "properties": {
                    "record_id": {"type": "string"}
                }
            }
        }

    async def execute(self, arguments: dict) -> str:
        record = await fetch_record(arguments["record_id"])
        return json.dumps(record)
```

For synchronous tools, use `SyncToolProvider` (runs in a thread pool):

```python
from heddle.worker.tools import SyncToolProvider

class MyDBTool(SyncToolProvider):
    def get_definition(self) -> dict:
        return { ... }

    def execute_sync(self, arguments: dict) -> str:
        # CPU-bound or blocking I/O — automatically offloaded
        result = db.query(arguments["sql"])
        return json.dumps(result)
```

### Step 2: Configure tools as knowledge silos

Tools are loaded via `knowledge_silos` with `type: "tool"`:

```yaml
knowledge_silos:
  - name: "db_lookup"
    type: "tool"
    provider: "mypackage.tools.MyDBTool"
    config:
      db_path: "/tmp/data.db"
```

The `config` dict is passed as keyword arguments to the tool class constructor.

### Step 3: How the tool loop works

1. LLM backends receive tool definitions alongside the system prompt
2. If the LLM returns `tool_calls`, the worker executes each tool
3. Tool results are fed back to the LLM as follow-up messages
4. This continues for up to 10 rounds until the LLM produces a final text response

## Part 10: Vector embeddings

Heddle provides an `EmbeddingProvider` abstraction for generating vector embeddings. The built-in implementation uses Ollama's `/api/embed` endpoint.

### Using OllamaEmbeddingProvider

```python
from heddle.worker.embeddings import OllamaEmbeddingProvider

provider = OllamaEmbeddingProvider(
    model="nomic-embed-text",
    base_url="http://localhost:11434",
)

# Single text
embedding = await provider.embed("Some text to embed")
# Returns: list[float] of dimension-sized vector

# Batch
embeddings = await provider.embed_batch(["text1", "text2", "text3"])
# Returns: list[list[float]]
```

### Creating a custom EmbeddingProvider

Implement the `EmbeddingProvider` ABC:

```python
from heddle.worker.embeddings import EmbeddingProvider

class MyEmbeddingProvider(EmbeddingProvider):
    async def embed(self, text: str) -> list[float]:
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        ...

    @property
    def dimensions(self) -> int | None:
        return 768
```

## Tips

- **Keep workers narrow.** A worker that does one thing well is better than a worker that tries to do everything. If you need multiple capabilities, create multiple workers and wire them with a pipeline.
- **Test without infrastructure.** All worker logic can be unit-tested without NATS or Valkey — mock the `publish` method and call `handle_message()` directly. See `tests/test_worker.py` for examples.
- **Use local tier for development.** Ollama with a small model (llama3.2:3b or command-r7b) gives fast iteration. Switch to standard/frontier tiers when you need better reasoning.
- **Schema validation is your safety net.** It catches malformed LLM output before it propagates to downstream stages. Define schemas tightly.
- **Monitor the dead-letter subject.** Tasks landing on `heddle.tasks.dead_letter` indicate routing failures or rate limit hits. Use `nats sub heddle.tasks.dead_letter` during development.
- **LLM calls are auto-instrumented with OTel.** Heddle automatically instruments all LLM calls with OpenTelemetry GenAI semantic conventions. Install the `otel` extra (`uv sync --extra otel`) to get distributed tracing across actors. See [Architecture — Distributed Tracing](ARCHITECTURE.md#distributed-tracing-tracing) for details.

## Part 11: DuckDB integration (`heddle.contrib.duckdb`)

Heddle includes optional DuckDB tools and backends for workflows that need embedded database storage, full-text search, or vector similarity search.

Install the optional dependency:

```bash
uv sync --extra duckdb
```

### DuckDBViewTool — expose a DuckDB view as an LLM tool

Lets the LLM query a read-only DuckDB view during reasoning. The tool auto-introspects the view's column schema and supports search (ILIKE) and list operations.

```yaml
knowledge_silos:
  - name: "catalog"
    type: "tool"
    provider: "heddle.contrib.duckdb.DuckDBViewTool"
    config:
      db_path: "/tmp/workspace/data.duckdb"
      view_name: "summaries"
      description: "Search and browse record summaries"
      max_results: 20
```

### DuckDBVectorTool — semantic similarity search

Finds records similar to a natural language query using vector embeddings stored in DuckDB. Embeds the query via Ollama at search time.

```yaml
knowledge_silos:
  - name: "similar_items"
    type: "tool"
    provider: "heddle.contrib.duckdb.DuckDBVectorTool"
    config:
      db_path: "/tmp/workspace/data.duckdb"
      table_name: "records"
      result_columns: ["id", "title", "summary", "created_at"]
      embedding_column: "embedding"
      tool_name: "find_similar"
      description: "Find semantically similar records"
      embedding_model: "nomic-embed-text"
```

If `result_columns` is omitted, the tool introspects the table schema at first use (excluding embedding and `full_text` columns).

### DuckDBQueryBackend — action-dispatch query backend

A `SyncProcessingBackend` for processor workers that provides configurable query actions against any DuckDB table: full-text search (BM25 via DuckDB FTS), attribute filtering, aggregate statistics, single-record retrieval, and vector similarity search.

```yaml
name: "my_query_worker"
worker_kind: "processor"
processing_backend: "myapp.backends.MyQueryBackend"
backend_config:
  db_path: "/tmp/workspace/data.duckdb"
```

Subclass `DuckDBQueryBackend` to set your schema defaults:

```python
from heddle.contrib.duckdb import DuckDBQueryBackend

class MyQueryBackend(DuckDBQueryBackend):
    def __init__(self, db_path="/tmp/workspace/data.duckdb"):
        super().__init__(
            db_path=db_path,
            table_name="records",
            result_columns=["id", "title", "status", "created_at"],
            json_columns={"tags"},
            filter_fields={
                "status": "status = ?",
                "min_score": "score >= ?",
            },
            stats_groups={"status"},
            stats_aggregates=[
                "COUNT(*) AS record_count",
                "ROUND(AVG(score), 2) AS avg_score",
            ],
            default_order_by="created_at DESC",
        )
```

The backend accepts payloads with `action` set to `search`, `filter`, `stats`, `get`, or `vector_search`, plus action-specific parameters.

## Part 12: MCP gateway (expose HEDDLE as an MCP server)

Any HEDDLE system can become a Model Context Protocol (MCP) server with a single YAML config — zero MCP-specific code needed. Workers, pipelines, and query backends are automatically discovered as MCP tools with typed input schemas.

Install the optional dependency:

```bash
uv sync --extra mcp
```

### Step 1: Create an MCP gateway config

```yaml
# configs/mcp/my_server.yaml
name: "my_server"
description: "My HEDDLE system as an MCP server"
nats_url: "nats://localhost:4222"

tools:
  workers:
    - config: "configs/workers/summarizer.yaml"
    - config: "configs/workers/classifier.yaml"
      name: "classify_document"           # optional name override
      description: "Classify a document"  # optional description override

  pipelines:
    - config: "configs/orchestrators/rag_pipeline.yaml"
      name: "ingest_document"             # required (pipeline has no natural name)
      description: "Ingest and vectorize a document"

  queries:
    - backend: "heddle.contrib.duckdb.query_backend.DuckDBQueryBackend"
      actions: ["search", "filter", "stats", "get"]
      name_prefix: "docs"                # → docs_search, docs_filter, etc.
      backend_config:
        db_path: "/tmp/workspace/data.duckdb"
        table_name: "documents"
        result_columns: ["id", "title", "summary"]

resources:
  workspace_dir: "/tmp/workspace"
  patterns: ["*.pdf", "*.json"]           # optional glob filter
```

### Step 2: How tool discovery works

**Worker tools:** Each worker YAML becomes one MCP tool. The tool name comes from `name` in the worker config (or the MCP entry override). The input schema comes from the worker's `input_schema`. The description comes from the MCP entry override, the worker config `description` field, or the first line of `system_prompt` (in that priority order).

**Pipeline tools:** Each pipeline becomes one MCP tool. The input schema is derived from the first stage's `input_mapping` — keys where the source starts with `goal.context.` become tool input properties.

**Query tools:** Each backend action becomes a separate MCP tool named `{name_prefix}_{action}`. Schemas are auto-generated from the backend configuration (filter fields, stats groups, etc.).

### Step 3: Run the MCP server

```bash
# stdio transport (default — for Claude Desktop, Cursor, etc.)
heddle mcp --config configs/mcp/my_server.yaml

# streamable-http transport (for web clients)
heddle mcp --config configs/mcp/my_server.yaml --transport streamable-http --port 8000
```

### Step 4: Workspace resources

If `resources.workspace_dir` is configured, workspace files are exposed as MCP resources with `workspace:///` URIs. Clients can list and read files matching the configured patterns. After each tool call, the gateway checks for new or modified files and emits change notifications.

### Programmatic usage

```python
from heddle.mcp import create_server, run_stdio, run_streamable_http

server, gateway = create_server("configs/mcp/my_server.yaml")

# stdio (for MCP clients like Claude Desktop)
run_stdio(server, gateway)

# or HTTP
run_streamable_http(server, gateway, host="0.0.0.0", port=8000)
```

---

## Multi-agent deliberation with councils

When the answer to a question benefits from **iterative discussion** rather
than a single pass, use a council instead of a pipeline.

### Council vs. pipeline

Pipelines are deterministic: stage A feeds stage B, data flows one way.
Councils are iterative: agents take turns, react to each other, and
refine their positions over multiple rounds.

Use pipelines when you need repeatability. Use councils when you need
depth — architecture reviews, risk assessments, adversarial analysis,
consensus-building.

### Running a council from code

```python
from heddle.worker.backends import build_backends_from_env
from heddle.contrib.council.config import load_council_config
from heddle.contrib.council.runner import CouncilRunner

config = load_council_config("configs/councils/example.yaml")
runner = CouncilRunner(build_backends_from_env())
result = await runner.run("Should we use event sourcing?", config=config)
print(result.synthesis)
```

### Council as an MCP tool

Add `tools.council` to your MCP gateway config:

```yaml
tools:
  council:
    configs_dir: "configs/councils"
```

This registers `council.start`, `council.status`, `council.transcript`,
`council.intervene`, and `council.stop` as MCP tools.

### Mixing vendors

Use ChatBridge adapters to include agents backed by different LLM
providers in the same discussion:

```yaml
agents:
  - name: "claude_view"
    worker_type: "analyst"
    tier: "standard"
  - name: "gpt_view"
    bridge: "heddle.contrib.chatbridge.openai.OpenAIChatBridge"
    bridge_config:
      model: "gpt-4o"
      api_key_env: "OPENAI_API_KEY"
```

See the full [Council How-To](council-howto.md) for protocols,
convergence detection, transcript management, and design patterns.
