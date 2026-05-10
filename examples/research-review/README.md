# Research Review Pipeline

A three-phase example that builds a research paper review pipeline,
demonstrating workers, pipelines, eval suites, and the blind audit pattern.

**Tutorial:** [docs/tutorials/research-review.md](../../docs/tutorials/research-review.md)

## Phases

### Phase 1 — Single Worker

One `claim_extractor` worker that takes a research abstract and returns
structured claims. Tested in Workshop with an eval suite.

**What you learn:** Worker configs, Workshop test bench, eval suites,
golden baselines.

### Phase 2 — Review Pipeline

Three workers chained in a pipeline: `claim_extractor` →
`methodology_reviewer` → `review_summarizer`. Data flows automatically
between stages.

**What you learn:** Pipeline configs, stage dependencies, input mappings.

### Phase 3 — Blind Adversarial Review

Adds a `terminology_neutralizer` and `blind_reviewer` running in parallel
with the sighted review. A `review_synthesizer` merges both paths and
flags where reviewers disagree.

**What you learn:** Knowledge silos, blind workers, parallel pipeline
branches, the blind audit pattern.

## Quick Start

```bash
# Phase 1: test a single worker
cp examples/research-review/phase-1/workers/claim_extractor.yaml configs/workers/
heddle validate configs/workers/claim_extractor.yaml
heddle workshop
# Open http://localhost:8080 → claim_extractor → Test

# Phase 2: run the review pipeline
cp examples/research-review/phase-2/workers/*.yaml configs/workers/
cp examples/research-review/phase-2/orchestrators/*.yaml configs/orchestrators/
heddle validate configs/workers/methodology_reviewer.yaml
heddle validate configs/workers/review_summarizer.yaml
heddle validate configs/orchestrators/research_review.yaml

# Phase 3: add blind audit
cp examples/research-review/phase-3/workers/*.yaml configs/workers/
cp examples/research-review/phase-3/orchestrators/*.yaml configs/orchestrators/
heddle validate configs/workers/terminology_neutralizer.yaml
heddle validate configs/workers/blind_reviewer.yaml
heddle validate configs/workers/review_synthesizer.yaml
heddle validate configs/orchestrators/research_review_blind.yaml
```

## Sample Data

`sample-data/abstracts.json` contains four synthetic research abstracts
across different domains (public health, NLP, environmental science, AI
safety). Each has intentional methodological strengths and weaknesses.

## Directory Structure

```text
research-review/
├── README.md
├── sample-data/
│   └── abstracts.json              # 4 synthetic abstracts
├── phase-1/
│   ├── workers/
│   │   └── claim_extractor.yaml    # Extract structured claims
│   └── eval/
│       └── test_suite.json         # 4 test cases for field_match scoring
├── phase-2/
│   ├── workers/
│   │   ├── claim_extractor.yaml
│   │   ├── methodology_reviewer.yaml
│   │   └── review_summarizer.yaml
│   └── orchestrators/
│       └── research_review.yaml    # Sequential 3-stage pipeline
└── phase-3/
    ├── workers/
    │   ├── claim_extractor.yaml
    │   ├── methodology_reviewer.yaml
    │   ├── terminology_neutralizer.yaml
    │   ├── blind_reviewer.yaml
    │   └── review_synthesizer.yaml
    └── orchestrators/
        └── research_review_blind.yaml  # Parallel sighted + blind branches
```

Each phase directory is self-contained — it includes all configs needed
to run that phase, including workers from earlier phases.

## Tested by

`tests/test_examples_smoke.py` validates every worker and pipeline
config under `examples/research-review/phase-*/` on every CI run.
End-to-end behaviour (running a phase through real LLM backends with
sample manuscript data) is exercised manually.
