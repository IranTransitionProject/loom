"""Tests for SubprocessBackend."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from heddle.contrib.subprocess.backend import (
    SubprocessBackend,
    SubprocessBackendError,
    SubprocessExitError,
    SubprocessOutputParseError,
    SubprocessTimeoutError,
    _parse_stdout,
    _validate_command,
)
from heddle.worker.processor import BackendError

# ── Error hierarchy ──────────────────────────────────────────────────


class TestErrorHierarchy:
    def test_subprocess_error_is_backend_error(self):
        assert issubclass(SubprocessBackendError, BackendError)

    def test_timeout_error_is_subprocess_error(self):
        assert issubclass(SubprocessTimeoutError, SubprocessBackendError)

    def test_exit_error_has_code_and_stderr(self):
        err = SubprocessExitError("fail", exit_code=1, stderr="bad input")
        assert err.exit_code == 1
        assert err.stderr == "bad input"

    def test_parse_error_is_subprocess_error(self):
        assert issubclass(SubprocessOutputParseError, SubprocessBackendError)


# ── Command validation ───────────────────────────────────────────────


class TestValidateCommand:
    def test_rejects_empty_list(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_command([])

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_command("echo")

    def test_rejects_non_string_element(self):
        with pytest.raises(ValueError, match="must be a string"):
            _validate_command(["echo", 42])

    def test_rejects_semicolon(self):
        with pytest.raises(ValueError, match="shell metacharacter"):
            _validate_command(["echo; rm -rf /"])

    def test_rejects_pipe(self):
        with pytest.raises(ValueError, match="shell metacharacter"):
            _validate_command(["cat", "|", "grep"])

    def test_rejects_backtick(self):
        with pytest.raises(ValueError, match="shell metacharacter"):
            _validate_command(["echo", "`whoami`"])

    def test_rejects_dollar_sign(self):
        with pytest.raises(ValueError, match="shell metacharacter"):
            _validate_command(["echo", "$HOME"])

    def test_allowlist_pass(self):
        _validate_command(["node", "worker.js"], allowed_commands=["node", "python3"])

    def test_allowlist_fail(self):
        with pytest.raises(ValueError, match="not in allowed_commands"):
            _validate_command(["ruby", "evil.rb"], allowed_commands=["node"])

    def test_valid_command(self):
        _validate_command(["node", "worker.js", "--port", "8080"])


# ── Constructor ──────────────────────────────────────────────────────


class TestConstructor:
    def test_stores_config(self):
        b = SubprocessBackend(command=["echo", "hi"], io_mode="stdin_stdout")
        assert b._command == ["echo", "hi"]
        assert b._io_mode == "stdin_stdout"
        assert b._model_used == "echo"

    def test_unknown_io_mode_raises(self):
        with pytest.raises(ValueError, match="Unknown io_mode"):
            SubprocessBackend(command=["echo"], io_mode="telepathy")

    def test_model_used_override(self):
        b = SubprocessBackend(command=["node", "x.js"], model_used="my-tool-v2")
        assert b._model_used == "my-tool-v2"

    def test_model_used_defaults_to_command0(self):
        b = SubprocessBackend(command=["python3", "worker.py"])
        assert b._model_used == "python3"

    def test_command_validated_at_init(self):
        with pytest.raises(ValueError, match="shell metacharacter"):
            SubprocessBackend(command=["echo; bad"])


# ── Parse stdout ─────────────────────────────────────────────────────


class TestParseStdout:
    def test_raw(self):
        assert _parse_stdout("  hello\n", "raw") == "  hello\n"

    def test_strip(self):
        assert _parse_stdout("  hello  \n", "strip") == "hello"

    def test_int(self):
        assert _parse_stdout("  42\n", "int") == 42

    def test_float(self):
        assert _parse_stdout(" 3.14 \n", "float") == pytest.approx(3.14)

    def test_lines(self):
        assert _parse_stdout("a\nb\n\nc\n", "lines") == ["a", "b", "c"]

    def test_json(self):
        assert _parse_stdout('{"key": "val"}', "json") == {"key": "val"}

    def test_json_failure(self):
        with pytest.raises(SubprocessOutputParseError, match="Failed to parse"):
            _parse_stdout("not json", "json")

    def test_unknown_mode(self):
        with pytest.raises(SubprocessOutputParseError, match="Unknown parse mode"):
            _parse_stdout("x", "xml")


# ── json_stdio mode ─────────────────────────────────────────────────


def _mock_run(stdout="", stderr="", returncode=0):
    """Create a mock subprocess.CompletedProcess."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


