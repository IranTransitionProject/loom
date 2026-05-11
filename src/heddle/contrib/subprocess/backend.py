"""Subprocess processing backend — wrap CLI tools as Heddle workers.

Any program that reads input and produces output can become a Heddle
worker through this backend.  Four I/O modes cover the common patterns:

- ``json_stdio`` — JSON in via stdin, JSON out from stdout (polyglot path)
- ``stdin_stdout`` — pipe a payload field to stdin, capture stdout
- ``args_stdout`` — payload fields become CLI args, capture stdout
- ``file_file`` — write temp input file, run tool, read output file

All subprocess calls use ``shell=False`` exclusively.

Example worker config::

    name: "my_tool"
    worker_kind: "processor"
    processing_backend: "heddle.contrib.subprocess.SubprocessBackend"
    backend_config:
      command: ["node", "worker.js"]
      io_mode: "json_stdio"
      subprocess_timeout: 15

See Also:
    heddle.worker.processor.SyncProcessingBackend — base class
    heddle.worker.processor.BackendError — error base class
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import structlog

from heddle.worker.processor import BackendError, SyncProcessingBackend

logger = structlog.get_logger()

# ── Error hierarchy ──────────────────────────────────────────────────

_SHELL_METACHARS = re.compile(r"[;|&$`\\!#~<>{}()]")

_VALID_IO_MODES = frozenset({"json_stdio", "stdin_stdout", "args_stdout", "file_file"})


class SubprocessBackendError(BackendError):
    """Base error for subprocess backend failures."""


class SubprocessTimeoutError(SubprocessBackendError):
    """Raised when the subprocess exceeds its timeout."""


class SubprocessExitError(SubprocessBackendError):
    """Raised when the subprocess exits with a non-zero code."""

    def __init__(self, message: str, *, exit_code: int, stderr: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class SubprocessOutputParseError(SubprocessBackendError):
    """Raised when stdout cannot be parsed into the expected format."""


# ── Command validation ───────────────────────────────────────────────


def _validate_command(
    command: list[str],
    allowed_commands: list[str] | None = None,
) -> None:
    """Validate command safety.  Raises ``ValueError`` on violations."""
    if not command or not isinstance(command, list):
        msg = "command must be a non-empty list of strings"
        raise ValueError(msg)

    for i, arg in enumerate(command):
        if not isinstance(arg, str):
            msg = f"command[{i}] must be a string, got {type(arg).__name__}"
            raise ValueError(msg)
        if _SHELL_METACHARS.search(arg):
            msg = (
                f"command[{i}] contains shell metacharacter: {arg!r}. "
                "SubprocessBackend does not use shell=True."
            )
            raise ValueError(msg)

    if allowed_commands is not None and command[0] not in allowed_commands:
        msg = f"Command {command[0]!r} not in allowed_commands: {allowed_commands}"
        raise ValueError(msg)


# ── Stdout parsing ───────────────────────────────────────────────────


def _parse_stdout(stdout: str, mode: str) -> Any:
    """Parse subprocess stdout according to the configured parse mode."""
    if mode == "raw":
        return stdout
    if mode == "strip":
        return stdout.strip()
    if mode == "int":
        return int(stdout.strip())
    if mode == "float":
        return float(stdout.strip())
    if mode == "lines":
        return [line for line in stdout.strip().splitlines() if line]
    if mode == "json":
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            msg = f"Failed to parse stdout as JSON: {exc}. First 200 chars: {stdout[:200]}"
            raise SubprocessOutputParseError(msg) from exc
    msg = f"Unknown parse mode: {mode!r}"
    raise SubprocessOutputParseError(msg)


# ── I/O mode executors ───────────────────────────────────────────────


def _check_exit(
    result: subprocess.CompletedProcess[str],
    command: list[str],
) -> None:
    """Raise :class:`SubprocessExitError` on non-zero exit."""
    if result.returncode != 0:
        msg = (
            f"Command {command[0]!r} exited with code {result.returncode}"
            f": {result.stderr[:500] if result.stderr else '(no stderr)'}"
        )
        raise SubprocessExitError(
            msg,
            exit_code=result.returncode,
            stderr=result.stderr or "",
        )


def _execute_json_stdio(
    command: list[str],
    payload: dict[str, Any],
    input_mapping: dict[str, Any],
    output_mapping: dict[str, Any],
    timeout: float,
    env: dict[str, str] | None,
    cwd: str | None,
) -> dict[str, Any]:
    """JSON in via stdin, JSON out from stdout."""
    fields = input_mapping.get("fields", "all")
    if fields == "all":
        stdin_data = json.dumps(payload, ensure_ascii=False)
    else:
        stdin_data = json.dumps(
            {k: payload[k] for k in fields if k in payload},
            ensure_ascii=False,
        )

    result = subprocess.run(
        command,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
        shell=False,
        check=False,
    )
    _check_exit(result, command)

    try:
        output_json = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse stdout as JSON: {exc}. First 200 chars: {result.stdout[:200]}"
        raise SubprocessOutputParseError(msg) from exc

    extract_key = output_mapping.get("extract_key")
    if extract_key:
        if not isinstance(output_json, dict) or extract_key not in output_json:
            msg = f"extract_key {extract_key!r} not found in stdout JSON"
            raise SubprocessOutputParseError(msg)
        output_json = output_json[extract_key]

    target = output_mapping.get("target", "root")
    if target == "root":
        if not isinstance(output_json, dict):
            msg = "json_stdio with target='root' requires stdout to be a JSON object"
            raise SubprocessOutputParseError(msg)
        return output_json
    return {target: output_json}


def _execute_stdin_stdout(
    command: list[str],
    payload: dict[str, Any],
    input_mapping: dict[str, Any],
    output_mapping: dict[str, Any],
    timeout: float,
    env: dict[str, str] | None,
    cwd: str | None,
) -> dict[str, Any]:
    """Pipe one payload field to stdin, capture stdout."""
    stdin_field = input_mapping.get("stdin_field", "input")
    stdin_data = str(payload.get(stdin_field, ""))

    result = subprocess.run(
        command,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
        shell=False,
        check=False,
    )
    _check_exit(result, command)

    stdout_field = output_mapping.get("stdout_field", "output")
    parse = output_mapping.get("parse", "raw")
    return {stdout_field: _parse_stdout(result.stdout, parse)}


def _execute_args_stdout(
    command: list[str],
    payload: dict[str, Any],
    input_mapping: dict[str, Any],
    output_mapping: dict[str, Any],
    timeout: float,
    env: dict[str, str] | None,
    cwd: str | None,
) -> dict[str, Any]:
    """Payload fields become CLI args, capture stdout."""
    extra_args: list[str] = []

    for pos in input_mapping.get("positional", []):
        field = pos["field"]
        if field in payload:
            extra_args.append(str(payload[field]))

    for flag_def in input_mapping.get("flags", []):
        field = flag_def["field"]
        flag = flag_def["flag"]
        is_boolean = flag_def.get("is_boolean", False)

        if field in payload:
            value = payload[field]
            if is_boolean:
                if value:
                    extra_args.append(flag)
            else:
                extra_args.extend([flag, str(value)])

    full_command = [*command, *extra_args]

    result = subprocess.run(
        full_command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
        shell=False,
        check=False,
    )
    _check_exit(result, full_command)

    stdout_field = output_mapping.get("stdout_field", "output")
    parse = output_mapping.get("parse", "raw")
    return {stdout_field: _parse_stdout(result.stdout, parse)}


def _execute_file_file(
    command: list[str],
    payload: dict[str, Any],
    input_mapping: dict[str, Any],
    output_mapping: dict[str, Any],
    timeout: float,
    env: dict[str, str] | None,
    cwd: str | None,
) -> dict[str, Any]:
    """Write temp input file, run tool, read temp output file."""
    content_field = input_mapping.get("content_field", "content")
    input_ext = input_mapping.get("input_extension", ".tmp")
    output_ext = input_mapping.get("output_extension", ".out")
    args_template = input_mapping.get("args_template", ["{input}"])

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / f"input{input_ext}"
        output_path = Path(tmpdir) / f"output{output_ext}"

        content = payload.get(content_field, "")
        if isinstance(content, bytes):
            input_path.write_bytes(content)
        else:
            input_path.write_text(str(content))

        final_args = [
            arg.replace("{input}", str(input_path)).replace("{output}", str(output_path))
            for arg in args_template
        ]
        full_command = [*command, *final_args]

        result = subprocess.run(
            full_command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            shell=False,
            check=False,
        )
        _check_exit(result, full_command)

        if not output_path.exists():
            msg = f"Expected output file {output_path} was not created by {command[0]!r}"
            raise SubprocessBackendError(msg)

        output_field = output_mapping.get("output_field", "content")
        read_mode = output_mapping.get("read_mode", "text")

        if read_mode == "text":
            output_content: str | Any = output_path.read_text()
        elif read_mode == "binary":
            output_content = base64.b64encode(output_path.read_bytes()).decode("ascii")
        else:
            msg = f"Unknown read_mode: {read_mode!r}"
            raise SubprocessOutputParseError(msg)

        return {output_field: output_content}


_IO_EXECUTORS: dict[str, Any] = {
    "json_stdio": _execute_json_stdio,
    "stdin_stdout": _execute_stdin_stdout,
    "args_stdout": _execute_args_stdout,
    "file_file": _execute_file_file,
}


# ── SubprocessBackend ────────────────────────────────────────────────


class SubprocessBackend(SyncProcessingBackend):
    """Wrap a CLI tool as a Heddle processing backend.

    The subprocess is invoked via :func:`subprocess.run` with
    ``shell=False``.  Four I/O modes handle common CLI patterns;
    see the module docstring for details.

    Constructor parameters come from ``backend_config`` in the worker
    YAML and are passed as keyword arguments by the CLI loader.

    Args:
        command: Base command as a list (e.g., ``["node", "worker.js"]``).
        io_mode: I/O strategy — ``json_stdio``, ``stdin_stdout``,
            ``args_stdout``, or ``file_file``.
        subprocess_timeout: Seconds before the subprocess is killed.
        input_mapping: Mode-specific input configuration.
        output_mapping: Mode-specific output configuration.
        env: Environment variables (merged with the operator's
            inherited ``env_passthrough`` whitelist).  Values may use
            ``${payload.field}`` for runtime interpolation.
        env_passthrough: Explicit list of operator-process env var
            names the subprocess should inherit.  Empty by default —
            the subprocess starts with a minimal environment built
            from this list plus ``env``.  Earlier the subprocess
            inherited the *entire* operator environment, which meant
            every API key (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``,
            ``HEDDLE_WORKSHOP_TOKEN``, ``TELEGRAM_API_HASH``) reached
            every subprocess regardless of what the deployed app
            actually needed.
        working_dir: Working directory for the subprocess.
        allowed_commands: If set, ``command[0]`` must be in this list.
        model_used: Value for :attr:`TaskResult.model_used`.
            Defaults to ``command[0]``.
    """

    def __init__(
        self,
        command: list[str],
        io_mode: str = "json_stdio",
        subprocess_timeout: float = 30.0,
        input_mapping: dict[str, Any] | None = None,
        output_mapping: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        env_passthrough: list[str] | None = None,
        working_dir: str | None = None,
        allowed_commands: list[str] | None = None,
        model_used: str | None = None,
    ) -> None:
        super().__init__(serialize_writes=False)

        _validate_command(command, allowed_commands)

        if io_mode not in _VALID_IO_MODES:
            msg = f"Unknown io_mode: {io_mode!r}. Valid: {sorted(_VALID_IO_MODES)}"
            raise ValueError(msg)

        self._command = list(command)
        self._io_mode = io_mode
        self._timeout = subprocess_timeout
        self._input_mapping: dict[str, Any] = input_mapping or {}
        self._output_mapping: dict[str, Any] = output_mapping or {}
        self._env_template = env
        self._env_passthrough: list[str] = list(env_passthrough or [])
        self._cwd = working_dir
        self._model_used = model_used or command[0]

    def _resolve_env(self, payload: dict[str, Any]) -> dict[str, str] | None:
        """Build environment dict for the subprocess.

        Composition (lowest precedence first):

        1. Empty dict by default — the subprocess does NOT inherit
           the operator's environment.
        2. Names listed in ``env_passthrough`` are copied from
           ``os.environ`` (silently skipped if unset).
        3. ``env`` template entries are applied last, with
           ``${payload.field}`` references resolved against the
           current task payload.

        Returns ``None`` only when both ``env`` and
        ``env_passthrough`` are empty, in which case
        ``subprocess.run`` uses the *parent's* environment — the
        original "inherit everything" behaviour, preserved for the
        backwards-compatible case where the operator hasn't opted
        into either knob.  Apps that set ``env_passthrough=[]``
        explicitly (or ``env={...}`` only) get the safer
        whitelist semantics.
        """
        if self._env_template is None and not self._env_passthrough:
            return None

        resolved: dict[str, str] = {}
        for name in self._env_passthrough:
            value = os.environ.get(name)
            if value is not None:
                resolved[name] = value

        if self._env_template is not None:
            for key, value in self._env_template.items():
                if "${payload." in value:
                    # Extract field name from ${payload.field_name}
                    start = value.index("${payload.") + len("${payload.")
                    end = value.index("}", start)
                    field = value[start:end]
                    resolved[key] = str(payload.get(field, ""))
                else:
                    resolved[key] = value
        return resolved

    def process_sync(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute the subprocess and return the result.

        Dispatches to the configured I/O mode executor, handles
        timeouts and errors, and wraps the output.
        """
        env = self._resolve_env(payload)
        executor = _IO_EXECUTORS[self._io_mode]

        try:
            output = executor(
                self._command,
                payload,
                self._input_mapping,
                self._output_mapping,
                self._timeout,
                env,
                self._cwd,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"Subprocess {self._command[0]!r} timed out after {self._timeout}s"
            raise SubprocessTimeoutError(msg) from exc
        except SubprocessBackendError:
            raise
        except Exception as exc:
            raise SubprocessBackendError(str(exc)) from exc

        return {"output": output, "model_used": self._model_used}
