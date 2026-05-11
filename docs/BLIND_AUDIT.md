# Adversarial Review Pipeline

How to set up genuine blind review of AI-generated analysis using Heddle's
knowledge silo isolation.

---

## The Problem

When you ask an AI to review its own work — or when you ask the same model
to both analyze and review — you get pseudo-confirmatory output. The
reviewer has access to the same analytical frame that produced the original
analysis. It pattern-matches against that frame rather than reasoning from
first principles. It confirms what it already "knows."

This isn't a model quality problem. It's a structural problem. The reviewer
can't be independent if it can see the framework it's reviewing against.

## The Solution: Blind Audit

Heddle's knowledge silo system lets you architecturally enforce reviewer
independence. A "blind" worker has an empty knowledge silo — it literally
cannot access the domain knowledge that the analytical worker used. It
can't conform to the analytical frame because it can't see it.

The pattern has three stages:

```text
  Analytical Worker ──► Terminology Neutralizer ──► Blind Reviewer
  (has domain knowledge)   (strips loaded language)    (no domain knowledge)
```

1. A **sighted worker** produces analysis using domain knowledge
2. A **terminology neutralizer** strips domain-specific vocabulary so the
   reviewer isn't primed by loaded language
3. A **blind reviewer** evaluates the neutralized text on its own merits —
   logical consistency, completeness, bias, reasoning quality

The blind reviewer catches things the sighted worker misses because it's
reasoning from first principles, not pattern-matching against the framework.

---

## Step 1: Create the Analytical Worker

This worker has access to domain knowledge. It produces the analysis that
will be reviewed.

```yaml
# configs/workers/analyst.yaml
name: "analyst"
description: "Produces analytical findings from source material."

system_prompt: |
  You are an analytical researcher. Given source material, produce a
  structured analysis with findings, confidence levels, and supporting
  evidence.

  INPUT FORMAT:
  - text (string): Source material to analyze
  - focus (string, optional): Specific aspect to focus on

  OUTPUT FORMAT:
  {
    "findings": [
      {
        "claim": "what you found",
        "confidence": "high/medium/low",
        "evidence": "supporting text from the source"
      }
    ],
    "summary": "overall assessment",
    "caveats": ["limitations or uncertainties"]
  }

knowledge_silos:
  - name: "domain_knowledge"
    type: "folder"
    path: "configs/knowledge/domain/"
    mode: "read"

input_schema:
  type: object
  required: [text]
  properties:
    text:
      type: string
      minLength: 1
    focus:
      type: string

output_schema:
  type: object
  required: [findings, summary]
  properties:
    findings:
      type: array
      items:
        type: object
    summary:
      type: string
    caveats:
      type: array
      items:
        type: string

default_model_tier: "standard"
reset_after_task: true
timeout_seconds: 60
```

The key line: `knowledge_silos` points to a folder of domain knowledge.
This worker can see everything in `configs/knowledge/domain/`.

## Step 2: Create the Terminology Neutralizer

This worker strips domain-specific vocabulary before the blind reviewer
sees the text. Without this step, loaded terminology primes the reviewer
toward the sighted worker's framing.

```yaml
# configs/workers/neutralizer.yaml
name: "neutralizer"
description: "Strips domain-specific terminology to prevent priming."

system_prompt: |
  You are a text neutralizer. Your job is to rewrite analytical text
  so that domain-specific jargon, loaded terms, and framework-specific
  vocabulary are replaced with plain language equivalents.

  RULES:
  - Replace specialized terms with plain descriptions
  - Preserve the logical structure and claims
  - Do NOT add, remove, or change any claims — only change the words
  - Do NOT evaluate the analysis — just neutralize the language
  - Keep the same JSON structure as the input

  INPUT FORMAT:
  - content (string): The analytical output to neutralize

  OUTPUT FORMAT:
  {
    "neutralized_content": "the rewritten text in plain language",
    "terms_replaced": ["list of domain terms that were replaced"]
  }

# No knowledge silos — the neutralizer doesn't need domain knowledge.
# It works purely on language, not content.

input_schema:
  type: object
  required: [content]
  properties:
    content:
      type: string
      minLength: 1

output_schema:
  type: object
  required: [neutralized_content]
  properties:
    neutralized_content:
      type: string
    terms_replaced:
      type: array
      items:
        type: string

default_model_tier: "local"
reset_after_task: true
timeout_seconds: 30
```

Note: no `knowledge_silos` at all. The neutralizer works on language, not
domain content.

## Step 3: Create the Blind Reviewer

This is the critical piece. The blind reviewer has **no knowledge silo** —
it cannot access the domain knowledge that the analytical worker used. It
evaluates the neutralized text purely on logical merit.

