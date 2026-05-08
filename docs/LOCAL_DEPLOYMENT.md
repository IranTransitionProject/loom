# Local Deployment Guide

**Heddle — Config-driven multi-LLM workflows**

---

## Overview

Heddle can run as a local background service accessible on your machine and
optionally published on your LAN. Three deployment methods are supported:

1. **Docker Compose** — recommended for most users
2. **Native process manager** — macOS (launchd) or Windows (NSSM)
3. **Kubernetes** — see [KUBERNETES.md](KUBERNETES.md)

---

## Docker Compose

The simplest way to run the full Heddle stack locally.

### Prerequisites

- Docker Desktop (Mac/Windows) or Docker Engine (Linux)
- Docker Compose v2+

### Start

```bash
cd heddle/
docker compose up -d
```

This starts:

- **NATS** message bus (port 4222, monitoring on 8222)
- **Valkey** checkpoint store (port 6379)
- **Workshop** web UI (port 8080)
- **Router** deterministic task router

### Access

- Workshop: <http://localhost:8080>
- NATS monitoring: <http://localhost:8222>

### Add Workers

Workers connect to Ollama on the host for local LLM inference.
Uncomment the worker section in `docker-compose.yml` or add:

```yaml
  worker-summarizer:
    build:
      context: .
      dockerfile: docker/Dockerfile.worker
    environment:
      - WORKER_CONFIG=configs/workers/summarizer.yaml
      - MODEL_TIER=local
      - NATS_URL=nats://nats:4222
      - OLLAMA_URL=http://host.docker.internal:11434
      # - HEDDLE_TRACE_CONTENT=1    # Enable prompt/completion logging in OTel spans
    depends_on:
      - nats
      - router
```

> **Tracing:** Set `HEDDLE_TRACE_CONTENT=1` on any worker or orchestrator container
> to record prompt and completion text as OpenTelemetry span events. Disabled by
> default to avoid storing sensitive data in your tracing backend.

### Deploy Apps

Upload app ZIPs through the Workshop at <http://localhost:8080/apps>.
Deployed apps persist across container restarts via the `heddle-apps` volume.

### LAN Access

Bind the Workshop to all interfaces:

```yaml
  workshop:
    ports:
      - "0.0.0.0:8080:8080"
```

Then access from other devices at `http://<your-ip>:8080`.

With mDNS enabled (install `heddle[mdns]`), the Workshop auto-advertises
as `heddle-workshop` on the LAN — discoverable via Bonjour/Avahi.

---

## Native Process Manager — macOS

Run Heddle as launchd background services that start on login.

### Prerequisites

- Python 3.11+ with Heddle installed: `pip install heddle-ai[workshop]`
- NATS server: `brew install nats-server` or Docker

### Install

```bash
# Default (localhost only)
bash deploy/macos/install.sh

# LAN accessible
bash deploy/macos/install.sh 0.0.0.0
```

### Services

| Service | Description | Log |
|---------|-------------|-----|
| com.heddle.workshop | Workshop UI (port 8080) | ~/Library/Logs/heddle/workshop.log |
| com.heddle.router | Task router | ~/Library/Logs/heddle/router.log |

### Manage

```bash
# Check status
launchctl list | grep heddle

# Stop workshop
launchctl stop com.heddle.workshop

# Start workshop
launchctl start com.heddle.workshop

# View logs
tail -f ~/Library/Logs/heddle/workshop.log
```

### Uninstall

```bash
bash deploy/macos/uninstall.sh
```

---

## Native Process Manager — Windows

Run Heddle as Windows services using NSSM.

### Prerequisites

- Python 3.11+ with Heddle installed: `pip install heddle-ai[workshop]`
- NSSM: `choco install nssm`
- NATS server: `choco install nats-server` or Docker

### Install

```powershell
# Default (localhost only)
.\deploy\windows\install.ps1

# LAN accessible
.\deploy\windows\install.ps1 -Host "0.0.0.0"
```

### Services

| Service | Description | Log |
|---------|-------------|-----|
| HeddleWorkshop | Workshop UI (port 8080) | %LOCALAPPDATA%\heddle\logs\workshop.log |
| HeddleRouter | Task router | %LOCALAPPDATA%\heddle\logs\router.log |

### Manage

```powershell
# Check status
nssm status HeddleWorkshop

# Restart
nssm restart HeddleWorkshop
```

### Uninstall

```powershell
.\deploy\windows\uninstall.ps1
```

---

## mDNS / Bonjour Discovery

Install the optional mDNS dependency to auto-advertise Heddle on your LAN:

```bash
pip install heddle-ai[mdns]
```

When the Workshop starts, it automatically registers as a Bonjour service.
Other devices on the network can discover it without knowing the IP address.

For headless deployments (no Workshop), use the standalone advertiser:

```bash
heddle mdns --workshop-port 8080 --nats-port 4222
```

### Discovery from clients

**macOS:**

```bash
dns-sd -B _http._tcp
```

**Linux (Avahi):**

```bash
avahi-browse -r _http._tcp
```

---

*For Kubernetes deployment, see [KUBERNETES.md](KUBERNETES.md).*
*For app bundle deployment, see [APP_DEPLOYMENT.md](APP_DEPLOYMENT.md).*
*For ITP analytical system setup (Baft + Framework), see [baft/docs/SETUP.md](https://github.com/IranTransitionProject/baft/blob/main/docs/SETUP.md).*
