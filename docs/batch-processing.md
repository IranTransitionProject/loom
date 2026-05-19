# Batch Processing — Iterate Over Many Items

Heddle's docs and examples mostly show how to process **one** item — paste
some text, run a worker, get a result. Real work usually involves
processing many items: a folder of documents, a CSV of records, a list
of URLs, every entry in a log file.

This guide covers three patterns for batch processing, ordered from
simplest to most scalable. Pick the one that matches your scale and
your tolerance for setup.

| Pattern | When to use | Infrastructure |
|---------|-------------|----------------|
| **A. Worker loop** | Single worker, dozens to a few thousand items | None — Python script |
| **B. Manual chain** | Two or three workers in sequence, prototyping a pipeline | None — Python script |
| **C. NATS submit loop** | Continuous processing, thousands of items, multiple worker replicas | NATS + workers + pipeline |

---

## Pattern A: Worker Loop (no NATS)

Use this when you have one worker and a list of inputs. The Workshop's
test runner is the right tool — it's the same code path that powers the
"Run" button in the Workshop UI, but you call it from Python.

```python
# batch_summarize.py
import asyncio
import json
import yaml
from pathlib import Path

from heddle.worker.backends import build_backends_from_env
from heddle.workshop.test_runner import WorkerTestRunner

async def main():
    with open("configs/workers/summarizer.yaml") as f:
        config = yaml.safe_load(f)

    # Backends respect LM_STUDIO_URL / OLLAMA_URL / ANTHROPIC_API_KEY
    backends = build_backends_from_env()
    runner = WorkerTestRunner(backends=backends)

    # Iterate over inputs (here: every .txt file in a folder)
    inputs_dir = Path("inputs/")
    results = []
    for path in sorted(inputs_dir.glob("*.txt")):
        payload = {"text": path.read_text(), "max_length": 200}
        result = await runner.run(config, payload)
        results.append({
            "file": path.name,
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "latency_ms": result.latency_ms,
            "tokens": result.token_usage,
        })
        status = "OK" if result.success else "FAIL"
        print(f"  {path.name}: {status} ({result.latency_ms}ms)")

    Path("results.json").write_text(json.dumps(results, indent=2, default=str))

asyncio.run(main())
```

**Notes:**

- Each `runner.run()` call is fully independent. Workers reset between
  tasks — no state leaks.
- Errors don't stop the loop. Check `result.success` and `result.error`
  per item.
- `result.token_usage` is per-call. Sum it across the batch to estimate cost.

### Concurrent variant — process N items in parallel

