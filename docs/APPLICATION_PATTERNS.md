# Heddle Application Patterns — Discipline for Pipelines Built on Heddle

**Purpose:** These are architectural patterns that *applications* built on
Heddle should follow when implementing pipelines with epistemic constraints,
blind audits, knowledge silos, or information barriers. They are not
Heddle framework code — they are design principles that emerge from how
the framework is meant to be used.

**Scope.** This file used to be Part II of
[`DESIGN_INVARIANTS.md`](DESIGN_INVARIANTS.md); it was extracted on
2026-05-11 because the framework-invariants document was mixing two genres
of constraint with different enforcement properties:

- **Framework invariants** ([Design Invariants](DESIGN_INVARIANTS.md)) are
  mechanically checked. Tests, validators, type checks, or code-path
  structure enforces them. A violation produces a test failure, a
  `ValueError` at config load, or a crash at import.
- **Application patterns** (this document) are *not* mechanically checked.
  Heddle cannot tell whether an audit pipeline has its blind nodes
  properly isolated, or whether a universal knowledge silo carries
  analytical content. Applications that violate these patterns produce
  contaminated outputs that look professionally formed — the failure is
  silent and epistemic, not technical.

Read this before designing a Heddle application that uses blind auditing,
knowledge silos, or behavioural monitoring. The framework cannot save you
from the failure modes listed here; only design discipline can.

---

## Patterns

### 1. Knowledge silo isolation is an epistemic quarantine, not a convenience grouping

When an application uses knowledge silos to implement information barriers
(e.g., between analytical workers and audit workers), silos marked as
isolated enforce epistemic quarantine. This is what makes blind audits
actually blind.

**Why:** If audit nodes can access the analytical framework they are supposed
to evaluate, they will pattern-match to existing conclusions and produce
pseudo-confirmatory "independent" judgments. The audit becomes epistemically
worthless — it tells you what you already believe, not whether what you
believe is correct.

**How it fails:** Adding domain knowledge to any audit node's `knowledge_sources`
breaks audit independence. The failure is invisible: audits still produce
professional-looking output, but their conclusions are contaminated by the
framework they were supposed to evaluate. There is no runtime error, no
warning, no indication that the audit is compromised.

**The trap:** It is tempting to "help" audit nodes by giving them more context
so they can be more informed. This is precisely the wrong thing to do. Blind
auditors must be knowledge-deprived by design.

### 2. Neutralization stages are audit firewalls, not text processors

When a pipeline implements blind auditing, a neutralization stage strips
domain-specific vocabulary before blind auditors see the text. The neutralizer
must have minimal knowledge — only the vocabulary mapping and procedural rules.

**Why:** If the neutralizer receives domain knowledge, it leaks domain-specific
framing into the "neutral" text. Audit nodes then receive text that, while
superficially generic, carries the structural fingerprint of domain conclusions.

**How it fails:**

- Adding entity registries to the neutralizer shifts it from lexical to
  semantic processing — a different epistemic role.
- Opaque identifiers (entity codes, reference IDs) must pass through unchanged.
  The neutralizer transforms vocabulary, not references.
- Removing the neutralizer entirely and sending raw text to auditors exposes
  every domain-specific term as a vector for framework contamination.

### 3. Blind auditors should have tiered knowledge deprivation

Not all audit nodes in a blind audit pipeline should be equally blind. Different
audit functions require different levels of knowledge deprivation:

- **Adversarial challengers** should be maximally blind — only procedural rules.
  Giving them evaluation rubrics shifts them from adversarial challenge to
  structured critique, which is a different cognitive function.
- **Structured auditors** (logic, perspective, methodology) need rubrics to know
  *what* to evaluate without knowing *what the domain framework says*.
- **Synthesis nodes** need audit outputs and decision logs to detect blind spots,
  but must not see the domain framework to avoid contamination.

**How it fails:** Adding any knowledge source to an adversarial challenger makes
it less adversarial. Adding domain content to structured auditors makes them
confirmatory. Adding domain content to synthesis nodes makes blind-spot
detection useless (it validates decisions against the framework that produced them).

### 4. Neutralization maps must be computed per-run, not pre-cached

When a neutralizer produces a reverse map (neutral term -> original domain term),
that map must be computed per document, not pre-cached globally.

**Why:** Terminology usage varies by document. A document about one topic uses
different domain terms than a document about another. A global reverse map
produces incorrect de-neutralization.

**How it fails:** Pre-computing a global reverse map produces wrong results for
documents that don't use all terms. Worse, it maps neutral terms back to wrong
domain terms when there are many-to-one mappings.

### 5. Quality gates should be content-driven, not intent-driven

When a pipeline uses flags to trigger escalation (e.g., routing to a more
expensive audit tier), the flag should be set based on the analytical worker's
assessment of content quality, not based on whether the user requested
escalation.

**Why:** Users may not recognize when their work has crossed a quality threshold
that warrants peer review. The flag is a content-quality signal, not a workflow
button.

**How it fails:** If the flag is set based on user intent ("publish this"), users
can bypass the audit pipeline by not requesting escalation. Conversely,
important analytical shifts skip audit because the user didn't ask for review.

### 6. Escalation thresholds gate expensive operations