```yaml
# configs/workers/blind_reviewer.yaml
name: "blind_reviewer"
description: "Reviews analysis for logical consistency, bias, and completeness without domain knowledge."

system_prompt: |
  You are an independent reviewer. You will receive an analytical text
  that has been stripped of domain-specific terminology. Your job is to
  evaluate it purely on its logical and methodological merits.

  Evaluate against these criteria:
  1. LOGICAL CONSISTENCY — Do the conclusions follow from the evidence?
     Are there contradictions? Unsupported leaps?
  2. COMPLETENESS — Are there obvious gaps? Alternative explanations not
     considered? Missing perspectives?
  3. BIAS — Does the analysis show systematic lean in one direction?
     Does it assume its conclusions? Is evidence selectively presented?
  4. REASONING QUALITY — Are confidence levels appropriate? Are caveats
     genuine or pro-forma?

  INPUT FORMAT:
  - content (string): Neutralized analytical text to review

  OUTPUT FORMAT:
  {
    "overall_score": 0.0-1.0,
    "overall_pass": true/false,
    "issues": [
      {
        "criterion": "which of the four criteria",
        "severity": 0.0-1.0,
        "description": "what the problem is",
        "suggestion": "how to fix it",
        "quote": "relevant excerpt"
      }
    ],
    "strengths": ["what the analysis does well"]
  }

  CRITICAL RULES:
  - You do NOT have domain knowledge. Do not evaluate domain accuracy.
  - Evaluate ONLY the logical structure, reasoning, and methodology.
  - If you cannot assess something without domain knowledge, say so.
  - Be specific — cite exact passages, not vague impressions.

# DELIBERATELY EMPTY — this is what makes the audit blind.
# The reviewer cannot access domain knowledge and cannot conform
# to the analytical frame it is evaluating.
knowledge_silos: []

input_schema:
  type: object
  required: [content]
  properties:
    content:
      type: string
      minLength: 1

output_schema:
  type: object
  required: [overall_score, overall_pass, issues]
  properties:
    overall_score:
      type: number
    overall_pass:
      type: boolean
    issues:
      type: array
      items:
        type: object
    strengths:
      type: array
      items:
        type: string

default_model_tier: "standard"
reset_after_task: true
timeout_seconds: 90
```

The critical design choice: `knowledge_silos: []` is explicitly empty.
This isn't an oversight — it's what makes the audit genuinely blind. The
reviewer can't conform to the analytical frame because it literally doesn't
have access to it.

## Step 4: Chain Them into a Pipeline

```yaml
# configs/orchestrators/blind_audit_pipeline.yaml
name: "blind_audit"
timeout_seconds: 300

pipeline_stages:
  - name: "analyze"
    worker_type: "analyst"
    tier: "standard"
    input_mapping:
      text: "goal.context.text"
      focus: "goal.context.focus"

  - name: "neutralize"
    worker_type: "neutralizer"
    tier: "local"
    input_mapping:
      content: "analyze.output.summary"

  - name: "blind_review"
    worker_type: "blind_reviewer"
    tier: "standard"
    input_mapping:
      content: "neutralize.output.neutralized_content"
```

Validate:

```bash
heddle validate configs/workers/analyst.yaml \
              configs/workers/neutralizer.yaml \
              configs/workers/blind_reviewer.yaml \
              configs/orchestrators/blind_audit_pipeline.yaml
```

## Step 5: Test Each Stage in Workshop

Before running the full pipeline, test each worker individually:

```bash
heddle workshop
```

1. **Test the analyst** — paste source text, check that findings are
   structured and sourced
2. **Test the neutralizer** — paste the analyst's output, check that
   domain terms are replaced without changing claims
3. **Test the blind reviewer** — paste the neutralizer's output, check
   that the review evaluates logic and methodology without relying on
   domain knowledge

Iterate on each worker's system prompt until each stage produces
consistent, useful output. Use the eval runner to set up regression
tests for each worker.

## What the Blind Reviewer Catches

The value of blind review isn't catching obvious errors — a sighted
reviewer can do that too. It's catching the subtle structural problems
that domain familiarity makes invisible:

- **Circular reasoning** — "this is important because our framework says
  it's important." A sighted reviewer who shares the framework doesn't
  notice. A blind reviewer does.
- **Confirmation bias** — selectively presenting evidence that supports
  the preferred conclusion. A reviewer steeped in domain context is
  susceptible to the same bias. A blind reviewer evaluates evidence
  balance without preconceptions.
- **Missing alternatives** — when the analyst doesn't consider an
  explanation because the domain framework doesn't include it. The blind
  reviewer, free of that framework, asks "what else could explain this?"
- **False precision** — confidence levels that sound authoritative but
  aren't supported by the evidence. Easier to spot without domain
  knowledge biasing your sense of "what's obvious."

## Going Further

### Use different models for analyst and reviewer

For maximum independence, run the analyst on one model provider and the
blind reviewer on another. Different training data means different biases.

```yaml
# analyst.yaml
default_model_tier: "standard"  # Claude Sonnet

# blind_reviewer.yaml
default_model_tier: "local"     # Ollama (different model, different biases)
```

Or use the `frontier` tier for the reviewer if the analysis is
high-stakes.

### Tiered knowledge deprivation

Not all blind workers need to be equally blind. Different audit functions
benefit from different levels of knowledge:

- **Adversarial challengers** — maximally blind, only procedural rules.
  Giving them evaluation rubrics shifts them from challenge to checkbox.
- **Structured auditors** (logic, methodology) — need rubrics to know
  what to check, but no domain content.
- **Synthesis nodes** — need audit outputs and decision logs to detect
  blind spots, but not the original domain knowledge.

### Add an audit synthesizer

In production, you'll want a fourth worker that combines the original
analysis with the blind review into a decision-ready report:

```yaml
  - name: "synthesize"
    worker_type: "audit_synthesizer"
    tier: "standard"
    input_mapping:
      original_analysis: "analyze.output"
      blind_review: "blind_review.output"
```

The synthesizer sees both the analysis and the review, and produces a
final report that incorporates the reviewer's concerns.

---

## Related

- **[Building Workflows](building-workflows.md)** — full guide to workers,
  pipelines, and knowledge silos
- **[Why Heddle?](WHY_HEDDLE.md)** — how the blind audit pattern fits into
  Heddle's input-layer approach
- **[Design Invariants](DESIGN_INVARIANTS.md)** — architectural rules
  for knowledge isolation (Part II, invariants 18-27)
- **[Workers Reference](workers-reference.md)** — the shipped `reviewer`
  worker is a generalized version of this pattern