class TestJsonStdio:
    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_sends_full_payload(self, mock_run):
        mock_run.return_value = _mock_run(stdout='{"result": "ok"}')
        b = SubprocessBackend(
            command=["node", "w.js"],
            io_mode="json_stdio",
            output_mapping={"target": "root"},
        )
        result = b.process_sync({"text": "hello", "lang": "en"}, {})
        assert result["output"] == {"result": "ok"}
        assert result["model_used"] == "node"

        call_args = mock_run.call_args
        stdin_sent = json.loads(call_args.kwargs.get("input", call_args[1].get("input", "")))
        assert stdin_sent == {"text": "hello", "lang": "en"}

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_fields_filter(self, mock_run):
        mock_run.return_value = _mock_run(stdout='{"out": "x"}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            input_mapping={"fields": ["text"]},
            output_mapping={"target": "root"},
        )
        b.process_sync({"text": "hi", "secret": "password"}, {})

        call_args = mock_run.call_args
        stdin_sent = json.loads(call_args.kwargs.get("input", call_args[1].get("input", "")))
        assert "text" in stdin_sent
        assert "secret" not in stdin_sent

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_extract_key(self, mock_run):
        mock_run.return_value = _mock_run(stdout='{"data": {"val": 1}, "meta": "x"}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            output_mapping={"target": "root", "extract_key": "data"},
        )
        result = b.process_sync({}, {})
        assert result["output"] == {"val": 1}

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_target_nests_under_key(self, mock_run):
        mock_run.return_value = _mock_run(stdout="[1, 2, 3]")
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            output_mapping={"target": "items"},
        )
        result = b.process_sync({}, {})
        assert result["output"] == {"items": [1, 2, 3]}

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_non_json_stdout_raises(self, mock_run):
        mock_run.return_value = _mock_run(stdout="not json at all")
        b = SubprocessBackend(command=["tool"], io_mode="json_stdio")
        with pytest.raises(SubprocessOutputParseError, match="Failed to parse"):
            b.process_sync({}, {})

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_root_target_requires_dict(self, mock_run):
        mock_run.return_value = _mock_run(stdout="42")
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            output_mapping={"target": "root"},
        )
        with pytest.raises(SubprocessOutputParseError, match="JSON object"):
            b.process_sync({}, {})


# ── stdin_stdout mode ────────────────────────────────────────────────


class TestStdinStdout:
    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_pipes_field_to_stdin(self, mock_run):
        mock_run.return_value = _mock_run(stdout="5")
        b = SubprocessBackend(
            command=["wc", "-w"],
            io_mode="stdin_stdout",
            input_mapping={"stdin_field": "text"},
            output_mapping={"stdout_field": "count", "parse": "int"},
        )
        result = b.process_sync({"text": "one two three four five"}, {})
        assert result["output"] == {"count": 5}

        call_args = mock_run.call_args
        assert call_args.kwargs.get("input", call_args[1].get("input")) == "one two three four five"

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_missing_field_uses_empty_string(self, mock_run):
        mock_run.return_value = _mock_run(stdout="")
        b = SubprocessBackend(
            command=["cat"],
            io_mode="stdin_stdout",
            input_mapping={"stdin_field": "missing"},
        )
        b.process_sync({"other": "val"}, {})
        call_args = mock_run.call_args
        assert call_args.kwargs.get("input", call_args[1].get("input")) == ""

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_parse_lines(self, mock_run):
        mock_run.return_value = _mock_run(stdout="a\nb\nc\n")
        b = SubprocessBackend(
            command=["sort"],
            io_mode="stdin_stdout",
            output_mapping={"stdout_field": "lines", "parse": "lines"},
        )
        result = b.process_sync({"input": "data"}, {})
        assert result["output"] == {"lines": ["a", "b", "c"]}


# ── args_stdout mode ────────────────────────────────────────────────


