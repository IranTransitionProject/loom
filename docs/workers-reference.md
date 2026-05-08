# Workers Reference

Heddle ships with six ready-made LLM workers and a document extraction module.
Use them directly, chain them into pipelines, or use them as templates for
your own workers.

## Quick Start

```bash
# Use a shipped worker with the Workshop test bench
uv run heddle workshop --port 8080
# Navigate to Workers → summarizer → Test

# Or chain them into a pipeline interactively
uv run heddle new pipeline
```

---

## LLM Workers

### summarizer

Compresses text into a structured summary with key points.

| Field | Value |
|---|---|
| Config | `configs/workers/summarizer.yaml` |
| Tier | `local` (LM Studio or Ollama) |
| Timeout | 30s |

**Input:**

```json
{
  "text": "The text to summarize...",
  "max_points": 5,
  "focus": "economic impact"
}
```

**Output:**

```json
{
  "summary": "2-3 sentence overview",
  "key_points": ["point 1", "point 2"],
  "word_count_original": 1200,
  "word_count_summary": 80
}
```

`max_points` and `focus` are optional. Summary is at least 70% shorter than input.

---

### classifier

Assigns text to one of provided categories with confidence scoring.

| Field | Value |
|---|---|
| Config | `configs/workers/classifier.yaml` |
| Tier | `local` (LM Studio or Ollama) |
| Timeout | 20s |

**Input:**

```json
{
  "text": "Article text...",
  "categories": ["politics", "economics", "sports", "technology"],
  "category_descriptions": {
    "politics": "Government policy, elections, diplomacy"
  }
}
```

**Output:**

```json
{
  "category": "politics",
  "confidence": 0.87,
  "reasoning": "The text discusses parliamentary elections..."
}
```

Categories are passed at runtime — the worker is generic. `category_descriptions`
is optional but improves accuracy. Requires at least 2 categories.

---

### extractor

Pulls structured fields from unstructured text.

| Field | Value |
|---|---|
| Config | `configs/workers/extractor.yaml` |
| Tier | `standard` (Claude Sonnet) |
| Timeout | 45s |

**Input:**

```json
{
  "text": "Contract between Acme Corp and...",
  "fields": [
    {"name": "parties", "description": "Contracting parties", "type": "list", "required": true},
    {"name": "effective_date", "description": "Contract start date", "type": "date"},
    {"name": "value", "description": "Total contract value", "type": "number"}
  ]
}
```

**Output:**

```json
{
  "extracted": {
    "parties": {"value": ["Acme Corp", "Widget Inc"], "source_quote": "between Acme Corp and Widget Inc"},
    "effective_date": {"value": "2026-01-15", "source_quote": "effective January 15, 2026"},
    "value": {"value": 50000, "source_quote": "$50,000 total"}
  },
  "missing_required": []
}
```

Supported types: `string`, `number`, `date`, `list`, `boolean`. Each extracted
field includes the source quote from the text.

---

### translator

Multi-language translation with automatic source language detection.

| Field | Value |
|---|---|
| Config | `configs/workers/translator.yaml` |
| Tier | `local` (LM Studio or Ollama) |
| Timeout | 60s |

**Input:**

```json
{
  "text": "متن فارسی برای ترجمه",
  "target_language": "English",
  "source_language": "Persian"
}
```

**Output:**

```json
{
  "translated_text": "Persian text for translation",
  "source_language": "Persian",
  "target_language": "English",
  "confidence": 0.92
}
```

`source_language` is optional — auto-detected if omitted. Preserves paragraph
structure and proper nouns. If text is already in the target language, returns
it unchanged with confidence 1.0.

---

### qa

Question answering over provided context with source citations. Designed for
RAG pipelines: retrieve chunks via vector search, pass them as context.

| Field | Value |
|---|---|
| Config | `configs/workers/qa.yaml` |
| Tier | `local` (LM Studio or Ollama) |
| Timeout | 45s |

**Input:**

```json
{
  "question": "What was the magnitude of the earthquake?",
  "context": "A 6.2 magnitude earthquake struck southeastern Iran on...",
  "answer_style": "concise"
}
```

**Output:**

```json
{
  "answer": "The earthquake was magnitude 6.2.",
  "confidence": 1.0,
  "source_quotes": ["A 6.2 magnitude earthquake struck"],
  "answerable": true
}
```

Answers ONLY from provided context — no outside knowledge. Sets `answerable`
to `false` when context is insufficient. `source_quotes` are exact substrings.
`answer_style` options: `concise` (default), `detailed`, `bullet_points`.

**RAG pipeline integration:**

```bash
# 1. Search for relevant chunks
results=$(heddle rag search "earthquake damage" --limit 5)

# 2. Pass results as context to QA worker (via Workshop test bench or pipeline)
```

---

### reviewer

Quality review of content against configurable criteria. Generalized from the
blind audit pattern used in production analytical pipelines.

