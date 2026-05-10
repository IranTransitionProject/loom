# ADR-003: Shallow JSON Schema for I/O contracts

**Status:** Accepted.
**Pairs with:** [Invariant 5](../DESIGN_INVARIANTS.md#5-json-schema-validation-is-intentionally-shallow).

## Context

Every Heddle worker declares an `input_schema` and `output_schema`
as JSON Schema. These contracts run on every task — the input
schema validates the payload before the worker sees it; the output
schema validates the worker's reply before it's published.

The validators live in `heddle.core.contracts`. They check:

- Required top-level keys are present.
- Top-level types match (string, number, boolean, object, array).
- That's it.

They explicitly do not validate `$ref`, `allOf`/`oneOf`,
`additionalProperties`, nested-object required fields, string
format constraints (`email`, `uri`, `date-time`), or numeric ranges.

The decision to make: should I/O validation enforce the full JSON
Schema draft, or stop at shallow type checks?

## Decision

**Validation is shallow. The contracts validator checks required
top-level keys and shallow types only. Deeper invariants are the
worker's responsibility, enforced via the system prompt and
output-parsing logic.**

We do not depend on the `jsonschema` library. The validator is a
purpose-built 100-line module that does exactly what it advertises.

## Alternatives considered

### Full JSON Schema validation via the `jsonschema` library (rejected)

The Python ecosystem has a mature `jsonschema` library that
implements every draft of the spec. Wiring it in would give us
nested required-field checks, `allOf`/`oneOf` discrimination,
format validators, and so on.

- **Rejected because** the 90% case is "does this dict have the
  right top-level keys?" — and the 10% case (deeper structural
  invariants) is mostly about LLM output, which an LLM cannot
  reliably satisfy via JSON Schema rules anyway. The framework
  would impose a constraint workers couldn't meet.
- Adds a runtime dependency to the framework's hot path. Every
  task pays the cost; almost no task gets a meaningful benefit
  beyond what shallow validation already catches.
- Encourages schema complexity. Once `oneOf` works, schemas
  accrete branches. The author of the schema, the author of the
  worker prompt, and the author of the downstream caller all
  have to keep three mental models in sync. Worth it for a REST
  API, not worth it for an LLM-output contract.

### Pydantic models as the canonical schema (partially adopted)

Workers can already declare `input_schema_ref` / `output_schema_ref`
pointing to a Pydantic model. `config.resolve_schema_refs()`
converts the model to JSON Schema at config load time, so the
worker config sees a structured schema even though the source of
truth is a Python class.

- **Adopted** for the *authoring* layer — Pydantic gives type
  hints, IDE autocompletion, and Python-level testability.
- **Not adopted** for the *validation* layer at message time —
  the resolved schema is still validated shallowly by
  `contracts.py`. Pydantic models can be deeply expressive in
  ways the framework deliberately does not enforce at runtime.

### Validate strictly on input, loosely on output (rejected)

Tighten input validation (we trust the orchestrator), loosen
output validation (we don't trust LLMs to nail nested schemas).
The two halves would diverge in depth.

- **Rejected because** the asymmetry is confusing. A worker
  author would have to remember "my output_schema is mostly
  decorative; my input_schema is real" — and the worker prompt
  would have to be authored against the looser side. Easier to
  document a single rule: both are shallow.

## Consequences

**Enables:**

- Sub-millisecond validation per message. Validation is a hot
  path; this cost matters.
- No `jsonschema` runtime dependency.
- Workers can be authored without learning the full JSON Schema
  spec. The worker's system prompt does the heavy lifting on
  output shape; the framework's job is to catch the gross
  misconfigurations.
- The validator is auditable in one file. Operators can read
  exactly what's checked.

**Costs:**

- A worker that emits valid top-level keys but malformed nested
  content gets dispatched onward. Downstream callers (or the
  orchestrator's synthesizer) need to handle that — they can't
  rely on the framework to filter it out.
- Bug class: an LLM occasionally emits a string where a nested
  object is expected. The validator passes (top-level type is
  right); the downstream worker crashes on first access. The
  parse-error resilience in `parse_task_result`
  ([ADR-004](004-skip-not-crash-on-malformed.md)) catches this
  at the result-stream layer, but doesn't repair the upstream
  shape.
- One specific gotcha worth calling out: boolean checks come
  before integer checks because Python's `bool` is a subclass
  of `int`. Without this ordering, `True` validates as an
  integer and the worker receives the wrong type. This is
  defended by a regression test in `test_contracts.py`.
