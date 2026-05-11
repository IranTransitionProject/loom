# Deploy Workshop safely

Workshop is a developer-facing web UI with full control over worker
configs, pipeline configs, eval runs, RAG ingest, and deployed apps.
The default bind is loopback-only with no auth — fine for local
development, **not safe** for anything else without the steps below.

The authoritative threat model lives in
[SECURITY_MODEL.md](../SECURITY_MODEL.md). This runbook is the
operational procedure that follows from it.

## Symptom

You need to stand up Workshop on:

- A LAN bind (so a teammate can reach it).
- A jump host (so you can reach it through SSH port-forwarding +
  someone else can too).
- Anything that isn't `127.0.0.1`.

## Diagnosis

Three layers of safety, in order of decreasing strength:

| Layer | Why it matters | Default state |
| ----- | -------------- | ------------- |
| Bind address | Loopback is unreachable from off-host; anything else needs auth | Loopback (`127.0.0.1`) |
| `HEDDLE_WORKSHOP_TOKEN` | Bearer/cookie auth on every mutating route | Unset (no auth) |
| App capability preview | Two-step deploy gates filesystem/subprocess capability acquisition | Enabled (cannot be disabled outside CI) |

The CLI emits `workshop.insecure_bind` at startup if **bind != loopback
AND token is unset**. Treat that warning as a P1 — it means anyone on
the network can mutate your configs.

## Mitigation

Procedure for a LAN bind:

1. **Generate a high-entropy token.** No fewer than 32 bytes.

    ```bash
    export HEDDLE_WORKSHOP_TOKEN="$(openssl rand -hex 32)"
    ```

    Store the value somewhere a reload can reach (env file,
    secrets manager). Workshop reads it **once at startup** —
    rotating requires a restart.

2. **Start Workshop with the explicit non-loopback bind.**

    ```bash
    uv run heddle workshop --host 0.0.0.0 --port 8000
    ```

    Confirm the startup log does **not** include
    `workshop.insecure_bind`. If it does, the token didn't reach the
    process — check the env-var name.

3. **Distribute the token.** For browser use, visit once:

    ```text
    http://your-host:8000/login
    ```

    Paste the token into the form and submit. Workshop sets a
    cookie; subsequent requests reuse it. The token is sent in the
    POST body so it never appears in uvicorn access logs. For
    programmatic access, pass `Authorization: Bearer <token>` on
    every mutating request.

4. **Confirm app deployment requires capability review.** Try
   deploying an app with elevated capabilities (e.g. subprocess
   backend, non-loopback MCP). The `/apps/deploy` flow should hand
   you a confirmation page enumerating capabilities; nothing
   extracts onto disk until you POST to the `/confirm/{token}`
   route. If it deploys silently, you are running in a CI/auto-
   approve mode — turn that off for any deployment outside a test
   harness.

5. **Plan for the data Workshop sees.** Workshop reads worker
   configs (sometimes containing API key references), reads
   knowledge silos (sometimes containing customer data), and
   accepts uploaded app bundles. Treat the bind address as the
   blast radius of any compromise.

## Verify

```bash
# Without the token, every mutating endpoint should 401
curl -i -X POST http://your-host:8000/workers/test_worker
# Expect: HTTP/1.1 401 Unauthorized

# With the token, the request goes through to the validator
curl -i -X POST http://your-host:8000/workers/test_worker \
  -H "Authorization: Bearer $HEDDLE_WORKSHOP_TOKEN" \
  -F "yaml_content=name: test_worker\nsystem_prompt: ok\n"
# Expect: HTTP/1.1 303 (redirect to detail) or 400 (validation error)
```

GET routes are not auth-gated by default — this is intentional, but
worth knowing. If a read-only leak is also unacceptable, front
Workshop with a reverse proxy that requires auth for all routes.

## Followup

Workshop is not designed to be an internet-facing service. Even
fully token-gated, the surface area (multipart uploads, app
extraction, subprocess backend deploys) is larger than a
production-hardened admin UI would tolerate. For multi-user team
deployments:

- Front it with a reverse proxy that adds SSO and audit logging.
- Run it in a network namespace separate from the production NATS.
- Read [SECURITY_MODEL.md](../SECURITY_MODEL.md) end-to-end before
  the first external user touches it.