| Field | Value |
|---|---|
| Config | `configs/workers/reviewer.yaml` |
| Tier | `standard` (Claude Sonnet) |
| Timeout | 90s |

**Input:**

```json
{
  "content": "Analysis text to review...",
  "criteria": ["accuracy", "completeness", "clarity", "bias"],
  "context": "This is a policy brief on energy subsidies",
  "severity_threshold": 0.3
}
```

**Output:**

```json
{
  "overall_score": 0.78,
  "overall_pass": true,
  "scores": {
    "accuracy": {"score": 0.9, "assessment": "Claims are well-sourced"},
    "completeness": {"score": 0.6, "assessment": "Missing cost analysis"},
    "clarity": {"score": 0.85, "assessment": "Well-structured"},
    "bias": {"score": 0.75, "assessment": "Slight framing bias in section 3"}
  },
  "issues": [
    {
      "criterion": "completeness",
      "severity": 0.7,
      "description": "No cost-benefit analysis included",
      "suggestion": "Add estimated fiscal impact of subsidy changes",
      "quote": "subsidies should be reformed"
    }
  ],
  "strengths": ["Clear structure", "Good use of primary sources"]
}
```

`criteria` can be any evaluation dimensions — the reviewer adapts. `context`
provides background on what the content should achieve. Only issues above
`severity_threshold` are reported. Uses `standard` tier for stronger reasoning.

---

## Document Processing (`contrib/docproc`)

Three extraction backends for PDF, DOCX, and other document formats. All
produce the same output contract (`ExtractorOutput`), so downstream steps
work unchanged regardless of which backend runs.

### MarkItDownBackend

Fast, lightweight extraction via Microsoft MarkItDown. No ML models, no torch
dependency. Best for well-structured digital documents.

```yaml
# Worker config
processing_backend: "heddle.contrib.docproc.markitdown_backend.MarkItDownBackend"
```

**Supports:** PDF, DOCX, PPTX, XLSX, HTML, plain text.
**Cannot:** OCR scanned PDFs or extract complex table structures.

### DoclingBackend

Deep extraction via IBM Docling with OCR, table structure recognition, and
layout analysis. Requires torch.

```yaml
processing_backend: "heddle.contrib.docproc.docling_backend.DoclingBackend"
```

**Supports:** Scanned PDFs, complex layouts, multi-column documents.
**Config options:** `device` (mps/cpu/cuda), `ocr_engine` (ocrmac/easyocr/tesseract),
`num_threads`, `layout_batch_size`, `ocr_batch_size`.

### SmartExtractorBackend (recommended)

Composite: tries MarkItDown first, falls back to Docling when needed.
Optimizes for speed without sacrificing accuracy on difficult documents.

```yaml
processing_backend: "heddle.contrib.docproc.smart_extractor.SmartExtractorBackend"
```

**Fallback triggers:**

- MarkItDown produces less than 50 characters (likely a scanned document)
- MarkItDown raises an error
- File extension is in `force_docling_extensions` list

Reports `model_used: "markitdown"` or `"docling"` so you know which path ran.

### Extraction output

All backends produce:

```json
{
  "file_ref": "document_extracted.json",
  "page_count": 12,
  "has_tables": true,
  "sections": ["Introduction", "Methods", "Results"],
  "text_preview": "First ~500 words..."
}
```

Full extracted text is written to the workspace directory (not passed through
messages). Downstream steps access it via `file_ref`.

---

## Example Pipelines

### Translate → Summarize

```yaml
pipeline_stages:
  - name: "translate"
    worker_type: "translator"
    input_mapping:
      text: "goal.context.text"
      target_language: "'English'"

  - name: "summarize"
    worker_type: "summarizer"
    input_mapping:
      text: "translate.output.translated_text"
```

### Extract → Review

```yaml
pipeline_stages:
  - name: "extract"
    worker_type: "extractor"
    input_mapping:
      text: "goal.context.document_text"
      fields: "goal.context.extraction_fields"

  - name: "review"
    worker_type: "reviewer"
    input_mapping:
      content: "extract.output.extracted"
      criteria: "'[\"accuracy\", \"completeness\"]'"
```

### Document Processing Pipeline

```yaml
pipeline_stages:
  - name: "extract"
    worker_type: "doc_extractor"
    tier: "local"
    input_mapping:
      file_ref: "goal.context.file_ref"

  - name: "classify"
    worker_type: "classifier"
    input_mapping:
      text: "extract.output.text_preview"
      categories: "'[\"report\", \"invoice\", \"contract\", \"memo\", \"other\"]'"

  - name: "summarize"
    worker_type: "summarizer"
    input_mapping:
      text: "extract.output.text_preview"
```

---

## Creating Custom Workers

Use `heddle new worker` for interactive scaffolding, or write YAML manually.
See [Building Workflows](building-workflows.md) for the full guide.

The existing workers in `configs/workers/` serve as templates — copy one,
modify the system prompt and schemas, and you have a new worker.
