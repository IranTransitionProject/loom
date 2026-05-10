# Heddle Security Model

> *Per-surface trust assumptions for Heddle deployments.*

This document is the authoritative source for what each Heddle
surface trusts, what it does not, and what an operator must do to
move a deployment from "trusted-local default" toward something more
hostile. It complements [`DESIGN_INVARIANTS.md`](DESIGN_INVARIANTS.md)
(which states the framework's safety contracts) and
[`APP_DEPLOYMENT.md`](APP_DEPLOYMENT.md) (which covers the deployment
flow itself).

If you are reading this because you want to expose Heddle outside
your laptop, the short answer is: **set
`HEDDLE_WORKSHOP_TOKEN`, keep `--host 127.0.0.1` unless you know why
you need otherwise, and audit the capability preview on every
deployed app.** Everything below is the long answer.

---

## 1. Trust assumptions per surface

Heddle is composed of several distinct surfaces. Each has its own
threat model, default posture, and operator-controlled hardening
knobs. Read the row for the surface you are exposing.

| Surface | Default posture | What it trusts | Hardening knob |
|---|---|---|---|
| **Workshop web UI** | Bound to `127.0.0.1`, no auth | Anyone who can reach loopback | `HEDDLE_WORKSHOP_TOKEN` env var enables Bearer-header / cookie auth on every mutating route |
| **MCP gateway** | Per-config bind, exposes named tools | Whoever can reach the bound address | Bind to loopback unless the network surface is intentional; review the `tools:` list in the gateway config |
| **NATS bus** | Plain-text, no auth on the default | Anything that can reach the NATS port | Use NATS auth in production; never expose 4222 to the internet |
| **Knowledge silos** | Local filesystem reads/writes per worker config | The worker config's listed `path:` values | Folder paths under operator control; folder_readwrite silos are write-back vectors — review before deploying |
| **Subprocess tool providers** | `shell=False`, but command + args under config control | The worker config that names them | Audit every `processing_backend` and `command:` field in deployed apps |
| **Deployed apps** | Path-safe extraction, name-collision rejection, capability preview | The bundle's referenced configs once confirmed | The deploy preview lists every powerful field — see §3 |
| **CLI (`heddle ...`)** | Runs as the invoking user | The operator's terminal | Process-level isolation only; no Heddle-side gate |

Defaults across the framework lean **trusted-local**: a fresh
`heddle workshop` invocation binds to `127.0.0.1`, no token is
required, and the same-origin middleware blocks malicious browser
tabs but does not authenticate the operator. This is appropriate
for a developer dashboard on a single workstation. Every step
beyond that — LAN binds, multi-user hosts, container deployments —
requires deliberately turning on the knobs in the table above.

---

## 2. Local-only vs. LAN mode

The Workshop has two operational modes. The CLI's
`workshop.starting` log line names the active mode under the
`bind=` and `auth=` fields.

**Local-only (default):**
`heddle workshop` with no extra flags. Bind = `127.0.0.1`, auth =
disabled. Mutating routes are protected by the same-origin
middleware (CSRF), but anyone with shell access on the machine can
issue requests via curl with no credential. Appropriate for a single
operator on a trusted workstation.

**LAN mode:**
`heddle workshop --host 0.0.0.0` (or any non-loopback bind). The CLI
emits a `workshop.insecure_bind` warning at startup if
`HEDDLE_WORKSHOP_TOKEN` is not set. **Set the token before exposing
the bind**:

```bash
export HEDDLE_WORKSHOP_TOKEN="$(openssl rand -hex 32)"
heddle workshop --host 0.0.0.0 --port 8080
```

With the token set:

- Every POST/PUT/PATCH/DELETE requires `Authorization: Bearer <token>`
  or the `heddle_workshop_token` cookie.
- The cookie is set by visiting `GET /login?token=<TOKEN>` once;
  subsequent forms POST with the cookie attached.
- Cookie attributes: `HttpOnly` + `SameSite=Strict` + `Path=/`.
  Operators terminating TLS in front of the workshop should add
  `Secure` via reverse proxy.
- The same-origin middleware still applies — knowing the token does
  not let a cross-origin browser tab mutate.
- Token comparison uses `secrets.compare_digest` so a timing attack
  cannot probe the matching prefix length.

GET routes remain open even with auth enabled. Auth is about
preventing unauthorised mutations, not browse-blocking, and keeping
GETs open lets the `/login` route bootstrap the cookie.

The token mode is the only auth shape Heddle ships today. Basic
auth and session cookies were considered and deferred; if your
deployment needs them, terminate auth in a reverse proxy in front of
the workshop.

---

## 3. Uploaded apps: extraction-safe ≠ sandboxed

App bundles deployed via the Workshop or `AppManager.deploy_app` go
through three layers of validation:

1. **ZIP path safety.** Members with `..` segments, absolute paths,
   or symlinks are rejected before any file is written. Atomic
   extract: files land in a temp dir under `apps_dir` and are moved
   into place only on success.
2. **Name-collision rejection.** Worker and pipeline filenames in
   the new bundle that already exist in base configs or other
   deployed apps cause `AppDeployError` at deploy time. The
   operator must rename one side before redeploying. Implemented
   in `AppManager.deploy_app`'s `forbidden_config_names` check.
3. **Capability preview.** Browser deploys land on a preview page
   listing every powerful field surfaced by the bundle's referenced
   configs:
   - **Code execution** — `processing_backend`, `backend_class`,
     `knowledge_silos` of type `tool`, MCP tools.
   - **Filesystem access** — `workspace_dir`, folder-type knowledge
     silos (read-only vs read+write distinguished),
     `knowledge_sources` paths, `resolve_file_refs`.
   - **Network binding** — MCP `host`/`port`.
   - **Environment variables** — every `${VAR}` / `$VAR` reference.

   The operator must click **Confirm Deploy** for the bundle to
   commit. Cancel discards the staged ZIP. CI / scripted deploys
   that have audited the bundle out-of-band can use
   `?auto_approve=1` to skip the preview.

**What the loader does NOT do:**

- Sandbox config-requested behavior. A confirmed app can still ask
  the worker mesh to import arbitrary Python (via
  `processing_backend`), read or write filesystem paths outside the
  app directory (via `workspace_dir` or folder silos), bind network
  ports (via MCP), and read environment variables (via `${VAR}`).
- Restrict `knowledge_silos` or `workspace_dir` paths to the app
  directory. This is a candidate "restricted app mode" knob for a
  future session (deferred from the D-series scope).
- Audit Python packages installed via `python_package`. If an app
  ships a Python package, you install it manually with
  `pip install -e <path>` — that's an explicit operator step, not a
  Heddle-side action.

**Treat every app bundle as code you are about to run.** Either
audit the source or trust the source. The capability preview gives
you the surface area; it does not vouch for intent.

---

## 4. Subprocess backend risk

Heddle ships `heddle.contrib.subprocess.SubprocessBackend` for
processor workers that need to invoke a local command. Configs
that reference it can run arbitrary commands under the operator's
user account. Specifically:

- `shell=False` is enforced — no shell interpolation on argv.
- `subprocess_timeout` caps individual invocations.
- `command` is taken verbatim from the config; it is not validated
  against an allowlist.

A malicious or misconfigured app config that names a subprocess
backend with a destructive command will run that command if the
operator deploys it. The capability preview surfaces every
`processing_backend` value — confirmation is the gate.

---

## 5. MCP resource exposure

The MCP gateway exposes workspace files as `workspace:///`
resources. Two trust boundaries apply:

- **Path traversal.** `WorkspaceResources.read_resource` resolves
  the requested URI against the workspace directory and rejects any
  path that escapes via `..` (Invariant 13 in
  [`DESIGN_INVARIANTS.md`](DESIGN_INVARIANTS.md#13-path-traversal-protection-uses-resolved-absolute-paths)).
- **Read size cap.** Every read goes through
  `heddle.core.limits.enforce_file_size`, which rejects files
  larger than `DEFAULT_FILE_READ_MAX_BYTES` (10 MiB) before any
  bytes reach memory. Per-call `max_bytes` overrides are
  available for known-large resources. The same cap covers
  `WorkspaceManager.read_text`/`read_json` and the
  knowledge-silo / knowledge-source loaders in
  `heddle.worker.knowledge`.

The traversal check runs **before** the size check, so an attacker
who points at `/etc/passwd` cannot learn its size from the cap
error message.

---

## 6. Secrets and environment variables

Heddle reads the following environment variables; their values
reach the configured backends and may end up in logs if you enable
verbose tracing.

| Env var | Used by | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | LLM backends (standard/frontier tiers) | Sent to Anthropic API |
| `LM_STUDIO_URL`, `OLLAMA_URL` | Local-tier LLM backends | URL only |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | OpenAI-compatible backend | Sent to provider |
| `HEDDLE_WORKSHOP_TOKEN` | Workshop auth | Must be high-entropy if set; never log it verbatim |
| `HEDDLE_TRACE` | Diagnostic tracing | When `=1`, full I/O is logged at DEBUG — keep off in production |
| `HEDDLE_LOCAL_BACKEND` | Local-tier selector | When both LM Studio and Ollama URLs are set, this picks one |

Worker configs can reference env vars in their YAML via `${VAR}`
interpolation (substituted at runtime by the backend layer). The
deploy preview surfaces every such reference so an operator can see
which environment variables a candidate app expects.

**Do not commit secrets to worker configs or app bundles.** Use
env var references and supply the values out-of-band.

---

## 7. Data retention

Heddle persists state in several places. Operators should know
where each lives and which contain potentially sensitive content.

| Store | Path | Contents | Retention |
|---|---|---|---|
| Workshop DuckDB | `~/.heddle/workshop.duckdb` (configurable via `--db-path`) | Worker versions, eval runs/results, metrics, baselines | Keeps everything until you delete the file |
| Deployed apps | `~/.heddle/apps/<name>/` | Extracted app bundles | Removed when the operator clicks **Remove** in the workshop or calls `AppManager.remove_app` |
| Deploy staging | `~/.heddle/apps/.staging/<token>/` | Uploaded ZIPs awaiting confirm | Removed on confirm or cancel; orphaned tokens linger until manual cleanup |
| Dead-letter store | In-memory by default (bounded FIFO; Invariant 12) | Routed-but-rejected task messages | Cleared on workshop restart or via `POST /dead-letters/clear` |
| Workspace files | Per-worker `workspace_dir` | Worker-produced artifacts | Operator-managed; not auto-cleaned |
| Knowledge silo writes | Per-silo `path:` | LLM-generated learnings (folder_readwrite silos) | Operator-managed; review before sharing the silo |

Eval results and worker versions can include payload content from
test runs, which may contain sensitive data depending on what the
operator fed in. The Workshop UI exposes them at
`/workers/{name}/eval/...`; consider whether the workshop's bind
address is appropriate before trusting that data.

---

## Cross-references

- [`docs/workshop.md`](workshop.md) — Workshop architecture and
  route table. Trust model summary at the top of the new
  *Auth + bind* section.
- [`docs/APP_DEPLOYMENT.md`](APP_DEPLOYMENT.md#trust-model) — App
  deployment flow with the in-context trust model section.
- [`docs/DESIGN_INVARIANTS.md`](DESIGN_INVARIANTS.md) — Framework
  invariants. Particularly relevant:
  - Invariant 12: bounded dead-letter store.
  - Invariant 13: path traversal uses resolved absolute paths.
  - Invariant 17: subscribe before publish (orchestrator
    request/reply contract).
- `heddle.workshop.security` —
  Origin check, upload size cap, token auth helpers.
- `heddle.core.limits` — Shared file-read size cap.
- `heddle.workshop.app_capabilities` —
  Deploy-time capability extractor.
