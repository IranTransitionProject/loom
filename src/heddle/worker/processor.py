"""
Processor worker for non-LLM task processing.

ProcessorWorker delegates to a ProcessingBackend — any Python library,
rules engine, or external tool that isn't an LLM. Examples: Docling for
document extraction, ffmpeg for media, scikit-learn for classification.

This module also provides:

    BackendError
        Base exception for processing backend failures. Backend
        implementations should subclass this (e.g., DoclingConversionError)
        to provide structured errors with the original cause preserved.

    SyncProcessingBackend
        Base class for backends wrapping synchronous, CPU-bound libraries.
        Subclasses implement ``process_sync()`` which is automatically
        offloaded to a thread pool via ``asyncio.run_in_executor``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

import structlog

from heddle.worker.base import TaskWorker

logger = structlog.get_logger()


class BackendError(Exception):
    """Base error for processing backend failures.

    Backend implementations should raise subclasses of this to provide
    structured, domain-specific errors with the original cause preserved
    via ``__cause__``.

    Example::

        class DoclingConversionError(BackendError):
            \"\"\"Raised when Docling fails to convert a document.\"\"\"

        try:
            converter.convert(path)
        except Exception as exc:
            raise DoclingConversionError(f"Failed: {exc}") from exc
    """


class ProcessingBackend(ABC):
    """
    Generic processing backend interface for non-LLM workers.

    Implementations wrap a specific tool or library (Docling, ffmpeg, etc.)
    and translate between Heddle's payload/output dicts and that tool's API.
    """

    @abstractmethod
    async def process(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Process a task payload.

        Args:
            payload: Validated input dict from TaskMessage.
            config: Full worker config dict (for backend-specific settings).

        Returns:
            A dict with the following structure::

                {
                    "output": dict,           # Structured output matching output_schema
                    "model_used": str | None, # Identifier (e.g., "docling-v2", "ffmpeg-6.1")
                }
        """
        ...

    async def aclose(self) -> None:  # noqa: B027 — intentional no-op default
        """Release any I/O resources held by this backend.

        Subclasses that wrap stateful clients (DB connections, HTTP
        clients, dynamically-loaded ChatBridges) override this to close
        them.  Idempotent — safe to call more than once.  The default
        is a no-op.
        """


class SyncProcessingBackend(ProcessingBackend):
    """Base class for backends wrapping synchronous, CPU-bound libraries.

    Subclasses implement ``process_sync()`` instead of ``process()``.
    The synchronous method is automatically offloaded to a thread pool
    via ``asyncio.run_in_executor`` so the async event loop stays
    responsive.

    If ``serialize_writes=True``, an asyncio.Lock ensures only one
    call to ``process_sync`` runs at a time.  Use this for backends
    that write to single-writer stores like DuckDB.

    Use this for backends that wrap libraries like Docling, ffmpeg,
    scikit-learn, or any other tool that performs blocking I/O or
    CPU-intensive computation.

    Example::

        class FFmpegBackend(SyncProcessingBackend):
            def process_sync(self, payload, config):
                # CPU-bound work — runs in thread pool automatically
                subprocess.run(["ffmpeg", ...])
                return {"output": {...}, "model_used": "ffmpeg"}
    """

    def __init__(self, *, serialize_writes: bool = False) -> None:
        self._write_lock: asyncio.Lock | None = asyncio.Lock() if serialize_writes else None

    @abstractmethod
    def process_sync(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Process a task payload synchronously.

        This method runs in a thread pool — do not use ``await`` here.
        Return format is identical to ``ProcessingBackend.process()``.

        Args:
            payload: Validated input dict from TaskMessage.
            config: Full worker config dict (for backend-specific settings).

        Returns:
            ``{"output": dict, "model_used": str | None}``
        """
        ...

    async def process(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Offload process_sync() to a thread pool and return the result.

        If ``serialize_writes`` was set, acquires the write lock first to
        ensure single-writer semantics (e.g., for DuckDB).
        """
        loop = asyncio.get_running_loop()
        if self._write_lock is not None:
            async with self._write_lock:
                return await loop.run_in_executor(None, self.process_sync, payload, config)
        return await loop.run_in_executor(None, self.process_sync, payload, config)


class ProcessorWorker(TaskWorker):
    """
    Non-LLM stateless worker.

    Delegates processing to a ProcessingBackend instead of an LLM.
    Follows the same lifecycle as LLMWorker: validate input, process,
    validate output, publish result.
    """

    def __init__(
        self,
        actor_id: str,
        config_path: str,
        backend: ProcessingBackend,
        nats_url: str = "nats://nats:4222",
    ) -> None:
        super().__init__(actor_id, config_path, nats_url)
        self.backend = backend

    async def disconnect(self) -> None:
        """Disconnect from the bus and close the owned processing backend."""
        try:
            await super().disconnect()
        finally:
            try:
                await self.backend.aclose()
            except Exception as e:
                logger.warning(
                    "processor.backend_close_failed",
                    backend=type(self.backend).__name__,
                    error=str(e),
                )

    async def process(self, payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        """Delegate processing to the backend and return the result."""
        logger.info("processor.processing", backend=type(self.backend).__name__)
        result = await self.backend.process(payload, self.config)
        return {
            "output": result["output"],
            "model_used": result.get("model_used"),
            "token_usage": {},
        }
