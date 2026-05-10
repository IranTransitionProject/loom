# App Deployment Guide

**Heddle — Deploying Application Bundles**

---

## Overview

A Heddle **app** is a ZIP archive containing worker configs, pipeline configs,
and an optional Python package. Apps are deployed through the Workshop web UI
or programmatically via the `AppManager` class.

After deployment, running actors are notified to reload their configs via
the NATS control channel — no restart required.

---

## Manifest Format

Every app ZIP must contain a `manifest.yaml` at the root:

```yaml
name: "myapp"                         # Required. lowercase, hyphens, underscores
version: "1.0.0"                      # Required. Semantic version
description: "My Heddle application"    # Required. Human-readable
heddle_version: ">=0.4.0"              # Minimum heddle version

required_extras:                      # Heddle extras this app needs
  - duckdb
  - mcp

python_package:                       # Optional — for apps with Python code
  name: "myapp"
  install_path: "src/"

entry_configs:
  workers:
    - config: "configs/workers/my_worker.yaml"
      tier: "standard"
  pipelines:
    - config: "configs/orchestrators/my_pipeline.yaml"
  schedulers:
    - config: "configs/schedulers/my_schedule.yaml"
  mcp:
    - config: "configs/mcp/my_mcp.yaml"

scripts:
  - path: "scripts/setup.py"
    description: "Initial setup script"
```

---

## Building an App ZIP

### Using the build script

Both `baft` and `docman` include build scripts:

```bash
# Build baft app bundle
cd baft/
bash scripts/build-app.sh
# Output: dist/baft-0.2.0.zip

# Build docman app bundle
cd docman/
bash scripts/build-app.sh
# Output: dist/docman-0.4.0.zip
```

### Manual build

```bash
cd myapp/
zip -r dist/myapp-1.0.0.zip \
    manifest.yaml \
    configs/ \
    -x "*.pyc" "__pycache__/*"
```

---

## Deploying via Workshop

1. Start the Workshop: `heddle workshop --port 8080`
2. Navigate to **Apps** in the navigation bar
3. Upload your `.zip` file using the deploy form
4. Review the **capability preview** that appears (see "Trust model" below)
5. Click **Confirm Deploy** to commit, or **Cancel** to discard
6. The app's workers and pipelines appear in the Workers and Pipelines lists

For scripted / CI deploys that have already audited the bundle, append
`?auto_approve=1` to the deploy URL to skip the preview step.

### Trust model

Heddle's app loader validates ZIP path safety (rejects `..` segments,
absolute paths, symlinks) and rejects deploys whose worker or pipeline
filenames collide with existing configs (base or other apps). **It does
not sandbox the configs themselves.** A well-formed app config can still
ask the worker mesh to:

- Import and execute arbitrary Python (`processing_backend`,
  `backend_class`, `knowledge_silos` of type `tool`)
- Read and write filesystem paths outside the app directory
  (`workspace_dir`, `knowledge_silos` of folder type, `knowledge_sources`
  paths, `resolve_file_refs`)
- Bind network ports (MCP gateway `host`/`port`)
- Read environment variables via `${VAR}` interpolation

The deploy preview surfaces every such field grouped by category before
the bundle touches `~/.heddle/apps/`. Treat the preview as a "what code
am I about to run" gate — only deploy bundles you trust the source of, or
audit the listed capabilities first. Bundles uploaded via
`?auto_approve=1` skip this gate, so reserve that flag for sources where
the audit happens earlier in the pipeline.

For the full per-surface trust model (Workshop auth, MCP gateway,
NATS bus, knowledge silos, subprocess backends, secrets, data
retention) see [`SECURITY_MODEL.md`](SECURITY_MODEL.md).

### Hot Reload

After deployment, the Workshop publishes a reload message to
`heddle.control.reload`. Running actors re-read their configs from disk
without restart. This works for:

- Workers (TaskWorker, LLMWorker, ProcessorWorker)
- Pipeline orchestrators
- Dynamic orchestrators

---

## Apps with Python Packages

If your app includes a `python_package` field in the manifest, the Workshop
will log a warning after deployment with install instructions:

```text
This app includes Python package 'docman'.
Install it manually: pip install -e ~/.heddle/apps/docman/src/
```

The Workshop cannot auto-install packages because it may not have write
access to the Python environment. Install the package manually before
starting workers that depend on it.

---

## App Directory Structure

Deployed apps are extracted to `~/.heddle/apps/{app_name}/`:

```text
~/.heddle/apps/
  baft/
    manifest.yaml
    configs/
      workers/
      orchestrators/
      schedulers/
      mcp/
    scripts/
  docman/
    manifest.yaml
    configs/
    src/docman/
```

---

## Removing Apps

From the Workshop, navigate to the app detail page and click **Remove App**.
Or from the Apps list, click the **Remove** button.

This deletes the app directory and its configs. Running actors are notified
to reload (they will no longer find the removed configs).

---

*For local deployment setup, see [LOCAL_DEPLOYMENT.md](LOCAL_DEPLOYMENT.md).
For Workshop features, see [workshop.md](workshop.md).*
