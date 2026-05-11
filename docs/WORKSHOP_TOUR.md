# Workshop Tour

The Workshop is Heddle's web UI for building, testing, and evaluating workers.
It runs locally — no NATS or cloud services needed. Start it with:

```bash
heddle workshop
# or: heddle workshop --port 8080
```

Open `http://localhost:8080` in your browser. Here's what each screen does.

---

## Workers List

**What you see:** A table of all worker configs found in your `configs/workers/`
directory — both the six shipped workers and any you've created.

**What you can do:**

- Click **Test** to open the test bench for that worker
- Click **Eval** to run a test suite against a worker
- Click a worker name to view and edit its YAML config

**When to use it:** This is your home screen. Come here to pick a worker to
test, or to see what's available.

---

## Test Bench

**What you see:** An input form for the selected worker. The fields match the
worker's input schema — for the summarizer, you'll see a text box; for the
classifier, you'll see text plus a categories field.

**What you can do:**

- Fill in the input fields and click **Run**
- See the structured JSON output, raw LLM response, token usage, and latency
- Change the model tier (local / standard / frontier) to compare outputs
- Iterate: edit the input, run again, compare

**When to use it:** This is the fastest way to try a worker. Paste any text,
click Run, see what you get. No YAML editing, no terminal commands.

**Tip:** The test bench validates your input against the worker's schema before
sending it to the LLM, and validates the output when it comes back. If either
fails, you'll see exactly which fields are wrong — this is the same validation
that runs in production.

---

## Worker Detail / YAML Editor

**What you see:** The full YAML config for a worker, editable in the browser.
Below it: version history (every save is tracked) and a clone form.

**What you can do:**

- Edit the system prompt, I/O schemas, model tier, or timeout
- Save to create a new version (the previous version is preserved)
- Clone a worker as a starting point for a new one
- View the diff between versions

**When to use it:** When you want to tweak a prompt, tighten an output schema,
or create a variation of an existing worker. Edit → save → test → repeat.

---

## Eval Runner

**What you see:** A form where you define test cases: each test case has an
input payload and (optionally) an expected output.

**What you can do:**

- Define a test suite with multiple input/expected-output pairs
- Choose a scoring method:
  - **Field match** — checks whether specific fields in the output match expected values
  - **Exact match** — full output must match exactly
  - **LLM judge** — a separate model scores the output on correctness, completeness, and format
- Run the suite and see per-case scores
- Set a golden dataset baseline for regression detection — future runs are compared against it

**When to use it:** When you need to know whether a prompt change made things
better or worse. Run the eval before and after, compare scores.

---

## Eval Detail

**What you see:** Results for a completed eval run. Each test case shows:
pass/fail, the score, the actual output, and (for LLM judge) the judge's
reasoning.

**What you can do:**

- Expand each case to see full input, expected output, actual output, and scoring details
- Compare against the golden baseline (if one is set)

**When to use it:** After running an eval, to understand which cases passed,
which failed, and why.

---

## Pipeline List

**What you see:** A table of pipeline configs found in `configs/orchestrators/`.

**What you can do:**

- Click a pipeline to open the editor

---

## Pipeline Editor

**What you see:** A visual dependency graph of the pipeline stages, plus forms
for editing stages.

**What you can do:**

- See which stages depend on which (the graph shows parallelism)
- Add, remove, reorder, or swap stages
- Edit input mappings for each stage (how data flows between stages)
- Validate the pipeline graph (catches circular dependencies, missing inputs)

**When to use it:** When you're building or modifying a multi-step workflow.
The visual graph makes it clear what runs in parallel and what's sequential.

---

## RAG Dashboard

**What you see:** An overview of your vector store — document count, embedding
dimensions, disk usage, and a list of ingested channels.

**What you can do:**

- See store statistics at a glance
- Browse channels with trust and bias metadata
- Run semantic searches directly from the browser

**When to use it:** After ingesting data with `heddle rag ingest`, to explore
what's in the store and run searches.

> The RAG dashboard appears when you start Workshop with RAG configured
> (either via `heddle rag serve` or `heddle workshop` after `heddle setup` has
> set RAG paths).

---

## Apps

**What you see:** A list of deployed app bundles — self-contained packages
that include worker configs, pipeline configs, and data files.

**What you can do:**

- Upload a ZIP app bundle
- View the app manifest (what workers and pipelines it contains)
- Remove deployed apps

**When to use it:** When you receive a pre-packaged Heddle application from
someone else and want to deploy it locally.

### Deploy preview

Browser deploys land on a confirmation page that lists every powerful
field the bundle's configs reference: subprocess backends, knowledge
silos, workspace dirs, MCP bindings, environment variables. Nothing
extracts onto disk until you click **Confirm Deploy**; Cancel discards
the staged ZIP. CI / scripted deploys that have audited the bundle
out-of-band can use the `?auto_approve=1` query parameter to skip the
preview. See
[`runbooks/deploy-workshop-safely.md`](runbooks/deploy-workshop-safely.md)
for the operator checklist.

---

## Dead Letters

**What you see:** A table of dead-letter entries — tasks the router
could not deliver because no worker subscribed to their tier/type,
or because the rate limiter rejected the dispatch.

**What you can do:**

- Inspect each entry's original task payload and dead-letter reason.
- Click **Replay** to re-publish a single entry to
  `heddle.tasks.incoming` (so the router gets a second chance).
- **Clear All** to drop every entry from the buffer.

The replay audit log keeps a record of every replay attempt, including
the original reason, so post-incident debugging is straightforward.
The interpretation flow lives in
[`runbooks/interpret-dead-letters.md`](runbooks/interpret-dead-letters.md)
(once a NATS bus is wired in via `--nats-url`, the panel shows live
traffic; without it the panel is empty by design).

---

## What's Next

- **[Workers Reference](workers-reference.md)** — I/O schemas for all six shipped workers
- **[Getting Started](GETTING_STARTED.md)** — install and configure Heddle
- **[Building Workflows](building-workflows.md)** — create custom workers and pipelines
- **[Workshop Architecture](workshop.md)** — internal design for contributors
