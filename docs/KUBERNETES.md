# Kubernetes Deployment

**Heddle — Config-driven multi-LLM workflows**

---

## Overview

Heddle ships with Kubernetes manifests in `k8s/` that are ready for Minikube.
The manifests deploy NATS, Valkey, the router, an orchestrator, and worker
pods into a dedicated `heddle` namespace.

---

## Minikube Deployment

### Start Minikube

```bash
minikube start --cpus=4 --memory=8192 --driver=docker
eval $(minikube docker-env)
```

### Build Container Images

Build images inside Minikube's Docker daemon so they're available to pods
without a registry:

```bash
docker build -f docker/Dockerfile.worker -t heddle-worker:latest .
docker build -f docker/Dockerfile.router -t heddle-router:latest .
docker build -f docker/Dockerfile.orchestrator -t heddle-orchestrator:latest .
docker build -f docker/Dockerfile.workshop -t heddle-workshop:latest .
```

### Create Namespace and Secrets

```bash
kubectl create namespace heddle
kubectl create secret generic heddle-secrets \
  --namespace heddle \
  --from-literal=anthropic-api-key="$ANTHROPIC_API_KEY"
```

### Deploy

```bash
kubectl apply -k k8s/
kubectl get pods -n heddle -w
```

### Access Workshop

The Workshop is exposed via NodePort on port 30080:

```bash
# Minikube
minikube service heddle-workshop -n heddle

# Or access directly
open http://$(minikube ip):30080
```

---

## Manifest Structure

```text
k8s/
├── namespace.yaml              # heddle namespace
├── nats-deployment.yaml        # NATS server
├── redis-deployment.yaml       # Valkey server
├── router-deployment.yaml      # Heddle router
├── orchestrator-deployment.yaml # Heddle orchestrator
├── worker-deployment.yaml      # Heddle worker(s)
├── workshop-deployment.yaml    # Heddle Workshop web UI (NodePort 30080)
└── kustomization.yaml          # Kustomize overlay
```

---

## Local LLM runtimes on Mac with Minikube

For local LLM inference, run LM Studio or Ollama natively on the host
and point workers at the host address:

```bash
# Option A: LM Studio (start the local server in the LM Studio UI)
LM_STUDIO_URL=http://host.minikube.internal:1234/v1
LM_STUDIO_MODEL=google/gemma-3-4b   # any id from /v1/models

# Option B: Ollama
ollama serve &
OLLAMA_URL=http://host.minikube.internal:11434
```

When both are configured, set
`HEDDLE_LOCAL_BACKEND=lmstudio` (or `ollama`) on the worker pods to
choose which one serves the local tier.

---

## Environment Variables

Workers, router, and orchestrator containers use the following environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `WORKER_CONFIG` | Workers | Path to worker YAML config |
| `MODEL_TIER` | Workers | Model tier (local, standard, frontier) |
| `NATS_URL` | All | NATS server URL |
| `LM_STUDIO_URL` | Optional | LM Studio `/v1/` endpoint |
| `LM_STUDIO_MODEL` | Optional | LM Studio model id |
| `OLLAMA_URL` | Optional | Ollama API endpoint |
| `OLLAMA_MODEL` | Optional | Ollama model name |
| `HEDDLE_LOCAL_BACKEND` | Optional | `lmstudio` or `ollama` (when both URLs are set) |
| `ANTHROPIC_API_KEY` | Optional | Anthropic API key (from secret) |
| `FRONTIER_MODEL` | Optional | Model name for frontier tier |

---

## Resource Requests and Limits

Configure resource requests and limits for each component type:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Router | 100m | 500m | 128Mi | 256Mi |
| Orchestrator | 200m | 1000m | 256Mi | 512Mi |
| Worker (local) | 200m | 1000m | 256Mi | 512Mi |
| Worker (standard) | 100m | 500m | 128Mi | 256Mi |
| NATS | 100m | 500m | 128Mi | 256Mi |
| Valkey | 100m | 500m | 128Mi | 256Mi |

Workers configured for the local tier (LM Studio or Ollama) generally
need more resources because they proxy LLM calls. Workers using remote
APIs (Anthropic) are lighter.

Example in a deployment spec:

```yaml
resources:
  requests:
    cpu: "200m"
    memory: "256Mi"
  limits:
    cpu: "1000m"
    memory: "512Mi"
```

---

## Health Checks

Heddle actors are long-running async processes. Use liveness and readiness
probes to detect stuck or unresponsive actors.

**Important — the probes below are placeholders.** Heddle does not yet
ship a built-in `/healthz` endpoint or standalone healthcheck CLI. The
commands shown exit 0 as long as the Python interpreter starts; they
confirm the container is alive but say nothing about whether the actor
is connected to NATS or processing messages. Treat them as scaffolding,
not as production-grade probes.

Operators running Heddle in production should replace them with one of:

- A sidecar exporter that publishes actor state to a `/healthz`
  endpoint and uses `httpGet`-style probes.
- A TCP probe against the actor's NATS server port from inside the
  pod (catches a NATS outage; does not catch a hung actor).
- A custom exec probe that reads a file the actor touches on each
  message-loop iteration (catches a hung actor; needs the actor to
  cooperate by touching the file).

A built-in `/healthz` endpoint is on the roadmap; until then, the
manifests use the placeholders below, **labelled as such** so the next
operator does not mistake them for a real check:

```yaml
# PLACEHOLDER: only checks the Python interpreter starts, not that
# the actor has a live NATS subscription.  Replace before production
# rollout — see Health Checks section above for guidance.
livenessProbe:
  exec:
    command: ["python", "-c", "import sys; sys.exit(0)"]
  initialDelaySeconds: 10
  periodSeconds: 30
  failureThreshold: 3
# PLACEHOLDER: same caveat.  A real readiness probe should confirm
# the actor's subscribe() has completed, not just that the process
# is up — otherwise the service routes traffic to a not-yet-ready
# actor and the first messages are dropped (NATS is at-most-once,
# see Design Invariant 17).
readinessProbe:
  exec:
    command: ["python", "-c", "import sys; sys.exit(0)"]
  initialDelaySeconds: 5
  periodSeconds: 10
```

For NATS connectivity at startup (rather than at probe time), the CLI
runs the same pre-flight check internally before every actor command;
see `docs/runbooks/verify-nats-connectivity.md` for the operator
workflow.

---

## Horizontal Scaling

Heddle actors scale horizontally via NATS queue groups with zero code changes.
Multiple replicas of the same actor type automatically load-balance.

```bash
# Scale workers manually
kubectl scale deployment/heddle-worker --replicas=5 -n heddle
```

### HPA Auto-Scaling

Use Horizontal Pod Autoscaler for CPU-based scaling:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: heddle-worker-hpa
  namespace: heddle
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: heddle-worker
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

Pipeline orchestrators also support concurrent goal processing via
`max_concurrent_goals` in config, which can complement horizontal scaling.

---

## Persistent Volumes

Valkey requires persistent storage for checkpoint data:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: redis-data
  namespace: heddle
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
```

Mount the PVC in the Valkey deployment's pod spec:

```yaml
volumes:
  - name: redis-data
    persistentVolumeClaim:
      claimName: redis-data
containers:
  - name: redis
    volumeMounts:
      - name: redis-data
        mountPath: /data
```

---

*For local development setup, see [Getting Started](GETTING_STARTED.md).
For architecture details, see [Architecture](ARCHITECTURE.md).*
