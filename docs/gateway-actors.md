# Gateway Actors — Bridging Non-NATS Protocols

**Audience:** operators connecting Heddle to systems that don't speak
NATS natively — IoT devices, HTTP webhooks, MQTT brokers, message
queues, hardware buses, anything else that needs to round-trip into
the framework.

The Heddle wire format is NATS + JSON (see
[Foreign-Language Actors](foreign-actors.md)). For external systems
that can't or shouldn't speak that protocol, the pattern is a
**gateway actor**: a translating bridge that subscribes to the
external protocol on one side and republishes as `TaskMessage`s on the
Heddle bus on the other.

## When to use which pattern

Three distinct patterns cover the common cases:

| Pattern | Use when | Cost |
| --- | --- | --- |
| **NATS-MQTT adapter** (built into NATS Server) | The external system speaks MQTT v3.1.1 and you want zero custom code in the bridge layer. | One NATS server config block. The MQTT client publishes to its native topic; NATS exposes it on a NATS subject; a Heddle actor subscribes there as normal. |
| **Gateway actor** (Python or foreign) | The external system speaks any other protocol (HTTP webhooks, CoAP, Modbus, custom binary, a SaaS REST API). | One actor that owns the translation: external receive → build `TaskMessage` → publish on NATS. Results flow back symmetrically. |
| **Sidecar proxy** | The external system is co-located with the worker (e.g. a USB-connected sensor, a local ML inference daemon, a process on the same host) and the bridge logic is more cohesive with the worker than with the framework. | One process per sidecar. Communicates with the worker over loopback / shared memory / domain socket; the worker is the only thing on the bus. |

Pick the lightest pattern that fits. The gateway-actor pattern is the
fallback when MQTT-adapter or sidecar isn't appropriate.

## Pattern 1: NATS-MQTT adapter

NATS Server has a built-in MQTT v3.1.1 listener. Enabling it on the
NATS deployment that already serves Heddle is the cleanest way to
accept MQTT publishers without writing a bridge:

```nats
# nats-server.conf
port: 4222

mqtt {
    port: 1883          # standard MQTT port
    # auth, tls, etc. as needed
}

jetstream {            # MQTT QoS-1 / retained messages need JetStream
    store_dir: "/var/lib/nats/jetstream"
}
```

Once enabled, an MQTT client publishing to topic `sensors/temp/room1`
lands on NATS subject `sensors.temp.room1` (NATS converts `/` to `.`
automatically). A Heddle actor can subscribe to either the raw
subject (and publish a `TaskMessage` for it) or, more idiomatically,
to a Heddle subject that the MQTT-side device publishes to directly
using the framework's subject conventions.

The pattern's strength: **no custom bridge code.** The pattern's
weakness: the MQTT-side device has to know about Heddle's message
shape and subject naming. That works when you control the firmware;
it doesn't work when integrating a vendor-supplied MQTT publisher
that emits its own schema.

## Pattern 2: Gateway actor (the general case)

A gateway actor is a normal Heddle actor with one extra
responsibility: it owns one side of an external protocol. Inbound:
external event → construct `TaskMessage` → publish to
`heddle.tasks.incoming`. Outbound (if the external system expects a
response): subscribe to `heddle.results.{parent_task_id}` →
translate `TaskResult` back to the external protocol.

### Example: HTTP webhook gateway

A vendor that fires HTTP POSTs at `/webhook/order-created` should
flow into a Heddle pipeline. The gateway:

```python
# src/heddle/contrib/gateways/http_webhook.py  (sketch)
from fastapi import FastAPI, Request

from heddle.core.actor import BaseActor
from heddle.core.messages import OrchestratorGoal


class WebhookGateway(BaseActor):
    """HTTP webhook → Heddle goal."""

    def __init__(self, actor_id: str, *, port: int = 8443, nats_url: str) -> None:
        super().__init__(actor_id, nats_url)
        self._app = FastAPI()
        self._app.add_api_route("/webhook/{event}", self._handle, methods=["POST"])
        self._port = port

    async def _handle(self, event: str, request: Request) -> dict:
        body = await request.json()
        goal = OrchestratorGoal(
            instruction=f"Process external event: {event}",
            context={"webhook_event": event, "payload": body},
        )
        await self.publish("heddle.goals.incoming", goal.model_dump(mode="json"))
        return {"accepted": True, "goal_id": goal.goal_id}
```

The gateway is a normal actor in every other respect: queue-group
subscriptions for redundancy, OTel propagation, structured logs. The
upstream system sees a vanilla HTTP endpoint; downstream Heddle
workers see normal `OrchestratorGoal` / `TaskMessage` flow.

### Example: MQTT gateway with translation

If you need to translate vendor-specific MQTT payloads (rather than
have the device speak Heddle's format), put a gateway actor between
the MQTT broker and `heddle.tasks.incoming`. The gateway subscribes
to vendor topics, parses the vendor schema, and republishes as
`TaskMessage`s the workers actually understand.

This is also how you'd handle authentication boundaries: the gateway
verifies the external system's credentials and rejects bad input
before it touches the Heddle bus.

## Pattern 3: Sidecar proxy

When the external system is **co-located** with one specific worker —
a USB sensor wired into a particular host, a local ML inference daemon
talking over loopback, a process that owns a hardware resource — the
bridge logic doesn't belong on the Heddle bus at all. It belongs
inside the worker process (or alongside it, talking over a Unix
socket / shared memory).

Examples:

- A `coreml_classifier` worker that needs a CoreML inference helper
  running in its own process for sandbox reasons → the helper is a
  sidecar, the worker calls it via XPC / domain socket, only the
  worker appears on the NATS bus.
- A `serial_sensor` worker reading a USB-connected device → the
  serial-port reader can be a thread in the worker or a subprocess
  the worker supervises. Either way, NATS sees one actor.

This pattern keeps the bus surface minimal and avoids the gateway
indirection when the bridge is naturally part of the worker's
responsibility anyway.

## Gateway actors and statelessness

Gateway actors are not subject to the worker-statelessness invariant
in the same shape as `TaskWorker` subclasses. They may legitimately
hold per-connection state (the HTTP server, the MQTT client, the
serial port). What matters is:

- **No state shared between independent requests.** Each external
  event maps to a fresh `TaskMessage` with its own payload. The
  gateway doesn't "remember" what happened to the previous event
  beyond what's needed to keep the external connection alive.
- **Restartable.** Gateway actor restart should not lose more than
  in-flight requests; durable state lives on NATS / in the
  orchestrator / in the external system's retry semantics, not in
  the gateway's memory.

## Where gateway actors live in the repo

By convention:

- **Built-in / general-purpose gateways** → `src/heddle/contrib/gateways/`
  (none today; the first concrete one will set the pattern).
- **Application-specific gateways** → in the application's own
  package or alongside the worker it bridges to.
- **Foreign-language gateways** → no convention yet. A .NET gateway
  for, say, a Windows-only data source would live in its own repo
  and connect over NATS the same way a Python gateway does.

## See also

- [Foreign-Language Actors](foreign-actors.md) — the wire protocol gateways translate to.
- [Workshop](workshop.md) — gateway actors appear in the deployed-actors list like any other.
- [Operator Runbooks](runbooks/index.md) — gateway-specific runbooks
  (e.g. webhook-credential rotation, MQTT broker connectivity) land
  here when concrete gateways ship.