Sequential is simple but slow. Use `asyncio.gather` with a semaphore to
bound concurrency (so you don't hammer the LLM provider):

```python
SEM = asyncio.Semaphore(8)  # max 8 concurrent calls

async def process_one(runner, config, path):
    async with SEM:
        payload = {"text": path.read_text(), "max_length": 200}
        result = await runner.run(config, payload)
        return path.name, result

async def main():
    # ... setup as above ...
    paths = sorted(Path("inputs/").glob("*.txt"))
    tasks = [process_one(runner, config, p) for p in paths]
    pairs = await asyncio.gather(*tasks, return_exceptions=True)
    # pairs is [(filename, WorkerTestResult), ...] or exceptions
```

Tune the semaphore size based on your LLM provider's rate limits. Local
backends (LM Studio, Ollama) generally tolerate 2–4 concurrent calls
depending on model size. Anthropic's API tier limits are higher — see
your account dashboard.

---

## Pattern B: Manual Chain (no NATS)

When the unit of work is **a small pipeline** — say, extract → classify
→ summarize — and you want to run it over many inputs without standing
up NATS, chain `WorkerTestRunner` calls manually. You decide what to
pass between stages, which keeps the script trivial to read and debug.

```python
# batch_chain.py
import asyncio
import json
import yaml
from pathlib import Path

from heddle.worker.backends import build_backends_from_env
from heddle.workshop.test_runner import WorkerTestRunner

def load(name):
    with open(f"configs/workers/{name}.yaml") as f:
        return yaml.safe_load(f)

async def process_one(runner, configs, text):
    """Run one input through the chain. Returns (success, final_output, errors)."""
    # Stage 1: extract
    r1 = await runner.run(configs["extractor"], {"text": text})
    if not r1.success:
        return False, None, [f"extractor: {r1.error or r1.validation_errors}"]

    # Stage 2: classify (consumes extractor output)
    r2 = await runner.run(
        configs["classifier"],
        {"entities": r1.output["entities"]},
    )
    if not r2.success:
        return False, None, [f"classifier: {r2.error or r2.validation_errors}"]

    # Stage 3: summarize (consumes both upstream outputs)
    r3 = await runner.run(
        configs["summarizer"],
        {
            "text": text,
            "entities": r1.output["entities"],
            "classification": r2.output["category"],
        },
    )
    if not r3.success:
        return False, None, [f"summarizer: {r3.error or r3.validation_errors}"]

    return True, r3.output, []

async def main():
    backends = build_backends_from_env()
    runner = WorkerTestRunner(backends=backends)
    configs = {
        "extractor": load("extractor"),
        "classifier": load("classifier"),
        "summarizer": load("summarizer"),
    }

    inputs = [p.read_text() for p in sorted(Path("inputs/").glob("*.txt"))]
    results = []
    for i, text in enumerate(inputs):
        ok, out, errs = await process_one(runner, configs, text)
        results.append({"index": i, "success": ok, "output": out, "errors": errs})
        print(f"  item {i}: {'OK' if ok else 'FAIL'}")

    Path("chain_results.json").write_text(json.dumps(results, indent=2, default=str))

asyncio.run(main())
```

**Notes:**

- This is not the same as running an `OrchestratorPipeline` config — you
  write the wiring in Python instead of YAML. The trade-off: you lose
  automatic parallelism inference and conditional stages, but you gain
  full control and visibility.
- For wider concurrency, wrap `process_one` calls in `asyncio.gather`
  with a semaphore (same pattern as Pattern A's concurrent variant).
- Once the chain stabilizes, port it to a YAML pipeline config and use
  Pattern C for production scaling.

### Pattern B+: in-memory pipeline test runner

For the common case of "I have a YAML pipeline; let me test it end-to-end
without NATS", use `PipelineTestRunner`. It spins up an `InMemoryBus`,
instantiates the worker actors the pipeline references, runs a real
`PipelineOrchestrator` over them, and returns a structured
`PipelineTestResult` with per-stage timing and token usage.

```python
from heddle.worker.backends import build_backends_from_env
from heddle.workshop.pipeline_runner import PipelineTestRunner

runner = PipelineTestRunner(backends=build_backends_from_env())
try:
    result = await runner.run(
        "configs/orchestrators/my_pipeline.yaml",
        context={"file_ref": "input.pdf"},
    )
    for stage in result.stage_results:
        print(f"  {stage.stage_name}: {stage.status} ({stage.latency_ms}ms)")
finally:
    await runner.aclose()
```

The manual chain example above stays as the teaching variant — explicit
wiring is pedagogically useful when you're learning Heddle's model — but
once you have a YAML pipeline, `PipelineTestRunner` is the recommended
path. A CLI surface is available too:

```bash
heddle pipeline test configs/orchestrators/my_pipeline.yaml \
    --context file_ref=test.pdf
```

---

## Pattern C: Production Batch (NATS + worker replicas)

When you need to process thousands of items, run multiple worker replicas
across machines, or batch-process on a schedule — bring up the full
distributed mesh and submit goals via NATS.

**Setup (in three terminals, or via Docker Compose):**

```bash
# Terminal 1: NATS + router
nats-server &
heddle router --nats-url nats://localhost:4222

# Terminal 2..N: workers (run multiple replicas of bottleneck workers)
heddle worker --config configs/workers/extractor.yaml --tier local
heddle worker --config configs/workers/extractor.yaml --tier local  # second replica
heddle worker --config configs/workers/classifier.yaml --tier standard
heddle worker --config configs/workers/summarizer.yaml --tier frontier

# Pipeline orchestrator
heddle pipeline --config configs/orchestrators/my_pipeline.yaml
```

NATS load-balances tasks across worker replicas in the same queue group.
Adding a third extractor replica triples extractor throughput with no
code changes.

### Submit from the shell — one-off

```bash
for f in inputs/*.pdf; do
  heddle submit "Process document" --context "file_ref=$f" --context "lang=en"
done
```

### Submit from Python — programmatic

For scripted submission, build `OrchestratorGoal` messages and publish
them directly. This is faster than spawning `heddle submit` per item and
gives you control over `goal_id` for downstream correlation.

```python
# batch_submit.py
import asyncio
from pathlib import Path
import nats

from heddle.core.messages import OrchestratorGoal

async def main():
    nc = await nats.connect("nats://localhost:4222")

    paths = sorted(Path("inputs/").glob("*.pdf"))
    print(f"Submitting {len(paths)} goals...")

    for path in paths:
        goal = OrchestratorGoal(
            instruction="Process document",
            context={"file_ref": str(path), "lang": "en"},
        )
        await nc.publish(
            "heddle.goals.incoming",
            goal.model_dump_json().encode(),
        )

    await nc.flush()
    await nc.close()
    print("All goals submitted. Watch the pipeline orchestrator log for completion.")

asyncio.run(main())
```

**Notes:**

- Goals are fire-and-forget at the NATS layer. The pipeline orchestrator
  picks them up and processes them as worker capacity allows.
- For result collection: configure the pipeline to write outputs to a
  known location (file, database) in its final stage, or subscribe to
  the orchestrator's completion subject in your script.
- Use the TUI dashboard (`heddle ui`) to watch tasks flow through the
  system in real time.
- Dead-letter inspection (`heddle dead-letter monitor`) catches goals
  that no worker could route — useful for debugging at scale.

---

## Choosing a Pattern

- **Just iterating one worker over inputs?** Pattern A. Simplest path,
  no infrastructure, works offline with local LLMs.
- **Iterating a small chain of workers, prototyping?** Pattern B.
  Explicit Python wiring, easy to debug, no infrastructure.
- **Production-scale, continuous processing, or wanting to scale
  horizontally across machines?** Pattern C. Full mesh, NATS, replicas.

A common workflow: develop with Pattern A in the Workshop, prototype
multi-stage flows with Pattern B, deploy to Pattern C when you outgrow
a single Python process.

## Cost and Rate Limit Awareness

Batch jobs can rack up LLM costs fast. Before running over thousands of
items:

- **Estimate first.** Run Pattern A over 10 items, multiply
  `result.token_usage` by your batch size, multiply by your provider's
  per-token price.
- **Use the cheapest tier that works.** The local tier (LM Studio /
  Ollama) is free; standard (Sonnet) is mid-cost; frontier (Opus) is
  expensive. Test which tier each worker actually needs in the Workshop's
  comparison view before batching.
- **Bound concurrency.** Semaphores in Patterns A and B, replica counts
  in Pattern C. Hammering an API tier triggers throttling and wastes
  retries.

## See Also

- [Workshop Tour](WORKSHOP_TOUR.md) — interactive testing and evaluation
  before scaling to batch
- [Building Workflows](building-workflows.md) — defining the workers
  and pipelines you'll batch over
- [Architecture](ARCHITECTURE.md) — message flow and queue group
  semantics for Pattern C
- [Local Deployment](LOCAL_DEPLOYMENT.md) — Docker Compose for the full
  Pattern C mesh
