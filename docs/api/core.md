# Core

The `heddle.core` package contains the foundational abstractions that all Heddle
components build on: actors, messages, configuration, I/O contracts, app
manifests, and workspace file management.

You rarely use these directly — they're used by workers, orchestrators, and the
CLI. See [Building Workflows](../building-workflows.md) for the user-facing guide.

## Actor

The base class for all NATS-connected actors (workers, routers, orchestrators).

::: heddle.core.actor

## Messages

Typed Pydantic models for all inter-actor communication: `TaskMessage`,
`TaskResult`, `OrchestratorGoal`, `CheckpointState`, `ModelTier`, `TaskStatus`.

::: heddle.core.messages

## Configuration

Config loading, validation, and schema-ref resolution. Validates worker,
pipeline, orchestrator, and router YAML configs.

::: heddle.core.config

## Contracts

Input/output contract validation. Ensures messages match their declared JSON
Schema, with correct bool/int distinction.

::: heddle.core.contracts

## Manifest

App bundle manifest model (`AppManifest`). Used by the Workshop's app deployment
system for ZIP bundle validation.

::: heddle.core.manifest

## Workspace

File-ref resolution with path traversal protection. Maps `file_ref` fields in
task payloads to actual file contents.

::: heddle.core.workspace
