"""Thin async context manager over a NATS JetStream connection.

Production callers typically own their NATS client lifecycle and pass
``js = nc.jetstream()`` directly to :class:`JetStreamEventLog` /
:class:`JetStreamRejectionLog`. :func:`connect_jetstream` is a
convenience for tests and small scripts that want to spin a
connection up and down with one call.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import nats

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from nats.aio.client import Client as NATSClient
    from nats.js import JetStreamContext


class JetStreamConnection:
    """Holder for a connected NATS client and its JetStream context.

    Use :func:`connect_jetstream` to construct one, or assemble manually
    from an existing :class:`nats.aio.client.Client`.
    """

    def __init__(self, nc: NATSClient, js: JetStreamContext) -> None:
        self.nc = nc
        self.js = js

    async def aclose(self) -> None:
        """Drain and close the underlying NATS connection."""
        await self.nc.drain()


@asynccontextmanager
async def connect_jetstream(
    servers: str | list[str] = "nats://localhost:4222",
) -> AsyncGenerator[JetStreamConnection, None]:
    """Open a NATS connection, expose its JetStream context, then drain on exit."""
    nc = await nats.connect(servers)  # type: ignore[reportUnknownMemberType]
    try:
        js = nc.jetstream()  # type: ignore[reportUnknownMemberType]
        yield JetStreamConnection(nc=nc, js=js)
    finally:
        await nc.drain()
