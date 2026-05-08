"""
mDNS/Bonjour service advertisement for Heddle.

Advertises Heddle services on the local network so that MCP clients,
Workshop browsers, and other tools can auto-discover them without
manual URL configuration.

Requires the ``zeroconf`` package::

    pip install heddle-ai[mdns]

Services advertised:
    - ``_http._tcp.local.`` — Heddle Workshop (web UI)
    - ``_nats._tcp.local.`` — NATS message bus
    - ``_http._tcp.local.`` — Heddle MCP server (streamable-http)

Usage::

    advertiser = HeddleServiceAdvertiser()
    await advertiser.start()
    advertiser.register_workshop(port=8080)
    advertiser.register_nats(port=4222)
    # ... later ...
    await advertiser.stop()
"""

from __future__ import annotations

import socket
from typing import Any

import structlog

logger = structlog.get_logger()


def _detect_lan_ip() -> str:
    """Return a non-loopback IPv4 address advertisable on the LAN.

    ``socket.gethostbyname(socket.gethostname())`` is unreliable: on
    Debian / Ubuntu it returns ``127.0.1.1`` (the conventional
    ``/etc/hosts`` entry for the machine name), so the advertised
    service is unreachable from peers.  Use ``ifaddr`` (already a
    transitive dep via ``zeroconf``) to enumerate adapters and pick a
    non-loopback IPv4.

    Falls back to ``gethostbyname(gethostname())`` if ``ifaddr`` isn't
    importable or finds no usable address — preserves prior behaviour
    on platforms where the legacy path was already correct.
    """
    try:
        import ifaddr  # type: ignore[import-not-found]
    except ImportError:
        return socket.gethostbyname(socket.gethostname())

    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue  # IPv6 entries are tuples; skip
            if ip.ip.startswith("127."):
                continue
            return ip.ip

    # No non-loopback adapter found — fall back to the legacy lookup.
    return socket.gethostbyname(socket.gethostname())


class HeddleServiceAdvertiser:
    """Advertise Heddle services via mDNS/Bonjour on the local network.

    This class wraps the ``zeroconf`` library to provide a simple interface
    for registering and unregistering Heddle services. Services become visible
    to any mDNS-capable client on the LAN (e.g., ``dns-sd -B _http._tcp``
    on macOS, or Avahi on Linux).
    """

    def __init__(self) -> None:
        self._zeroconf: Any = None
        self._infos: list[Any] = []

    async def start(self) -> None:
        """Initialize the Zeroconf instance.

        Raises:
            ImportError: If ``zeroconf`` is not installed.
        """
        from zeroconf import Zeroconf

        self._zeroconf = Zeroconf()
        logger.info("mdns.started")

    def register_workshop(self, port: int = 8080, host: str | None = None) -> None:
        """Register the Workshop web UI as an HTTP service."""
        self._register_service(
            service_type="_http._tcp.local.",
            name="Heddle Workshop._http._tcp.local.",
            port=port,
            host=host,
            properties={"path": "/", "version": "0.4.0"},
        )

    def register_nats(self, port: int = 4222, host: str | None = None) -> None:
        """Register the NATS message bus."""
        self._register_service(
            service_type="_nats._tcp.local.",
            name="Heddle NATS._nats._tcp.local.",
            port=port,
            host=host,
            properties={"version": "0.4.0"},
        )

    def register_mcp(self, port: int = 8000, host: str | None = None) -> None:
        """Register an MCP server (streamable-http transport)."""
        self._register_service(
            service_type="_http._tcp.local.",
            name="Heddle MCP._http._tcp.local.",
            port=port,
            host=host,
            properties={"path": "/mcp", "version": "0.4.0"},
        )

    def _register_service(
        self,
        service_type: str,
        name: str,
        port: int,
        host: str | None = None,
        properties: dict[str, str] | None = None,
    ) -> None:
        """Register a single mDNS service."""
        if self._zeroconf is None:
            logger.warning("mdns.not_started", hint="Call start() before registering services")
            return

        from zeroconf import ServiceInfo

        # Resolve the host address
        host_addr = _detect_lan_ip() if host is None or host in ("0.0.0.0", "") else host

        info = ServiceInfo(
            service_type,
            name,
            addresses=[socket.inet_aton(host_addr)],
            port=port,
            properties=properties or {},
            server=f"{socket.gethostname()}.local.",
        )

        self._zeroconf.register_service(info)
        self._infos.append(info)
        logger.info(
            "mdns.service_registered",
            name=name,
            port=port,
            host=host_addr,
        )

    async def stop(self) -> None:
        """Unregister all services and close Zeroconf."""
        if self._zeroconf is None:
            return

        for info in self._infos:
            self._zeroconf.unregister_service(info)
            logger.info("mdns.service_unregistered", name=info.name)

        self._infos.clear()
        self._zeroconf.close()
        self._zeroconf = None
        logger.info("mdns.stopped")
