"""Tests for the deploy-time capability extractor (workshop/app_capabilities.py).

Each test crafts a minimal in-memory ZIP and asserts that
``summarize_capabilities`` surfaces exactly the right
``CapabilityFinding``s, grouped by category.  The findings drive what
operators see on the deploy preview page, so over- or under-reporting
either annoys them or hides risk.
"""

from __future__ import annotations

import io
import zipfile

from heddle.core.manifest import AppManifest
from heddle.workshop.app_capabilities import (
    CATEGORY_CODE,
    CATEGORY_ENV,
    CATEGORY_FILESYSTEM,
    CATEGORY_NETWORK,
    summarize_capabilities,
)


def _zip(members: dict[str, str]) -> zipfile.ZipFile:
    """Build an in-memory ZipFile from {member_name: text content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


def _manifest(name="testapp", *, workers=(), pipelines=(), schedulers=(), mcp=()) -> AppManifest:
    return AppManifest(
        name=name,
        version="1.0.0",
        description="x",
        entry_configs={
            "workers": [{"config": c} for c in workers],
            "pipelines": [{"config": c} for c in pipelines],
            "schedulers": [{"config": c} for c in schedulers],
            "mcp": [{"config": c} for c in mcp],
        },
    )


class TestExtractor:
    def test_no_findings_for_plain_llm_worker(self):
        worker_yaml = (
            "name: plain\n"
            "system_prompt: hi\n"
            "input_schema: {type: object, properties: {x: {type: string}}}\n"
            "output_schema: {type: object, properties: {y: {type: string}}}\n"
            "default_model_tier: local\n"
        )
        zf = _zip({"configs/workers/plain.yaml": worker_yaml})
        summary = summarize_capabilities(zf, _manifest(workers=["configs/workers/plain.yaml"]))
        assert summary.findings == []
        assert summary.has_findings is False

    def test_subprocess_backend_flagged_as_code_execution(self):
        worker_yaml = (
            "name: sub\n"
            "worker_kind: processor\n"
            "processing_backend: heddle.contrib.subprocess.SubprocessBackend\n"
            "input_schema: {type: object}\n"
            "output_schema: {type: object}\n"
            "default_model_tier: local\n"
        )
        zf = _zip({"configs/workers/sub.yaml": worker_yaml})
        summary = summarize_capabilities(zf, _manifest(workers=["configs/workers/sub.yaml"]))
        codes = [f for f in summary.findings if f.category == CATEGORY_CODE]
        assert len(codes) == 1
        assert "SubprocessBackend" in codes[0].summary
        assert codes[0].config_file == "configs/workers/sub.yaml"

    def test_workspace_dir_and_silos_flagged_as_filesystem(self):
        worker_yaml = (
            "name: fs\n"
            "system_prompt: hi\n"
            "workspace_dir: /tmp/ws\n"
            "knowledge_silos:\n"
            "  - {name: a, type: folder_readonly, path: /etc/a}\n"
            "  - {name: b, type: folder_readwrite, path: /etc/b}\n"
            "  - {name: c, type: tool}\n"  # tool silos => CODE finding
            "knowledge_sources:\n"
            "  - {path: /etc/source.yaml, inject_as: reference}\n"
            "resolve_file_refs: [doc, attachment]\n"
            "input_schema: {type: object}\n"
            "output_schema: {type: object}\n"
            "default_model_tier: local\n"
        )
        zf = _zip({"configs/workers/fs.yaml": worker_yaml})
        summary = summarize_capabilities(zf, _manifest(workers=["configs/workers/fs.yaml"]))
        fs_findings = [f for f in summary.findings if f.category == CATEGORY_FILESYSTEM]
        # workspace + 2 folder silos + 1 source + resolve_file_refs = 5
        assert len(fs_findings) == 5
        # The tool silo should land in CODE, not FILESYSTEM.
        code_findings = [f for f in summary.findings if f.category == CATEGORY_CODE]
        assert any("knowledge_silo tool" in f.summary for f in code_findings)
        # readwrite silo is distinguished from read-only in the summary text.
        assert any("read+write" in f.summary for f in fs_findings)

    def test_mcp_host_port_flagged_as_network(self):
        mcp_yaml = "name: gateway\nhost: 0.0.0.0\nport: 8765\ntools:\n  - {name: search}\n"
        zf = _zip({"configs/mcp/gw.yaml": mcp_yaml})
        summary = summarize_capabilities(zf, _manifest(mcp=["configs/mcp/gw.yaml"]))
        net_findings = [f for f in summary.findings if f.category == CATEGORY_NETWORK]
        assert len(net_findings) == 1
        assert "0.0.0.0:8765" in net_findings[0].summary
        # Tools list lands in CODE.
        code_findings = [f for f in summary.findings if f.category == CATEGORY_CODE]
        assert any("search" in f.summary for f in code_findings)

    def test_env_var_refs_collected_into_single_finding(self):
        worker_yaml = (
            "name: env\n"
            "system_prompt: hi\n"
            "backend_config:\n"
            "  api_key: ${OPENAI_API_KEY}\n"
            "  bare: $LEGACY_VAR\n"
            "  nested:\n"
            "    - ${ANOTHER_ONE}\n"
            "    - text with ${OPENAI_API_KEY} repeated\n"
            "input_schema: {type: object}\n"
            "output_schema: {type: object}\n"
            "default_model_tier: local\n"
        )
        zf = _zip({"configs/workers/env.yaml": worker_yaml})
        summary = summarize_capabilities(zf, _manifest(workers=["configs/workers/env.yaml"]))
        env_findings = [f for f in summary.findings if f.category == CATEGORY_ENV]
        assert len(env_findings) == 1
        # Three distinct vars; OPENAI_API_KEY was deduped despite two mentions.
        for var in ("OPENAI_API_KEY", "LEGACY_VAR", "ANOTHER_ONE"):
            assert var in env_findings[0].summary

    def test_missing_config_file_logs_and_skips(self):
        """If the manifest references a YAML that's not actually in the ZIP
        (a malformed bundle), the extractor logs and skips rather than
        raising — preview should still render so the operator can cancel.
        """
        zf = _zip({})  # no member at all
        summary = summarize_capabilities(zf, _manifest(workers=["configs/workers/missing.yaml"]))
        assert summary.findings == []

    def test_yaml_parse_error_is_skipped_not_fatal(self):
        """A malformed YAML inside the ZIP must not crash the extractor."""
        zf = _zip({"configs/workers/broken.yaml": "name: ok\n: : invalid\n"})
        summary = summarize_capabilities(zf, _manifest(workers=["configs/workers/broken.yaml"]))
        assert summary.findings == []


class TestSummaryGrouping:
    def test_by_category_keys_are_stable(self):
        worker_yaml = (
            "name: w\n"
            "worker_kind: processor\n"
            "processing_backend: pkg.mod.Backend\n"
            "workspace_dir: /tmp/x\n"
            "input_schema: {type: object}\n"
            "output_schema: {type: object}\n"
            "default_model_tier: local\n"
        )
        zf = _zip({"configs/workers/w.yaml": worker_yaml})
        summary = summarize_capabilities(zf, _manifest(workers=["configs/workers/w.yaml"]))
        grouped = summary.by_category()
        assert set(grouped.keys()) == {CATEGORY_CODE, CATEGORY_FILESYSTEM}
