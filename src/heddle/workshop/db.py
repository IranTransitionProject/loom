"""
WorkshopDB — DuckDB storage for eval results, worker versions, and metrics.

Provides persistent storage for the Workshop's evaluation and versioning
workflow.  Uses DuckDB for zero-config embedded analytics.

The database is created at ``~/.heddle/workshop.duckdb`` by default (or
``:memory:`` for testing).  Schema is auto-created on first connection.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

if TYPE_CHECKING:
    from datetime import datetime


class WorkshopDB:
    """DuckDB-backed storage for the Workshop.

    Args:
        db_path: Path to the DuckDB file.  Use ``:memory:`` for tests.
            The ``~`` prefix is expanded automatically.
    """

    def __init__(self, db_path: str = "~/.heddle/workshop.duckdb") -> None:
        if db_path != ":memory:":
            path = Path(db_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = duckdb.connect(str(path))
        else:
            self._conn = duckdb.connect(":memory:")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_versions (
                id              VARCHAR PRIMARY KEY,
                worker_name     VARCHAR NOT NULL,
                config_hash     VARCHAR NOT NULL,
                config_yaml     TEXT NOT NULL,
                created_at      TIMESTAMP DEFAULT current_timestamp,
                description     VARCHAR,
                UNIQUE (worker_name, config_hash)
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_runs (
                id              VARCHAR PRIMARY KEY,
                worker_name     VARCHAR NOT NULL,
                worker_version_id VARCHAR,
                tier            VARCHAR NOT NULL,
                started_at      TIMESTAMP DEFAULT current_timestamp,
                completed_at    TIMESTAMP,
                status          VARCHAR NOT NULL DEFAULT 'running',
                total_cases     INTEGER NOT NULL,
                passed_cases    INTEGER DEFAULT 0,
                failed_cases    INTEGER DEFAULT 0,
                avg_latency_ms  DOUBLE,
                avg_prompt_tokens DOUBLE,
                avg_completion_tokens DOUBLE,
                metadata        JSON,
                error           TEXT
            )
        """)
        # Backfill the ``error`` column on databases created before it was added
        # to the schema above.  ``ADD COLUMN IF NOT EXISTS`` is idempotent on
        # DuckDB ≥0.10, which the project's ``duckdb>=1.0.0`` floor guarantees.
        self._conn.execute("ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS error TEXT")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_results (
                id              VARCHAR PRIMARY KEY,
                run_id          VARCHAR NOT NULL,
                case_name       VARCHAR NOT NULL,
                input_payload   JSON NOT NULL,
                expected_output JSON,
                actual_output   JSON,
                raw_response    TEXT,
                validation_errors JSON,
                score           DOUBLE,
                score_details   JSON,
                latency_ms      INTEGER,
                prompt_tokens   INTEGER,
                completion_tokens INTEGER,
                model_used      VARCHAR,
                passed          BOOLEAN NOT NULL,
                error           TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_metrics (
                id              VARCHAR PRIMARY KEY,
                worker_name     VARCHAR NOT NULL,
                tier            VARCHAR NOT NULL,
                recorded_at     TIMESTAMP DEFAULT current_timestamp,
                window_seconds  INTEGER NOT NULL DEFAULT 60,
                request_count   INTEGER NOT NULL,
                success_count   INTEGER NOT NULL,
                failure_count   INTEGER NOT NULL,
                avg_latency_ms  DOUBLE,
                p95_latency_ms  DOUBLE,
                avg_prompt_tokens DOUBLE,
                avg_completion_tokens DOUBLE
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_baselines (
                id              VARCHAR PRIMARY KEY,
                worker_name     VARCHAR NOT NULL UNIQUE,
                run_id          VARCHAR NOT NULL,
                promoted_at     TIMESTAMP DEFAULT current_timestamp,
                description     VARCHAR
            )
        """)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Worker versions
    # ------------------------------------------------------------------

    def save_worker_version(
        self,
        worker_name: str,
        config_yaml: str,
        description: str | None = None,
    ) -> str:
        """Save a worker config version.  Deduplicates by content hash.

        Returns the version ID (existing if duplicate, new if unique).
        """
        config_hash = hashlib.sha256(config_yaml.encode()).hexdigest()[:16]

        # Check for existing version with same hash
        existing = self._conn.execute(
            "SELECT id FROM worker_versions WHERE worker_name = ? AND config_hash = ?",
            [worker_name, config_hash],
        ).fetchone()
        if existing:
            return existing[0]

        version_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO worker_versions (id, worker_name, config_hash, config_yaml, description)
               VALUES (?, ?, ?, ?, ?)""",
            [version_id, worker_name, config_hash, config_yaml, description],
        )
        return version_id

    def get_worker_versions(self, worker_name: str) -> list[dict[str, Any]]:
        """Get version history for a worker, newest first."""
        rows = self._conn.execute(
            """SELECT id, worker_name, config_hash, config_yaml, created_at, description
               FROM worker_versions WHERE worker_name = ?
               ORDER BY created_at DESC""",
            [worker_name],
        ).fetchall()
        return [
            {
                "id": r[0],
                "worker_name": r[1],
                "config_hash": r[2],
                "config_yaml": r[3],
                "created_at": r[4],
                "description": r[5],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Eval runs
    # ------------------------------------------------------------------

    def save_eval_run(
        self,
        worker_name: str,
        tier: str,
        total_cases: int,
        worker_version_id: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Create a new eval run record.  Returns the run ID."""
        run_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO eval_runs
               (id, worker_name, worker_version_id, tier, total_cases, status, metadata)
               VALUES (?, ?, ?, ?, ?, 'running', ?)""",
            [
                run_id,
                worker_name,
                worker_version_id,
                tier,
                total_cases,
                json.dumps(metadata) if metadata else None,
            ],
        )
        return run_id

    def update_eval_run(self, run_id: str, updates: dict[str, Any]) -> None:
        """Update fields on an eval run (e.g., status, passed_cases, avg_latency_ms)."""
        set_clauses = []
        values = []
        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.append(run_id)
        self._conn.execute(
            f"UPDATE eval_runs SET {', '.join(set_clauses)} WHERE id = ?",
            values,
        )

    def get_eval_runs(
        self,
        worker_name: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get eval runs, optionally filtered by worker name."""
        if worker_name:
            rows = self._conn.execute(
                """SELECT id, worker_name, worker_version_id, tier, started_at,
                          completed_at, status, total_cases, passed_cases,
                          failed_cases, avg_latency_ms, error
                   FROM eval_runs WHERE worker_name = ?
                   ORDER BY started_at DESC LIMIT ?""",
                [worker_name, limit],
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT id, worker_name, worker_version_id, tier, started_at,
                          completed_at, status, total_cases, passed_cases,
                          failed_cases, avg_latency_ms, error
                   FROM eval_runs ORDER BY started_at DESC LIMIT ?""",
                [limit],
            ).fetchall()
        return [
            {
                "id": r[0],
                "worker_name": r[1],
                "worker_version_id": r[2],
                "tier": r[3],
                "started_at": r[4],
                "completed_at": r[5],
                "status": r[6],
                "total_cases": r[7],
                "passed_cases": r[8],
                "failed_cases": r[9],
                "avg_latency_ms": r[10],
                "error": r[11],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Eval results
    # ------------------------------------------------------------------

    def save_eval_result(
        self,
        run_id: str,
        case_name: str,
        input_payload: dict,
        passed: bool,
        expected_output: dict | None = None,
        actual_output: dict | None = None,
        raw_response: str | None = None,
        validation_errors: list[str] | None = None,
        score: float | None = None,
        score_details: dict | None = None,
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        model_used: str | None = None,
        error: str | None = None,
    ) -> str:
        """Save an individual eval result.  Returns the result ID."""
        result_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO eval_results
               (id, run_id, case_name, input_payload, expected_output,
                actual_output, raw_response, validation_errors, score,
                score_details, latency_ms, prompt_tokens, completion_tokens,
                model_used, passed, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                result_id,
                run_id,
                case_name,
                json.dumps(input_payload),
                json.dumps(expected_output) if expected_output else None,
                json.dumps(actual_output) if actual_output else None,
                raw_response,
                json.dumps(validation_errors) if validation_errors else None,
                score,
                json.dumps(score_details) if score_details else None,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                model_used,
                passed,
                error,
            ],
        )
        return result_id

    def get_eval_results(self, run_id: str) -> list[dict[str, Any]]:
        """Get all results for an eval run."""
        rows = self._conn.execute(
            """SELECT id, run_id, case_name, input_payload, expected_output,
                      actual_output, raw_response, validation_errors, score,
                      score_details, latency_ms, prompt_tokens, completion_tokens,
                      model_used, passed, error
               FROM eval_results WHERE run_id = ?
               ORDER BY case_name""",
            [run_id],
        ).fetchall()
        return [
            {
                "id": r[0],
                "run_id": r[1],
                "case_name": r[2],
                "input_payload": r[3],
                "expected_output": r[4],
                "actual_output": r[5],
                "raw_response": r[6],
                "validation_errors": r[7],
                "score": r[8],
                "score_details": r[9],
                "latency_ms": r[10],
                "prompt_tokens": r[11],
                "completion_tokens": r[12],
                "model_used": r[13],
                "passed": r[14],
                "error": r[15],
            }
            for r in rows
        ]

    def compare_eval_runs(
        self,
        run_id_a: str,
        run_id_b: str,
    ) -> dict[str, Any]:
        """Compare two eval runs side-by-side.

        Returns a dict with run metadata and per-case comparison.
        """
        runs_a = self.get_eval_runs()
        runs_b = self.get_eval_runs()
        run_a = next((r for r in runs_a if r["id"] == run_id_a), None)
        run_b = next((r for r in runs_b if r["id"] == run_id_b), None)

        results_a = {r["case_name"]: r for r in self.get_eval_results(run_id_a)}
        results_b = {r["case_name"]: r for r in self.get_eval_results(run_id_b)}

        all_cases = sorted(set(results_a.keys()) | set(results_b.keys()))
        comparison = [
            {
                "case_name": case,
                "a": results_a.get(case),
                "b": results_b.get(case),
            }
            for case in all_cases
        ]

        return {
            "run_a": run_a,
            "run_b": run_b,
            "cases": comparison,
        }

    # ------------------------------------------------------------------
    # Eval baselines
    # ------------------------------------------------------------------

    def promote_baseline(
        self,
        worker_name: str,
        run_id: str,
        description: str | None = None,
    ) -> str:
        """Promote an eval run as the baseline for a worker.

        Replaces any existing baseline for the worker.  The baseline run
        serves as the reference point for regression detection.

        Returns the baseline record ID.
        """
        baseline_id = str(uuid.uuid4())
        # Upsert: delete any existing baseline for this worker, then insert.
        self._conn.execute(
            "DELETE FROM eval_baselines WHERE worker_name = ?",
            [worker_name],
        )
        self._conn.execute(
            """INSERT INTO eval_baselines (id, worker_name, run_id, description)
               VALUES (?, ?, ?, ?)""",
            [baseline_id, worker_name, run_id, description],
        )
        return baseline_id

    def get_baseline(self, worker_name: str) -> dict[str, Any] | None:
        """Get the current baseline for a worker.

        Returns a dict with baseline metadata, or None if no baseline is set.
        """
        row = self._conn.execute(
            """SELECT id, worker_name, run_id, promoted_at, description
               FROM eval_baselines WHERE worker_name = ?""",
            [worker_name],
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "worker_name": row[1],
            "run_id": row[2],
            "promoted_at": row[3],
            "description": row[4],
        }

    def remove_baseline(self, worker_name: str) -> bool:
        """Remove the baseline for a worker.  Returns True if one was removed."""
        result = self._conn.execute(
            "DELETE FROM eval_baselines WHERE worker_name = ? RETURNING id",
            [worker_name],
        ).fetchone()
        return result is not None

    def compare_against_baseline(
        self,
        worker_name: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Compare an eval run against the worker's baseline.

        Returns the comparison dict (same as ``compare_eval_runs``), or None
        if no baseline is set for this worker.
        """
        baseline = self.get_baseline(worker_name)
        if not baseline:
            return None
        return self.compare_eval_runs(baseline["run_id"], run_id)

    # ------------------------------------------------------------------
    # Worker metrics
    # ------------------------------------------------------------------

    def save_worker_metric(
        self,
        worker_name: str,
        tier: str,
        request_count: int,
        success_count: int,
        failure_count: int,
        window_seconds: int = 60,
        avg_latency_ms: float | None = None,
        p95_latency_ms: float | None = None,
        avg_prompt_tokens: float | None = None,
        avg_completion_tokens: float | None = None,
    ) -> str:
        """Save an aggregated metrics window."""
        metric_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO worker_metrics
               (id, worker_name, tier, window_seconds, request_count,
                success_count, failure_count, avg_latency_ms, p95_latency_ms,
                avg_prompt_tokens, avg_completion_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                metric_id,
                worker_name,
                tier,
                window_seconds,
                request_count,
                success_count,
                failure_count,
                avg_latency_ms,
                p95_latency_ms,
                avg_prompt_tokens,
                avg_completion_tokens,
            ],
        )
        return metric_id

    def get_worker_metrics(
        self,
        worker_name: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get metrics for a worker, optionally filtered by time."""
        if since:
            rows = self._conn.execute(
                """SELECT id, worker_name, tier, recorded_at, window_seconds,
                          request_count, success_count, failure_count,
                          avg_latency_ms, p95_latency_ms,
                          avg_prompt_tokens, avg_completion_tokens
                   FROM worker_metrics
                   WHERE worker_name = ? AND recorded_at >= ?
                   ORDER BY recorded_at DESC LIMIT ?""",
                [worker_name, since, limit],
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT id, worker_name, tier, recorded_at, window_seconds,
                          request_count, success_count, failure_count,
                          avg_latency_ms, p95_latency_ms,
                          avg_prompt_tokens, avg_completion_tokens
                   FROM worker_metrics
                   WHERE worker_name = ?
                   ORDER BY recorded_at DESC LIMIT ?""",
                [worker_name, limit],
            ).fetchall()
        return [
            {
                "id": r[0],
                "worker_name": r[1],
                "tier": r[2],
                "recorded_at": r[3],
                "window_seconds": r[4],
                "request_count": r[5],
                "success_count": r[6],
                "failure_count": r[7],
                "avg_latency_ms": r[8],
                "p95_latency_ms": r[9],
                "avg_prompt_tokens": r[10],
                "avg_completion_tokens": r[11],
            }
            for r in rows
        ]