class TestArgsStdout:
    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_positional_args(self, mock_run):
        mock_run.return_value = _mock_run(stdout="output")
        b = SubprocessBackend(
            command=["tool"],
            io_mode="args_stdout",
            input_mapping={"positional": [{"field": "path"}, {"field": "format"}]},
        )
        b.process_sync({"path": "/tmp/file.txt", "format": "json"}, {})

        called_cmd = mock_run.call_args[0][0]
        assert called_cmd == ["tool", "/tmp/file.txt", "json"]

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_boolean_flag_true(self, mock_run):
        mock_run.return_value = _mock_run(stdout="x")
        b = SubprocessBackend(
            command=["tool"],
            io_mode="args_stdout",
            input_mapping={
                "flags": [{"flag": "--verbose", "field": "verbose", "is_boolean": True}]
            },
        )
        b.process_sync({"verbose": True}, {})
        called_cmd = mock_run.call_args[0][0]
        assert "--verbose" in called_cmd

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_boolean_flag_false(self, mock_run):
        mock_run.return_value = _mock_run(stdout="x")
        b = SubprocessBackend(
            command=["tool"],
            io_mode="args_stdout",
            input_mapping={
                "flags": [{"flag": "--verbose", "field": "verbose", "is_boolean": True}]
            },
        )
        b.process_sync({"verbose": False}, {})
        called_cmd = mock_run.call_args[0][0]
        assert "--verbose" not in called_cmd

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_named_flag_with_value(self, mock_run):
        mock_run.return_value = _mock_run(stdout="x")
        b = SubprocessBackend(
            command=["tool"],
            io_mode="args_stdout",
            input_mapping={"flags": [{"flag": "--format", "field": "fmt"}]},
        )
        b.process_sync({"fmt": "json"}, {})
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd == ["tool", "--format", "json"]


# ── file_file mode ──────────────────────────────────────────────────


class TestFileFile:
    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_writes_and_reads_temp_files(self, mock_run):
        def side_effect(cmd, **kwargs):
            # Find {output} path in command and write to it.
            for arg in cmd:
                if "output" in arg and not arg.startswith("-"):
                    from pathlib import Path

                    Path(arg).write_text("<h1>Hello</h1>")
                    break
            return _mock_run()

        mock_run.side_effect = side_effect
        b = SubprocessBackend(
            command=["pandoc"],
            io_mode="file_file",
            input_mapping={
                "content_field": "md",
                "input_extension": ".md",
                "output_extension": ".html",
                "args_template": ["-o", "{output}", "{input}"],
            },
            output_mapping={"output_field": "html", "read_mode": "text"},
        )
        result = b.process_sync({"md": "# Hello"}, {})
        assert result["output"]["html"] == "<h1>Hello</h1>"

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_binary_read_mode_returns_base64(self, mock_run):
        def side_effect(cmd, **kwargs):
            for arg in cmd:
                if "output" in arg and not arg.startswith("-"):
                    from pathlib import Path

                    Path(arg).write_bytes(b"\x89PNG\r\n")
                    break
            return _mock_run()

        mock_run.side_effect = side_effect
        b = SubprocessBackend(
            command=["convert"],
            io_mode="file_file",
            input_mapping={
                "content_field": "data",
                "output_extension": ".png",
                "args_template": ["{input}", "{output}"],
            },
            output_mapping={"output_field": "image", "read_mode": "binary"},
        )
        result = b.process_sync({"data": "source"}, {})

        import base64

        decoded = base64.b64decode(result["output"]["image"])
        assert decoded == b"\x89PNG\r\n"

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_missing_output_file_raises(self, mock_run):
        mock_run.return_value = _mock_run()
        b = SubprocessBackend(
            command=["tool"],
            io_mode="file_file",
            input_mapping={"args_template": ["{input}"]},
            output_mapping={"output_field": "out"},
        )
        with pytest.raises(SubprocessBackendError, match="not created"):
            b.process_sync({"content": "x"}, {})


# ── Error handling ───────────────────────────────────────────────────


class TestErrorHandling:
    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_timeout_raises_subprocess_timeout_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["slow"], timeout=5)
        b = SubprocessBackend(command=["slow"], io_mode="json_stdio")
        with pytest.raises(SubprocessTimeoutError, match="timed out"):
            b.process_sync({}, {})

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_nonzero_exit_raises_exit_error(self, mock_run):
        mock_run.return_value = _mock_run(returncode=1, stderr="bad input")
        b = SubprocessBackend(command=["tool"], io_mode="stdin_stdout")
        with pytest.raises(SubprocessExitError) as exc_info:
            b.process_sync({"input": "x"}, {})
        assert exc_info.value.exit_code == 1
        assert "bad input" in exc_info.value.stderr

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_stderr_captured_in_exit_error(self, mock_run):
        mock_run.return_value = _mock_run(returncode=2, stderr="detailed error msg")
        b = SubprocessBackend(command=["tool"], io_mode="json_stdio")
        with pytest.raises(SubprocessExitError) as exc_info:
            b.process_sync({}, {})
        assert exc_info.value.stderr == "detailed error msg"


