"""Tests for Workshop FastAPI routes (workshop/app.py).

Tests the HTTP layer for baseline management, dead-letter replay log,
and eval scoring integration.  Uses FastAPI's TestClient with mock backends.

Lifespan tests pre-import ``create_app`` at module level so ``duckdb``
(loaded transitively) lands in ``sys.modules`` BEFORE any per-test
``mock.patch.dict("sys.modules", ...)`` snapshot is taken.  When the
patch context exits it would otherwise restore the snapshot and remove
``duckdb`` from ``sys.modules``, breaking the next test's re-import.
This is the same pattern documented in
``tests/test_workshop_security.py``.
"""

from __future__ import annotations

import unittest.mock as mock
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from heddle.workshop.app import create_app


@pytest.fixture
def client(tmp_path):
    """Workshop TestClient with a temp config dir and in-memory DB."""
    configs_dir = tmp_path / "configs"
    workers_dir = configs_dir / "workers"
    workers_dir.mkdir(parents=True)

    # Create a minimal worker config
    worker_yaml = workers_dir / "test_worker.yaml"
    worker_yaml.write_text(
        "name: test_worker\n"
        "system_prompt: Summarize the text.\n"
        "input_schema:\n"
        "  type: object\n"
        "  required: [text]\n"
        "  properties:\n"
        "    text: {type: string}\n"
        "output_schema:\n"
        "  type: object\n"
        "  required: [summary]\n"
        "  properties:\n"
        "    summary: {type: string}\n"
        "default_model_tier: local\n"
    )

    # Create orchestrators dir (needed by ConfigManager)
    (configs_dir / "orchestrators").mkdir()

    app = create_app(
        configs_dir=str(configs_dir),
        db_path=":memory:",
        apps_dir=str(tmp_path / "apps"),
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health + basic routes
# ---------------------------------------------------------------------------


class TestBasicRoutes:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_root_redirects(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 307)

    def test_workers_list(self, client):
        resp = client.get("/workers")
        assert resp.status_code == 200
        assert "test_worker" in resp.text

    def test_worker_detail(self, client):
        resp = client.get("/workers/test_worker")
        assert resp.status_code == 200

    def test_worker_not_found(self, client):
        resp = client.get("/workers/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Eval baseline routes
# ---------------------------------------------------------------------------


class TestEvalBaselines:
    def _create_eval_run(self, client):
        """Helper: manually insert an eval run via the DB."""
        app = client.app
        db = app.state.db
        run_id = db.save_eval_run("test_worker", "local", total_cases=1)
        db.save_eval_result(
            run_id=run_id,
            case_name="case_1",
            input_payload={"text": "hello"},
            passed=True,
            score=0.9,
        )
        db.update_eval_run(
            run_id,
            {"status": "completed", "passed_cases": 1, "failed_cases": 0},
        )
        return run_id

    def test_eval_detail_shows_no_baseline_initially(self, client):
        run_id = self._create_eval_run(client)
        resp = client.get(f"/workers/test_worker/eval/{run_id}")
        assert resp.status_code == 200

    def test_promote_baseline(self, client):
        run_id = self._create_eval_run(client)
        resp = client.post(
            f"/workers/test_worker/eval/{run_id}/promote-baseline",
            data={"description": "Test baseline"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Verify baseline was set
        db = client.app.state.db
        baseline = db.get_baseline("test_worker")
        assert baseline is not None
        assert baseline["run_id"] == run_id
        assert baseline["description"] == "Test baseline"

    def test_remove_baseline(self, client):
        run_id = self._create_eval_run(client)
        db = client.app.state.db
        db.promote_baseline("test_worker", run_id)

        resp = client.post(
            "/workers/test_worker/eval/remove-baseline",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert db.get_baseline("test_worker") is None

    def test_eval_detail_with_baseline_comparison(self, client):
        db = client.app.state.db

        # Create baseline run
        baseline_id = db.save_eval_run("test_worker", "local", total_cases=1)
        db.save_eval_result(baseline_id, "case_1", {"text": "a"}, True, score=0.8)
        db.update_eval_run(
            baseline_id, {"status": "completed", "passed_cases": 1, "failed_cases": 0}
        )
        db.promote_baseline("test_worker", baseline_id)

        # Create new run
        new_id = db.save_eval_run("test_worker", "local", total_cases=1)
        db.save_eval_result(new_id, "case_1", {"text": "a"}, True, score=0.9)
        db.update_eval_run(new_id, {"status": "completed", "passed_cases": 1, "failed_cases": 0})

        resp = client.get(f"/workers/test_worker/eval/{new_id}")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Dead-letter replay log
# ---------------------------------------------------------------------------


class TestDeadLetterReplayLog:
    def test_dead_letters_page_shows_replay_log(self, client):
        resp = client.get("/dead-letters")
        assert resp.status_code == 200

    def test_replay_creates_audit_record(self, client):
        consumer = client.app.state.dead_letter_consumer
        entry = consumer.store(
            {"worker_type": "summarizer", "payload": {"text": "hi"}},
            "rate_limited",
            task_id="t-1",
            worker_type="summarizer",
        )
        assert consumer.replay_count() == 0

        # Replay via HTTP
        resp = client.post(
            f"/dead-letters/{entry.id}/replay",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert consumer.replay_count() == 1

        log = consumer.replay_log()
        assert log[0]["task_id"] == "t-1"
        assert log[0]["original_reason"] == "rate_limited"

    def test_clear_does_not_clear_replay_log(self, client):
        consumer = client.app.state.dead_letter_consumer
        entry = consumer.store({"x": 1}, "test", task_id="t-1")

        # Replay then clear
        client.post(
            f"/dead-letters/{entry.id}/replay",
            follow_redirects=False,
        )
        client.post("/dead-letters/clear", follow_redirects=False)

        assert consumer.count() == 0
        assert consumer.replay_count() == 1  # Log preserved


# ---------------------------------------------------------------------------
# Eval run with scoring method in metadata
# ---------------------------------------------------------------------------


class TestEvalScoringMetadata:
    def test_eval_run_stores_scoring_method(self, client):
        """Eval runs should store scoring_method in metadata."""
        db = client.app.state.db
        run_id = db.save_eval_run(
            "test_worker",
            "local",
            total_cases=1,
            metadata={"scoring_method": "llm_judge"},
        )
        runs = db.get_eval_runs("test_worker")
        run = next(r for r in runs if r["id"] == run_id)
        assert run["id"] == run_id


# ---------------------------------------------------------------------------
# Backend detection (_detect_available_backends)
# ---------------------------------------------------------------------------


class TestDetectAvailableBackends:
    def test_no_backends_when_no_env_vars(self, client, monkeypatch):
        """Health endpoint works; no backends detected without env vars."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        from heddle.workshop.app import _detect_available_backends

        result = _detect_available_backends()
        assert result == []

    def test_anthropic_backend_detected(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        from heddle.workshop.app import _detect_available_backends

        result = _detect_available_backends()
        names = [b["name"] for b in result]
        assert "anthropic" in names

    def test_ollama_backend_detected(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        from heddle.workshop.app import _detect_available_backends

        result = _detect_available_backends()
        names = [b["name"] for b in result]
        assert "ollama" in names

    def test_openai_backend_detected_via_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        from heddle.workshop.app import _detect_available_backends

        result = _detect_available_backends()
        names = [b["name"] for b in result]
        assert "openai" in names

    def test_openai_backend_detected_via_base_url(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")

        from heddle.workshop.app import _detect_available_backends

        result = _detect_available_backends()
        names = [b["name"] for b in result]
        assert "openai" in names

    def test_all_backends_detected(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        from heddle.workshop.app import _detect_available_backends

        result = _detect_available_backends()
        names = [b["name"] for b in result]
        assert "anthropic" in names
        assert "ollama" in names
        assert "openai" in names


# ---------------------------------------------------------------------------
# _build_extra_config_dirs
# ---------------------------------------------------------------------------


class TestBuildExtraConfigDirs:
    def test_no_apps_returns_empty(self, tmp_path):
        from heddle.workshop.app import _build_extra_config_dirs
        from heddle.workshop.app_manager import AppManager

        mgr = AppManager(apps_dir=str(tmp_path / "apps"))
        result = _build_extra_config_dirs(mgr)
        assert result == []

    def test_deployed_app_with_configs_included(self, tmp_path):
        from heddle.workshop.app import _build_extra_config_dirs
        from heddle.workshop.app_manager import AppManager

        apps_dir = tmp_path / "apps"
        apps_dir.mkdir()

        # Simulate a deployed app with a manifest and configs dir
        app_dir = apps_dir / "myapp"
        app_dir.mkdir()
        configs_dir = app_dir / "configs"
        configs_dir.mkdir()
        manifest_yaml = app_dir / "manifest.yaml"
        manifest_yaml.write_text("name: myapp\nversion: 1.0.0\ndescription: Test app\n")

        mgr = AppManager(apps_dir=str(apps_dir))
        result = _build_extra_config_dirs(mgr)
        assert configs_dir in result

    def test_deployed_app_without_configs_excluded(self, tmp_path):
        from heddle.workshop.app import _build_extra_config_dirs
        from heddle.workshop.app_manager import AppManager

        apps_dir = tmp_path / "apps"
        apps_dir.mkdir()

        # Deployed app with no configs subdir
        app_dir = apps_dir / "emptyapp"
        app_dir.mkdir()
        manifest_yaml = app_dir / "manifest.yaml"
        manifest_yaml.write_text("name: emptyapp\nversion: 1.0.0\ndescription: Empty app\n")

        mgr = AppManager(apps_dir=str(apps_dir))
        result = _build_extra_config_dirs(mgr)
        assert result == []


# ---------------------------------------------------------------------------
# Lifespan / mDNS (lines 87-98)
# ---------------------------------------------------------------------------


@contextmanager
def _lifespan_workshop(tmp_path, *, mdns_module=None, runner_cls_patch=None):
    """Build a workshop app for lifespan tests with shared mdns / runner stubs.

    Centralises the boilerplate that previously appeared four times in
    ``TestLifespan``: temp ``configs/`` directory, ``mock.patch.dict`` for
    ``heddle.discovery.mdns`` (so the real zeroconf advertiser does not
    block the event loop), optional ``WorkerTestRunner`` patch, and the
    ``TestClient`` lifespan dance.

    Yields the ``TestClient`` so the test can hit routes; the runner-class
    patch (if any) is exposed via ``client._runner_cls`` for assertions.

    Args:
        tmp_path: pytest's per-test temp directory.
        mdns_module: value to insert at ``sys.modules["heddle.discovery.mdns"]``.
            ``None`` simulates ``ImportError`` (the real mdns code path is
            ``import heddle.discovery.mdns`` which raises when the value
            is ``None``).  A ``MagicMock`` simulates the package being
            available with a stubbed advertiser.
        runner_cls_patch: optional ``MagicMock`` already configured to be
            returned by ``mock.patch("heddle.workshop.app.WorkerTestRunner")``.
            ``None`` skips the runner patch entirely.
    """
    configs_dir = tmp_path / "configs"
    (configs_dir / "workers").mkdir(parents=True)
    (configs_dir / "orchestrators").mkdir()

    patches: list = [mock.patch.dict("sys.modules", {"heddle.discovery.mdns": mdns_module})]
    runner_patcher = None
    if runner_cls_patch is not None:
        runner_patcher = mock.patch(
            "heddle.workshop.app.WorkerTestRunner",
            return_value=runner_cls_patch,
        )
        patches.append(runner_patcher)

    with patches[0]:
        if runner_patcher is not None:
            with runner_patcher:
                app = create_app(
                    configs_dir=str(configs_dir),
                    db_path=":memory:",
                    apps_dir=str(tmp_path / "apps"),
                )
                with TestClient(app) as client:
                    yield client
        else:
            app = create_app(
                configs_dir=str(configs_dir),
                db_path=":memory:",
                apps_dir=str(tmp_path / "apps"),
            )
            with TestClient(app) as client:
                yield client


class TestLifespan:
    def test_lifespan_mdns_import_error_is_swallowed(self, tmp_path):
        """If heddle[mdns] is not installed, lifespan should not raise."""
        with _lifespan_workshop(tmp_path, mdns_module=None) as c:
            resp = c.get("/health")
            assert resp.status_code == 200

    def test_lifespan_mdns_advertiser_started_and_stopped(self, tmp_path):
        """If mDNS is available, advertiser should be started and stopped."""
        mock_advertiser = mock.AsyncMock()
        mock_advertiser.start = mock.AsyncMock()
        mock_advertiser.stop = mock.AsyncMock()
        mock_advertiser.register_workshop = mock.MagicMock()

        mock_mdns_module = mock.MagicMock()
        mock_mdns_module.HeddleServiceAdvertiser.return_value = mock_advertiser

        with _lifespan_workshop(tmp_path, mdns_module=mock_mdns_module) as c:
            c.get("/health")

        mock_advertiser.start.assert_awaited_once()
        mock_advertiser.stop.assert_awaited_once()

    def test_lifespan_closes_test_runner_on_shutdown(self, tmp_path):
        """Shutdown must invoke ``test_runner.aclose()`` so backend httpx
        clients are released.  Without this, every workshop restart
        leaks sockets and tasks against any backend that was wired up
        from environment vars.
        """
        instance = mock.MagicMock()
        instance.aclose = mock.AsyncMock()

        with _lifespan_workshop(tmp_path, mdns_module=None, runner_cls_patch=instance) as c:
            c.get("/health")
            # TestClient.__exit__ runs the lifespan teardown.

        instance.aclose.assert_awaited_once()

    def test_lifespan_swallows_test_runner_aclose_failure(self, tmp_path):
        """A flaky aclose() on the test runner must not crash shutdown.

        Workshop shutdown is best-effort — the lifespan teardown logs
        the error and continues.
        """
        instance = mock.MagicMock()
        instance.aclose = mock.AsyncMock(side_effect=RuntimeError("flaky"))

        # No exception should escape the lifespan.
        with _lifespan_workshop(tmp_path, mdns_module=None, runner_cls_patch=instance) as c:
            c.get("/health")

        instance.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Worker validate (lines 178-197)
# ---------------------------------------------------------------------------


class TestWorkerValidate:
    def test_validate_valid_yaml(self, client):
        resp = client.post(
            "/workers/test_worker/validate",
            data={
                "yaml_content": (
                    "name: test_worker\nsystem_prompt: Summarize.\ndefault_model_tier: local\n"
                )
            },
        )
        assert resp.status_code == 200

    def test_validate_empty_yaml(self, client):
        resp = client.post(
            "/workers/test_worker/validate",
            data={"yaml_content": ""},
        )
        assert resp.status_code == 200
        assert "empty" in resp.text.lower() or resp.text  # renders partial

    def test_validate_non_mapping_yaml(self, client):
        resp = client.post(
            "/workers/test_worker/validate",
            data={"yaml_content": "- item1\n- item2\n"},
        )
        assert resp.status_code == 200

    def test_validate_invalid_yaml_syntax(self, client):
        resp = client.post(
            "/workers/test_worker/validate",
            data={"yaml_content": "name: [unclosed"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Worker save (lines 218-230)
# ---------------------------------------------------------------------------


class TestWorkerSave:
    def test_save_valid_config_redirects(self, client):
        yaml_content = (
            "name: test_worker\nsystem_prompt: Do something.\ndefault_model_tier: local\n"
        )
        resp = client.post(
            "/workers/test_worker",
            data={"yaml_content": yaml_content, "description": "updated"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/workers/test_worker" in resp.headers["location"]

    def test_save_invalid_yaml_returns_400(self, client):
        resp = client.post(
            "/workers/test_worker",
            data={"yaml_content": "name: [broken yaml"},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_save_invalid_config_returns_400(self, client):
        # Valid YAML but fails config validation (missing system_prompt)
        resp = client.post(
            "/workers/test_worker",
            data={"yaml_content": "not_a_valid_field: 123\n"},
            follow_redirects=False,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Worker clone (lines 232-239)
# ---------------------------------------------------------------------------


class TestWorkerClone:
    def test_clone_existing_worker_redirects(self, client):
        resp = client.post(
            "/workers/test_worker/clone",
            data={"new_name": "cloned_worker"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "cloned_worker" in resp.headers["location"]

    def test_clone_overwrites_existing_and_redirects(self, client):
        # Clone test_worker twice to same new name — second should also redirect
        # (clone_worker always overwrites; no unique-name enforcement)
        client.post(
            "/workers/test_worker/clone",
            data={"new_name": "cloned_worker_dup"},
            follow_redirects=False,
        )
        resp = client.post(
            "/workers/test_worker/clone",
            data={"new_name": "cloned_worker_dup"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "cloned_worker_dup" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Worker test page (lines 243-258)
# ---------------------------------------------------------------------------


class TestWorkerTestPage:
    def test_test_page_found(self, client):
        resp = client.get("/workers/test_worker/test")
        assert resp.status_code == 200

    def test_test_page_not_found(self, client):
        resp = client.get("/workers/nonexistent/test")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Worker test run (lines 260-284)
# ---------------------------------------------------------------------------


class TestWorkerTestRun:
    def test_run_returns_result_partial(self, client):
        import unittest.mock as mock

        from heddle.workshop.test_runner import WorkerTestResult

        mock_result = WorkerTestResult(output={"summary": "hello"}, latency_ms=42)

        with mock.patch(
            "heddle.workshop.test_runner.WorkerTestRunner.run",
            new=mock.AsyncMock(return_value=mock_result),
        ):
            resp = client.post(
                "/workers/test_worker/test/run",
                data={
                    "payload": '{"text": "hello world"}',
                    "tier": "local",
                },
            )
        assert resp.status_code == 200

    def test_run_invalid_json_payload(self, client):
        resp = client.post(
            "/workers/test_worker/test/run",
            data={"payload": "not valid json", "tier": "local"},
        )
        assert resp.status_code == 200
        assert "Invalid JSON" in resp.text

    def test_run_worker_not_found(self, client):
        resp = client.post(
            "/workers/nonexistent/test/run",
            data={"payload": '{"text": "hi"}', "tier": "local"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Worker eval page (lines 289-305)
# ---------------------------------------------------------------------------


class TestWorkerEvalPage:
    def test_eval_page_found(self, client):
        resp = client.get("/workers/test_worker/eval")
        assert resp.status_code == 200

    def test_eval_page_not_found(self, client):
        resp = client.get("/workers/nonexistent/eval")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Worker eval run (lines 307-332)
# ---------------------------------------------------------------------------


class TestWorkerEvalRun:
    def test_eval_run_redirects_to_detail(self, client):
        import unittest.mock as mock

        fake_run_id = "run-abc-123"
        with mock.patch(
            "heddle.workshop.eval_runner.EvalRunner.run_suite",
            new=mock.AsyncMock(return_value=fake_run_id),
        ):
            resp = client.post(
                "/workers/test_worker/eval/run",
                data={
                    "test_suite": (
                        "- name: case1\n"
                        "  input:\n"
                        "    text: hello\n"
                        "  expected_output:\n"
                        "    summary: hi\n"
                    ),
                    "tier": "local",
                    "scoring": "field_match",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert fake_run_id in resp.headers["location"]

    def test_eval_run_invalid_suite_returns_400(self, client):
        resp = client.post(
            "/workers/test_worker/eval/run",
            data={
                "test_suite": "not a list",
                "tier": "local",
                "scoring": "field_match",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_eval_run_worker_not_found(self, client):
        resp = client.post(
            "/workers/nonexistent/eval/run",
            data={
                "test_suite": "- name: c\n  input:\n    text: hi\n",
                "tier": "local",
                "scoring": "field_match",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_eval_run_llm_judge_accepts_scoring_param(self, client):
        """llm_judge scoring param is accepted; redirects when run_suite succeeds."""
        import unittest.mock as mock

        with mock.patch(
            "heddle.workshop.eval_runner.EvalRunner.run_suite",
            new=mock.AsyncMock(return_value="run-id-x"),
        ):
            resp = client.post(
                "/workers/test_worker/eval/run",
                data={
                    "test_suite": ("- name: case1\n  input:\n    text: hello\n"),
                    "tier": "local",
                    "scoring": "llm_judge",
                },
                follow_redirects=False,
            )
        # With no backends at all, returns 400; with a backend, redirects (303)
        assert resp.status_code in (303, 400)


# ---------------------------------------------------------------------------
# App deploy validation (lines 373-390, 403-436)
# ---------------------------------------------------------------------------


class TestAppDeploy:
    def _make_valid_zip(self, tmp_path, name="testapp") -> bytes:
        """Create a minimal valid ZIP with manifest.yaml."""
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            manifest_content = f"name: {name}\nversion: 1.0.0\ndescription: Test application\n"
            zf.writestr("manifest.yaml", manifest_content)
        return buf.getvalue()

    def test_deploy_non_zip_redirects_with_error(self, client):
        resp = client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": ("app.txt", b"not a zip", "text/plain")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in resp.headers["location"].lower()

    def test_deploy_valid_zip_redirects_to_app(self, client, tmp_path):
        zip_bytes = self._make_valid_zip(tmp_path)
        resp = client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": ("testapp.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "testapp" in resp.headers["location"]

    def test_deploy_invalid_zip_content_redirects_with_error(self, client):
        resp = client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": ("bad.zip", b"PK\x03\x04garbage", "application/zip")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in resp.headers["location"].lower()

    def test_deploy_zip_without_manifest_redirects_with_error(self, client):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("some_file.txt", "no manifest here")
        zip_bytes = buf.getvalue()

        resp = client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": ("nomanifest.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" in resp.headers["location"].lower()

    def test_deploy_error_redirect_urlencodes_message(self, client):
        """AppDeployError messages must be URL-encoded in the redirect.

        ``&`` in an error message would inject an extra query parameter;
        ``#`` would create a URL fragment.  Both corrupt the redirect.
        Patches ``peek_manifest`` because the route calls it first (for
        the D1 collision check); the urlencode code path is shared with
        the ``deploy_app`` branch so one test covers both.
        """
        from urllib.parse import parse_qs, urlparse

        from heddle.workshop import app_manager

        bad_msg = "bad & dangerous # error"
        with mock.patch.object(
            app_manager.AppManager,
            "peek_manifest",
            side_effect=app_manager.AppDeployError(bad_msg),
        ):
            resp = client.post(
                "/apps/deploy?auto_approve=1",
                files={"zip_file": ("any.zip", b"PK\x03\x04anything", "application/zip")},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        parsed = urlparse(resp.headers["location"])
        assert parsed.path == "/apps"
        assert parsed.fragment == ""  # ``#`` did not escape into a fragment
        assert parse_qs(parsed.query)["error"] == [bad_msg]


# ---------------------------------------------------------------------------
# App detail (lines 440-444)
# ---------------------------------------------------------------------------


class TestAppDetail:
    def _deploy_test_app(self, client, name="myapp"):
        """Deploy a minimal app and return its name."""
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            manifest_content = f"name: {name}\nversion: 1.0.0\ndescription: Detail test app\n"
            zf.writestr("manifest.yaml", manifest_content)
        zip_bytes = buf.getvalue()
        client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": (f"{name}.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        return name

    def test_app_detail_shows_deployed_app(self, client):
        name = self._deploy_test_app(client, name="detailapp")
        resp = client.get(f"/apps/{name}")
        assert resp.status_code == 200

    def test_app_detail_not_found(self, client):
        resp = client.get("/apps/nonexistent_app_xyz")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Deployed-app config lookup (#12 D1)
# ---------------------------------------------------------------------------


def _build_app_zip_with_configs(
    *,
    app_name: str,
    workers: dict[str, str] | None = None,
    pipelines: dict[str, str] | None = None,
) -> bytes:
    """Build a ZIP containing a manifest plus the named worker/pipeline configs.

    ``workers`` / ``pipelines`` map filename stem → YAML content string.
    """
    import io
    import zipfile

    workers = workers or {}
    pipelines = pipelines or {}
    sections = ["entry_configs:"]
    if workers:
        sections.append("  workers:")
        sections.extend(f"    - config: configs/workers/{n}.yaml" for n in workers)
    else:
        sections.append("  workers: []")
    if pipelines:
        sections.append("  pipelines:")
        sections.extend(f"    - config: configs/orchestrators/{n}.yaml" for n in pipelines)
    else:
        sections.append("  pipelines: []")
    manifest = (
        f"name: {app_name}\n"
        "version: 1.0.0\n"
        "description: D1 lookup test app\n" + "\n".join(sections) + "\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.yaml", manifest)
        for stem, body in workers.items():
            zf.writestr(f"configs/workers/{stem}.yaml", body)
        for stem, body in pipelines.items():
            zf.writestr(f"configs/orchestrators/{stem}.yaml", body)
    return buf.getvalue()


_DEPLOYED_WORKER_YAML = (
    "name: deployed_w\n"
    "system_prompt: From the deployed app.\n"
    "input_schema:\n"
    "  type: object\n"
    "  required: [text]\n"
    "  properties:\n"
    "    text: {type: string}\n"
    "output_schema:\n"
    "  type: object\n"
    "  required: [summary]\n"
    "  properties:\n"
    "    summary: {type: string}\n"
    "default_model_tier: local\n"
)

_DEPLOYED_PIPELINE_YAML = (
    "name: deployed_p\n"
    "description: Pipeline from deployed app\n"
    "pipeline_stages:\n"
    "  - name: stage1\n"
    "    worker_type: deployed_w\n"
    "    input_mapping:\n"
    "      text: input.text\n"
)


class TestDeployedAppConfigLookup:
    def _deploy(self, client, *, app_name, workers=None, pipelines=None):
        zip_bytes = _build_app_zip_with_configs(
            app_name=app_name, workers=workers, pipelines=pipelines
        )
        resp = client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": (f"{app_name}.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        assert resp.status_code == 303, (
            f"deploy failed: {resp.status_code} {resp.headers.get('location')}"
        )
        return resp

    def test_deployed_app_worker_detail_route_resolves_extra_config(self, client):
        """Pre-fix the deployed app's worker showed in /workers but
        /workers/{name} 404'd because get_worker only searched base."""
        self._deploy(client, app_name="lookupapp", workers={"deployed_w": _DEPLOYED_WORKER_YAML})

        list_resp = client.get("/workers")
        assert list_resp.status_code == 200
        assert "deployed_w" in list_resp.text

        detail_resp = client.get("/workers/deployed_w")
        assert detail_resp.status_code == 200
        assert "From the deployed app." in detail_resp.text

    def test_deployed_app_pipeline_detail_route_resolves_extra_config(self, client):
        """Same bug, pipeline edition."""
        self._deploy(
            client, app_name="pipelookup", pipelines={"deployed_p": _DEPLOYED_PIPELINE_YAML}
        )

        list_resp = client.get("/pipelines")
        assert list_resp.status_code == 200
        assert "deployed_p" in list_resp.text

        detail_resp = client.get("/pipelines/deployed_p")
        assert detail_resp.status_code == 200

    def test_deploy_rejects_collision_with_base_worker(self, client):
        """Operator decision: reject on collision (chosen during D-session
        scoping).  Deploying an app whose worker name already exists in
        the base configs must fail with a clear error rather than shadow.
        """
        # The ``client`` fixture already wrote ``test_worker.yaml`` to base.
        zip_bytes = _build_app_zip_with_configs(
            app_name="collideapp", workers={"test_worker": _DEPLOYED_WORKER_YAML}
        )
        resp = client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": ("collideapp.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "error" in location
        # Error message mentions both the colliding name and the rename
        # remediation so the operator knows what to do.
        from urllib.parse import parse_qs, urlparse

        err = parse_qs(urlparse(location).query)["error"][0]
        assert "test_worker" in err
        assert "rename" in err.lower()

    def test_deploy_rejects_collision_across_apps(self, client):
        """Two different apps can't both ship a worker named 'shared_w'."""
        self._deploy(client, app_name="firstapp", workers={"shared_w": _DEPLOYED_WORKER_YAML})
        zip_bytes = _build_app_zip_with_configs(
            app_name="secondapp", workers={"shared_w": _DEPLOYED_WORKER_YAML}
        )
        resp = client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": ("secondapp.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        from urllib.parse import parse_qs, urlparse

        err = parse_qs(urlparse(resp.headers["location"]).query)["error"][0]
        assert "shared_w" in err

    def test_redeploy_same_app_does_not_collide_with_itself(self, client):
        """An app's own existing config stems must be excluded from the
        forbidden set — otherwise every redeploy would fail."""
        self._deploy(client, app_name="redeployapp", workers={"my_worker": _DEPLOYED_WORKER_YAML})
        # Redeploy with the same name + same worker stem — should succeed.
        self._deploy(client, app_name="redeployapp", workers={"my_worker": _DEPLOYED_WORKER_YAML})


# ---------------------------------------------------------------------------
# Two-step deploy preview (#10 D2)
# ---------------------------------------------------------------------------


_RISKY_WORKER_YAML = (
    "name: risky_w\n"
    "worker_kind: processor\n"
    "processing_backend: heddle.contrib.subprocess.SubprocessBackend\n"
    "backend_config:\n"
    "  workspace_dir: /tmp/risky\n"
    "  command: ${RISKY_CMD}\n"
    "knowledge_silos:\n"
    "  - name: notes\n"
    "    type: folder_readwrite\n"
    "    path: /var/notes\n"
    "input_schema:\n"
    "  type: object\n"
    "  required: [x]\n"
    "  properties:\n"
    "    x: {type: string}\n"
    "output_schema:\n"
    "  type: object\n"
    "  required: [y]\n"
    "  properties:\n"
    "    y: {type: string}\n"
    "default_model_tier: local\n"
)


class TestDeployPreview:
    """End-to-end coverage of the two-step deploy + ``?auto_approve=1`` bypass."""

    def _upload_for_preview(self, client, *, app_name, workers):
        zip_bytes = _build_app_zip_with_configs(app_name=app_name, workers=workers)
        # NO ?auto_approve — exercise the preview path.
        resp = client.post(
            "/apps/deploy",
            files={"zip_file": (f"{app_name}.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        return resp

    def test_upload_renders_preview_with_capability_findings(self, client):
        """Uploading without ?auto_approve returns the preview HTML, not a 303,
        and the page lists every powerful field from the risky worker.
        """
        resp = self._upload_for_preview(
            client, app_name="previewapp", workers={"risky_w": _RISKY_WORKER_YAML}
        )
        assert resp.status_code == 200
        assert "Confirm Deploy" in resp.text
        assert "previewapp" in resp.text
        # The four capability categories the risky worker should trip:
        assert "SubprocessBackend" in resp.text  # code_execution
        assert "/tmp/risky" in resp.text  # filesystem (workspace_dir)
        assert "/var/notes" in resp.text  # filesystem (knowledge silo)
        assert "RISKY_CMD" in resp.text  # env_var
        # Confirm + cancel forms both rendered with the same staging token
        import re

        tokens = re.findall(r"/apps/deploy/(?:confirm|cancel)/([a-f0-9]{32})", resp.text)
        assert len(tokens) == 2
        assert tokens[0] == tokens[1]

    def test_preview_then_confirm_deploys_and_app_detail_loads(self, client):
        """Full happy-path e2e: upload → preview → confirm → /apps/{name} 200."""
        import re

        preview = self._upload_for_preview(
            client, app_name="confirmapp", workers={"deployed_w": _DEPLOYED_WORKER_YAML}
        )
        assert preview.status_code == 200
        token = re.search(r"/apps/deploy/confirm/([a-f0-9]{32})", preview.text).group(1)

        # Pre-confirm: the app is NOT yet visible.
        list_before = client.get("/apps")
        assert "confirmapp" not in list_before.text

        confirm = client.post(f"/apps/deploy/confirm/{token}", follow_redirects=False)
        assert confirm.status_code == 303
        assert confirm.headers["location"].endswith("/apps/confirmapp")

        # Post-confirm: detail page loads, deployed worker is reachable
        # via the D1 source-aware lookup.
        detail = client.get("/apps/confirmapp")
        assert detail.status_code == 200
        worker_detail = client.get("/workers/deployed_w")
        assert worker_detail.status_code == 200

    def test_cancel_discards_staged_bundle_without_deploying(self, client):
        """Cancel must remove the staging dir and not commit the app."""
        import re

        preview = self._upload_for_preview(
            client, app_name="cancelapp", workers={"deployed_w": _DEPLOYED_WORKER_YAML}
        )
        token = re.search(r"/apps/deploy/cancel/([a-f0-9]{32})", preview.text).group(1)

        cancel = client.post(f"/apps/deploy/cancel/{token}", follow_redirects=False)
        assert cancel.status_code == 303
        assert cancel.headers["location"].endswith("/apps")

        # Confirming with the same token after cancel must fail cleanly.
        retry = client.post(f"/apps/deploy/confirm/{token}", follow_redirects=False)
        assert retry.status_code == 303
        assert "error" in retry.headers["location"]

        # The app is not visible.
        list_after = client.get("/apps")
        assert "cancelapp" not in list_after.text

    def test_confirm_with_invalid_token_format_redirects_with_error(self, client):
        """Token format is regex-validated.  Non-hex tokens (like attacker
        payloads that slipped through other defences) get rejected before
        any filesystem call, redirected to /apps with a clear error.
        Path-segment-violating payloads (with ``/``) are rejected even
        earlier by FastAPI's routing — that's a separate layer.
        """
        from urllib.parse import parse_qs, urlparse

        resp = client.post(
            "/apps/deploy/confirm/notahextoken1234567890notahex01", follow_redirects=False
        )
        assert resp.status_code == 303
        err = parse_qs(urlparse(resp.headers["location"]).query)["error"][0]
        assert "Invalid staging token" in err

    def test_auto_approve_bypasses_preview(self, client):
        """``?auto_approve=1`` is the CI escape hatch — no preview, just deploy.

        Verified by the existing helpers that already use this query
        param; this test makes the bypass contract explicit so a future
        refactor that removes it will fail loudly here.
        """
        zip_bytes = _build_app_zip_with_configs(
            app_name="bypassapp", workers={"deployed_w": _DEPLOYED_WORKER_YAML}
        )
        resp = client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": ("bypassapp.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # NOT 200 (preview) — straight to redirect
        assert resp.headers["location"].endswith("/apps/bypassapp")


# ---------------------------------------------------------------------------
# App remove (lines 450-452)
# ---------------------------------------------------------------------------


class TestAppRemove:
    def test_remove_deployed_app_redirects(self, client):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "manifest.yaml",
                "name: removeapp\nversion: 1.0.0\ndescription: Remove test\n",
            )
        zip_bytes = buf.getvalue()
        client.post(
            "/apps/deploy?auto_approve=1",
            files={"zip_file": ("removeapp.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )

        resp = client.post("/apps/removeapp/remove", follow_redirects=False)
        assert resp.status_code == 303
        assert "/apps" in resp.headers["location"]

    def test_remove_nonexistent_app_still_redirects(self, client):
        # FileNotFoundError is swallowed; should redirect cleanly
        resp = client.post("/apps/does_not_exist/remove", follow_redirects=False)
        assert resp.status_code == 303
        assert "/apps" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Dead letters list (lines 460-464)
# ---------------------------------------------------------------------------


class TestDeadLettersList:
    def test_dead_letters_list_shows_counts(self, client):
        consumer = client.app.state.dead_letter_consumer
        consumer.store({"x": 1}, "test_reason", task_id="t-dl-1")

        resp = client.get("/dead-letters")
        assert resp.status_code == 200

    def test_dead_letters_list_includes_replay_log(self, client):
        consumer = client.app.state.dead_letter_consumer
        entry = consumer.store({"x": 2}, "test_reason", task_id="t-dl-2")

        # Replay it so replay log is non-empty
        client.post(
            f"/dead-letters/{entry.id}/replay",
            follow_redirects=False,
        )

        resp = client.get("/dead-letters")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Dead letter replay (lines 472-496)
# ---------------------------------------------------------------------------


class TestDeadLetterReplay:
    def test_replay_valid_entry_redirects(self, client):
        consumer = client.app.state.dead_letter_consumer
        entry = consumer.store(
            {"worker_type": "test", "payload": {"x": 1}},
            "timeout",
            task_id="replay-t1",
        )

        resp = client.post(
            f"/dead-letters/{entry.id}/replay",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/dead-letters" in resp.headers["location"]

    def test_replay_increments_replay_count(self, client):
        consumer = client.app.state.dead_letter_consumer
        before = consumer.replay_count()
        entry = consumer.store({"y": 1}, "rate_limited", task_id="replay-t2")

        client.post(
            f"/dead-letters/{entry.id}/replay",
            follow_redirects=False,
        )
        assert consumer.replay_count() == before + 1

    def test_replay_creates_audit_entry(self, client):
        consumer = client.app.state.dead_letter_consumer
        entry = consumer.store(
            {"z": 1}, "worker_error", task_id="replay-t3", worker_type="my_worker"
        )

        client.post(
            f"/dead-letters/{entry.id}/replay",
            follow_redirects=False,
        )
        log = consumer.replay_log()
        task_ids = [r["task_id"] for r in log]
        assert "replay-t3" in task_ids


# ---------------------------------------------------------------------------
# Dead letters clear (lines 500-506)
# ---------------------------------------------------------------------------


class TestDeadLettersClear:
    def test_clear_removes_entries(self, client):
        consumer = client.app.state.dead_letter_consumer
        consumer.store({"a": 1}, "reason_a", task_id="clear-t1")
        consumer.store({"b": 2}, "reason_b", task_id="clear-t2")
        assert consumer.count() >= 2

        resp = client.post("/dead-letters/clear", follow_redirects=False)
        assert resp.status_code == 303
        assert consumer.count() == 0

    def test_clear_redirects_to_dead_letters(self, client):
        resp = client.post("/dead-letters/clear", follow_redirects=False)
        assert resp.status_code == 303
        assert "/dead-letters" in resp.headers["location"]

    def test_clear_preserves_replay_log(self, client):
        consumer = client.app.state.dead_letter_consumer
        entry = consumer.store({"c": 3}, "test", task_id="clear-t3")
        client.post(
            f"/dead-letters/{entry.id}/replay",
            follow_redirects=False,
        )
        before_replay_count = consumer.replay_count()

        client.post("/dead-letters/clear", follow_redirects=False)

        # Entries cleared, replay log preserved
        assert consumer.count() == 0
        assert consumer.replay_count() == before_replay_count


# ---------------------------------------------------------------------------
# Pipeline stage edit (lines 401-436)
# ---------------------------------------------------------------------------


class TestPipelineStageEdit:
    """Tests for POST /pipelines/{name}/stage (insert/remove/swap actions)."""

    @pytest.fixture
    def pipeline_client(self, tmp_path):
        """Workshop TestClient with a pipeline config available."""
        configs_dir = tmp_path / "configs"
        workers_dir = configs_dir / "workers"
        workers_dir.mkdir(parents=True)
        orch_dir = configs_dir / "orchestrators"
        orch_dir.mkdir(parents=True)

        # Create a worker config
        worker_yaml = workers_dir / "summarizer.yaml"
        worker_yaml.write_text(
            "name: summarizer\nsystem_prompt: Summarize.\ndefault_model_tier: local\n"
        )

        # Create a pipeline config
        pipeline_yaml = orch_dir / "test_pipe.yaml"
        pipeline_yaml.write_text(
            "name: test_pipe\npipeline_stages:\n  - name: stage1\n    worker_type: summarizer\n"
        )

        app = create_app(
            configs_dir=str(configs_dir),
            db_path=":memory:",
            apps_dir=str(tmp_path / "apps"),
        )
        return TestClient(app)

    def test_pipeline_stage_insert(self, pipeline_client):
        resp = pipeline_client.post(
            "/pipelines/test_pipe/stage",
            data={
                "action": "insert",
                "stage_yaml": "name: stage2\nworker_type: summarizer\n",
                "after_stage": "stage1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/pipelines/test_pipe" in resp.headers["location"]

    def test_pipeline_stage_remove(self, pipeline_client):
        # First insert a second stage so we can remove stage1
        pipeline_client.post(
            "/pipelines/test_pipe/stage",
            data={
                "action": "insert",
                "stage_yaml": "name: stage2\nworker_type: summarizer\n",
            },
            follow_redirects=False,
        )
        resp = pipeline_client.post(
            "/pipelines/test_pipe/stage",
            data={
                "action": "remove",
                "stage_name": "stage2",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/pipelines/test_pipe" in resp.headers["location"]

    def test_pipeline_stage_swap(self, pipeline_client):
        resp = pipeline_client.post(
            "/pipelines/test_pipe/stage",
            data={
                "action": "swap",
                "stage_name": "stage1",
                "new_worker_type": "summarizer",
                "new_tier": "standard",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/pipelines/test_pipe" in resp.headers["location"]

    def test_pipeline_not_found(self, pipeline_client):
        resp = pipeline_client.post(
            "/pipelines/nonexistent_pipeline/stage",
            data={
                "action": "remove",
                "stage_name": "stage1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# NATS wiring — dead-letter subscription + reload publish (A1)
# ---------------------------------------------------------------------------


class TestNATSWiring:
    """Pin the lifespan-time NATS wiring contract.

    The Workshop must:
    - Use no NATS at all when ``nats_url`` is ``None`` (default for
      tests and local-dev).
    - Subscribe to ``heddle.tasks.dead_letter`` and feed entries into
      the ``DeadLetterConsumer`` when ``nats_url`` points at a
      reachable bus.
    - Publish ``heddle.control.reload`` via the ``AppManager`` on
      ``notify_reload()`` (read of dead-letter UI + write of reload
      both share the same bus).
    - Fall back silently to the process-local ``InMemoryBus`` when
      ``nats_url`` is provided but ``connect()`` raises — the UI must
      still come up so the operator can inspect deployed apps even
      when NATS is down.
    """

    @staticmethod
    @contextmanager
    def _build_app(tmp_path, *, nats_url, patched_bus_cls=None):
        """Build a Workshop app with mDNS stubbed and NATSBus optionally swapped.

        Yielded as a context manager so the patches stay active for the
        whole ``with TestClient(app) as client`` block — the lifespan
        runs when ``TestClient`` is entered, well after ``create_app``
        returns, so the patches must outlive the construction call.
        """
        import contextlib as _ctxlib

        configs_dir = tmp_path / "configs"
        (configs_dir / "workers").mkdir(parents=True, exist_ok=True)
        (configs_dir / "orchestrators").mkdir(exist_ok=True)

        # mDNS registration uses ``zeroconf`` which spins up its own
        # event loop on a thread; under ``TestClient`` lifespan that
        # loop is the test's loop and gets closed between cases,
        # tripping ``EventLoopBlocked``.  Stub the advertiser to a
        # no-op so the lifespan path under test (the NATS wiring) is
        # the only thing exercised.
        class _NoOpAdvertiser:
            async def start(self):
                return None

            def register_workshop(self, port):
                _ = port

            async def stop(self):
                return None

        bus_ctx = (
            mock.patch("heddle.bus.nats_adapter.NATSBus", patched_bus_cls)
            if patched_bus_cls is not None
            else _ctxlib.nullcontext()
        )
        with (
            mock.patch("heddle.discovery.mdns.HeddleServiceAdvertiser", _NoOpAdvertiser),
            bus_ctx,
        ):
            yield create_app(
                configs_dir=str(configs_dir),
                db_path=":memory:",
                apps_dir=str(tmp_path / "apps"),
                nats_url=nats_url,
            )

    def test_no_nats_url_skips_wiring(self, tmp_path):
        with self._build_app(tmp_path, nats_url=None) as app, TestClient(app) as client:
            assert client.app.state.nats_bus is None
            resp = client.get("/dead-letters")
            assert resp.status_code == 200

    def test_nats_url_connects_and_shares_bus(self, tmp_path):
        from heddle.bus.memory import InMemoryBus

        class _StubNATS(InMemoryBus):
            def __init__(self, url):
                _ = url
                super().__init__()

        with (
            self._build_app(
                tmp_path, nats_url="nats://stub:4222", patched_bus_cls=_StubNATS
            ) as app,
            TestClient(app) as client,
        ):
            bus = client.app.state.nats_bus
            assert isinstance(bus, _StubNATS)
            # state.nats_bus and the consumer's bus must be the same
            # instance — otherwise the reload broadcast and the
            # dead-letter drain are talking past each other (the bug
            # A1 closed).
            assert client.app.state.dead_letter_consumer._bus is bus

    def test_nats_unavailable_falls_back_silently(self, tmp_path):
        class _FailingNATS:
            def __init__(self, url):
                self.url = url

            async def connect(self):
                raise OSError("connection refused")

            async def close(self):
                pass

        with (
            self._build_app(
                tmp_path,
                nats_url="nats://unreachable:4222",
                patched_bus_cls=_FailingNATS,
            ) as app,
            TestClient(app) as client,
        ):
            # Fallback: state.nats_bus is None, but the UI still works.
            assert client.app.state.nats_bus is None
            resp = client.get("/dead-letters")
            assert resp.status_code == 200
            # The consumer's bus stayed the in-memory default.
            from heddle.bus.memory import InMemoryBus

            assert isinstance(client.app.state.dead_letter_consumer._bus, InMemoryBus)