When an adversarial challenge node produces a high-strength challenge that
triggers escalation (e.g., to manual review with an alternate LLM provider),
the threshold must be calibrated carefully.

**Why:** Escalation targets are expensive (different provider, human review,
longer cycles). The threshold must ensure genuine threats escalate while
routine challenges don't.

**How it fails:** Too low: false-positive escalations waste resources and erode
trust ("the system always escalates"). Too high: genuine analytical failures
pass through to publication.

### 7. Context flows through messages, not worker state

Session identifiers, request context, and inter-stage metadata must flow
through `input_mapping` template references, not through worker instance state.

**Why:** Workers are stateless ([Invariant 1](DESIGN_INVARIANTS.md#1-worker-statelessness-is-enforced-not-optional)).
They cannot track sessions internally. The pipeline's `input_mapping` is the
mechanism for passing context through a stateless execution chain.

**How it fails:**

- If an intermediate worker filters context fields from its output, downstream
  workers lose access and cross-cutting concerns (governance audits, session
  tracking) break silently.
- If context is added to worker instance state instead of flowing through
  messages, multi-replica deployments lose context tracking.

### 8. Behavioral monitors must be isolated from analytical content

When a pipeline includes a behavioral monitoring worker (e.g., monitoring
analyst fatigue, tunnel vision, or cognitive bias), that worker must NOT
have access to the analytical framework or domain database.

**Why:** If a behavioral monitor sees domain content, it evaluates whether the
user's *analysis* is correct rather than whether their *behavior* is healthy.
It becomes a second analytical worker with worse prompting instead of a
cognitive monitor.

**How it fails:** Adding domain content to a behavioral monitor means it flags
analytical disagreements as behavioral anomalies. "User spent 40 minutes on
entity X" gets flagged as tunnel vision even if entity X genuinely requires
deep analysis.

### 9. Universal silos must never contain domain-specific analytical content

If a silo is shared across all workers — including blind auditors — it must
contain only procedural and epistemic discipline rules (source evaluation
standards, neutrality requirements, anti-bias framing). Never analytical
conclusions, domain assessments, or entity evaluations.

**Why:** If a universal silo contains analytical content, blind auditors receive
domain context through the one channel that bypasses all isolation checks.

**How it fails:** Someone adds "current high-priority findings" to a universal
silo as "standing guidance." Now every blind auditor knows what the framework
considers important. Audit independence is destroyed through the one silo
everyone trusts.

### 10. There is no "improve the audit by giving auditors more information"

This is the most frequently proposed and most damaging class of "improvement"
to blind audit pipelines. The information asymmetry between sighted and blind
nodes is the mechanism, not the bug.

**The principle:** Audit quality comes from independence, not from information.
A well-informed auditor who has read the conclusions will confirm them. A blind
auditor who has not read the conclusions will challenge them on logical and
perspectival grounds. Both are necessary. Merging them destroys the one you
can't get any other way.

### 11. YAML configs are the right medium for what they describe

Heddle application configs are typically 80% natural-language system prompts and
20% structural configuration (schemas, mappings, silo assignments). A Python
DSL would help with the structural 20% but would make the prompt 80% harder
to read and edit.

**The right approach:** Generate the structural parts (I/O schemas, silo
references, pipeline topology) from typed Python models. Keep the natural
language in YAML where it's readable without a Python interpreter.

**How it fails:** Replacing all YAML with Python forces system prompts into
Python strings (escaping hell, no syntax highlighting, harder to diff). It
also removes the ability for non-developer domain experts to review and
suggest changes to worker behavior.

### 12. Single-writer processors must be truly single-instance

When a processor worker uses `serialize_writes=True` for a single-writer store
(DuckDB, file-based databases), the application must ensure exactly one instance
runs. The asyncio lock only serializes within one process
([Invariant 11](DESIGN_INVARIANTS.md#11-processorworker-serialize_writes-is-per-instance-only)).

Additionally, no other worker should bypass the designated writer to access the
store directly. The writer typically enforces validation, cross-referencing,
and governance triggers that direct access would skip.

**How it fails:** Running two instances causes write races. Bypassing the writer
skips validation and governance triggers (e.g., operation count thresholds that
fire audit escalations).

---

## Summary — Application Red Lines

These are application-level discipline rules. Heddle's framework cannot
detect or prevent violations — a pipeline that breaks any of them produces
output that looks legitimate but is epistemically contaminated. The
mechanism of failure is silent.

1. **Never add domain content to blind auditor knowledge sources.** Not even
   "just the entity names" or "just the high-level findings." Any domain
   leakage contaminates the audit. (Pattern 1)
2. **Never put analytical content in universal silos.** They are the one
   channel that reaches every node, including blind auditors. (Pattern 9)
3. **Never pre-compute neutralization reverse maps.** They must be
   per-document; global maps map neutral terms back to wrong domain terms.
   (Pattern 4)
4. **Never give blind auditors more information to "improve" the audit.**
   Independence is the mechanism, not the bug. (Pattern 10)
5. **Never let behavioural monitors see analytical content.** They become a
   second analytical worker with worse prompting. (Pattern 8)

Cross-reference: framework-level red lines (mechanically enforced) live in
the [Design Invariants summary](DESIGN_INVARIANTS.md#summary--framework-red-lines).