# ── Environment variables ────────────────────────────────────────────


class TestEnvironment:
    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_env_vars_forwarded(self, mock_run):
        mock_run.return_value = _mock_run(stdout='{"ok": true}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            env={"MY_VAR": "hello"},
        )
        b.process_sync({}, {})
        call_env = mock_run.call_args.kwargs.get("env", mock_run.call_args[1].get("env"))
        assert call_env["MY_VAR"] == "hello"

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_payload_interpolation(self, mock_run):
        mock_run.return_value = _mock_run(stdout='{"ok": true}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            env={"LANG": "${payload.language}"},
        )
        b.process_sync({"language": "fr"}, {})
        call_env = mock_run.call_args.kwargs.get("env", mock_run.call_args[1].get("env"))
        assert call_env["LANG"] == "fr"

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_no_env_passes_none(self, mock_run):
        mock_run.return_value = _mock_run(stdout='{"ok": true}')
        b = SubprocessBackend(command=["tool"], io_mode="json_stdio")
        b.process_sync({}, {})
        call_env = mock_run.call_args.kwargs.get("env", mock_run.call_args[1].get("env"))
        assert call_env is None

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_env_template_does_not_leak_operator_env(self, mock_run, monkeypatch):
        """E1: env={...} alone no longer inherits the operator's env.

        Earlier the subprocess started with ``dict(os.environ)`` and
        only then merged the template, so a deployed app's tool saw
        every API key the operator had set.  Now ``env={...}``
        produces only the explicit keys (plus anything the operator
        opts into via ``env_passthrough``).
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("HEDDLE_WORKSHOP_TOKEN", "do-not-leak")
        mock_run.return_value = _mock_run(stdout='{"ok": true}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            env={"MY_VAR": "hello"},
        )
        b.process_sync({}, {})
        call_env = mock_run.call_args.kwargs.get("env", mock_run.call_args[1].get("env"))
        assert call_env == {"MY_VAR": "hello"}
        assert "ANTHROPIC_API_KEY" not in call_env
        assert "HEDDLE_WORKSHOP_TOKEN" not in call_env

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_env_passthrough_explicit_whitelist(self, mock_run, monkeypatch):
        """E1: env_passthrough copies only the listed operator vars."""
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        mock_run.return_value = _mock_run(stdout='{"ok": true}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            env_passthrough=["PATH"],
        )
        b.process_sync({}, {})
        call_env = mock_run.call_args.kwargs.get("env", mock_run.call_args[1].get("env"))
        assert call_env == {"PATH": "/usr/bin:/bin"}
        assert "ANTHROPIC_API_KEY" not in call_env

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_env_passthrough_missing_var_silently_skipped(self, mock_run, monkeypatch):
        """``env_passthrough=["NOT_SET"]`` produces an empty whitelist when unset."""
        monkeypatch.delenv("NOT_SET", raising=False)
        mock_run.return_value = _mock_run(stdout='{"ok": true}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            env={"MY_VAR": "hello"},
            env_passthrough=["NOT_SET"],
        )
        b.process_sync({}, {})
        call_env = mock_run.call_args.kwargs.get("env", mock_run.call_args[1].get("env"))
        assert call_env == {"MY_VAR": "hello"}

    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_env_template_overrides_passthrough(self, mock_run, monkeypatch):
        """``env`` template takes precedence over ``env_passthrough`` for same key."""
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        mock_run.return_value = _mock_run(stdout='{"ok": true}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            env={"LANG": "fr_FR.UTF-8"},
            env_passthrough=["LANG"],
        )
        b.process_sync({}, {})
        call_env = mock_run.call_args.kwargs.get("env", mock_run.call_args[1].get("env"))
        assert call_env["LANG"] == "fr_FR.UTF-8"


# ── Working directory ────────────────────────────────────────────────


class TestWorkingDir:
    @patch("heddle.contrib.subprocess.backend.subprocess.run")
    def test_cwd_passed(self, mock_run):
        mock_run.return_value = _mock_run(stdout='{"ok": true}')
        b = SubprocessBackend(
            command=["tool"],
            io_mode="json_stdio",
            working_dir="/opt/tools",
        )
        b.process_sync({}, {})
        call_cwd = mock_run.call_args.kwargs.get("cwd", mock_run.call_args[1].get("cwd"))
        assert call_cwd == "/opt/tools"
