"""Typed worker base — adds Pydantic-typed payload / output to TaskWorker.

The default :class:`heddle.worker.base.TaskWorker` exposes
``payload: dict[str, Any]`` / ``output: dict[str, Any]`` to subclasses
because the worker config carries arbitrary JSON Schema and Heddle
doesn't bind concrete Python types at the actor layer.  That's fine for
the framework, but it shifts the type-safety burden onto every worker:
each one re-unpacks the dict, hand-rolls field access, and gets no
pyright benefit on its own domain types.

``TypedTaskWorker`` closes that gap for workers that want it.  The
subclass declares concrete Pydantic models for its payload and output:

    class WhoisPayload(BaseModel):
        domain: str
        timeout_seconds: int = 5

    class WhoisOutput(BaseModel):
        registrar: str | None
        created: datetime | None

    class WhoisWorker(TypedTaskWorker[WhoisPayload, WhoisOutput]):
        payload_model = WhoisPayload
        output_model = WhoisOutput

        async def handle(self, payload: WhoisPayload, metadata: dict[str, Any]) -> WhoisOutput:
            ...  # ``payload`` is fully typed; pyright-strict sees the fields.

The framework's existing JSON-Schema validation (``validate_input`` /
``validate_output``) still runs against the worker config — the typed
models add a Python-level layer on top, they don't replace the wire
contract.  This is intentional: the YAML config is the source of truth
for what's accepted on the bus; the Pydantic models are a more precise
local view for the worker's own code.

Subclasses opt in; the existing untyped ``TaskWorker`` shape is
unchanged and remains the default.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from heddle.worker.base import TaskWorker

PayloadT = TypeVar("PayloadT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class TypedTaskWorker(TaskWorker, Generic[PayloadT, OutputT]):
    """Stateless worker with Pydantic-typed payload / output.

    Subclasses set :attr:`payload_model` and :attr:`output_model` as
    class attributes (or override the defaults at ``__init__`` time)
    and implement :meth:`handle`.  The base class's :meth:`process`
    parses the dict payload into the typed model, calls ``handle``,
    and dumps the typed output back to a dict for publishing.

    Pydantic validation errors on the way in or out surface as
    standard ``Exception`` instances and route through TaskWorker's
    existing FAILED-result path; they do not bypass the framework's
    JSON-Schema validation, which runs first.
    """

    # ``ClassVar`` because these are per-subclass declarations, not
    # per-instance state.  Subclasses MUST set both.  A subclass that
    # forgets surfaces as ``AttributeError`` at first ``process`` call
    # rather than a quieter failure deep in the dump path.
    payload_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    async def process(self, payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        """Parse, hand off to :meth:`handle`, dump back to a dict.

        This implementation is intentionally tiny — every line of it
        is a load-bearing step in the typed round-trip and is covered
        by ``test_typed_worker``.
        """
        typed_payload: PayloadT = self.payload_model.model_validate(payload)  # type: ignore[assignment]
        typed_output: OutputT = await self.handle(typed_payload, metadata)
        return {
            "output": typed_output.model_dump(mode="json"),
            "model_used": None,
            "token_usage": None,
            "metadata": None,
        }

    @abstractmethod
    async def handle(self, payload: PayloadT, metadata: dict[str, Any]) -> OutputT:
        """The typed worker entry point.  Subclasses implement this.

        Args:
            payload: Pydantic instance of ``payload_model``, already
                validated.
            metadata: TaskMessage metadata dict, unchanged from the
                framework's contract.

        Returns:
            A Pydantic instance of ``output_model``.  The framework
            dumps it back to a dict via ``model_dump(mode="json")``
            before publishing the TaskResult, so any custom serialisers
            on the output model are respected.
        """
        ...
