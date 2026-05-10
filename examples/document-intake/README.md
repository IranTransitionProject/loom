# Document Intake Pipeline

A three-phase example that builds a public comment processing system,
demonstrating classification, entity extraction, custom processing
backends, and bias detection through blind audit.

**Tutorial:** [docs/tutorials/document-intake.md](../../docs/tutorials/document-intake.md)

## Phases

### Phase 1 — Single Worker

A `comment_classifier` worker that classifies individual public comments
by topic, stance, and actionability. Tested in Workshop with an eval suite.

**What you learn:** Worker configs, Workshop test bench, eval suites,
domain-specific classification design.

### Phase 2 — Intake Pipeline with Custom Backend

A custom `CsvReaderBackend` (ProcessingBackend) reads a CSV file. The
pipeline chains: `csv_reader` → `comment_classifier` → `entity_extractor`
→ `batch_summarizer`.

**What you learn:** Custom ProcessingBackend, processor workers, pipelines
with mixed worker types (processor + LLM), batch processing.

### Phase 3 — Bias Detection

Adds a `blind_bias_auditor` (sees only classifications, not text) and a
`fairness_reviewer` (sees both) running in parallel with the intake
pipeline.

**What you learn:** Blind audit applied to classification, parallel
pipeline branches, sighted vs. blind analysis.

## Quick Start

```bash
# Phase 1: test the classifier
cp examples/document-intake/phase-1/workers/comment_classifier.yaml configs/workers/
heddle validate configs/workers/comment_classifier.yaml
heddle workshop
# Open http://localhost:8080 → comment_classifier → Test

# Phase 2: run the intake pipeline
cp examples/document-intake/phase-2/workers/*.yaml configs/workers/
cp examples/document-intake/phase-2/orchestrators/*.yaml configs/orchestrators/
# Also ensure the processing module is importable (see tutorial)

# Phase 3: add bias audit
cp examples/document-intake/phase-3/workers/*.yaml configs/workers/
cp examples/document-intake/phase-3/orchestrators/*.yaml configs/orchestrators/
```

## Sample Data

`sample-data/` contains 12 synthetic public comments on a mixed-use
development proposal at a former factory site. Available in two formats:

- `public_comments.json` — for direct use with Workshop
- `public_comments.csv` — for the Phase 2 CSV reader pipeline

Comments span multiple topics (traffic, environment, schools, housing,
historic preservation), stances (support, oppose, conditional support),
and levels of specificity (from technical engineering observations to
vague complaints).

## Custom Processing Backend

`processing/csv_reader.py` contains `CsvReaderBackend` — a custom
`SyncProcessingBackend` implementation (~40 lines of Python) that reads
CSV files. It demonstrates how to write non-LLM processing steps for
Heddle pipelines.

## Directory Structure

```text
document-intake/
├── README.md
├── sample-data/
│   ├── public_comments.json     # 12 comments (JSON)
│   └── public_comments.csv      # Same data in CSV format
├── processing/
│   ├── __init__.py
│   └── csv_reader.py            # CsvReaderBackend implementation
├── phase-1/
│   ├── workers/
│   │   └── comment_classifier.yaml   # Single-item classifier
│   └── eval/
│       └── test_suite.json           # 6 test cases
├── phase-2/
│   ├── workers/
│   │   ├── csv_reader.yaml           # Processor worker config
│   │   ├── comment_classifier.yaml   # Batch-aware classifier
│   │   ├── entity_extractor.yaml     # Batch entity extraction
│   │   └── batch_summarizer.yaml     # Aggregation + report
│   └── orchestrators/
│       └── document_intake.yaml      # 4-stage sequential pipeline
└── phase-3/
    ├── workers/
    │   ├── csv_reader.yaml
    │   ├── comment_classifier.yaml
    │   ├── entity_extractor.yaml
    │   ├── batch_summarizer.yaml
    │   ├── blind_bias_auditor.yaml   # Sees only classifications
    │   └── fairness_reviewer.yaml    # Sees text + classifications
    └── orchestrators/
        └── document_intake_audited.yaml  # With parallel audit branch
```

Each phase directory is self-contained — it includes all configs needed
to run that phase.

## Tested by

`tests/test_examples_smoke.py` validates every worker and pipeline
config under `examples/document-intake/phase-*/` on every CI run.
End-to-end behaviour (running a phase through real LLM backends with
sample CSV data) is exercised manually.
