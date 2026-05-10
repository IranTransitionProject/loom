"""
Tests for heddle.contrib.docproc — document extraction backends.

Tests the MarkItDown and SmartExtractor backends with mocked dependencies.
Docling tests are skipped if torch is not installed (CI-friendly).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from heddle.contrib.docproc.contracts import ExtractorInput, ExtractorOutput

# ---------------------------------------------------------------------------
# Pydantic contracts
# ---------------------------------------------------------------------------


class TestContracts:
    """Test the shared I/O contracts."""

    def test_extractor_input_valid(self):
        inp = ExtractorInput(file_ref="test.pdf")
        assert inp.file_ref == "test.pdf"

    def test_extractor_input_requires_file_ref(self):
        with pytest.raises(ValueError):
            ExtractorInput()

    def test_extractor_output_valid(self):
        out = ExtractorOutput(
            file_ref="test_extracted.json",
            page_count=3,
            has_tables=True,
            sections=["Introduction", "Methods"],
            text_preview="This is a test document...",
        )
        assert out.page_count == 3
        assert out.has_tables is True
        assert len(out.sections) == 2

    def test_extractor_output_schema(self):
        schema = ExtractorOutput.model_json_schema()
        assert "file_ref" in schema["properties"]
        assert "page_count" in schema["properties"]
        assert "has_tables" in schema["properties"]


# ---------------------------------------------------------------------------
# MarkItDown backend
# ---------------------------------------------------------------------------


class TestMarkItDownBackend:
    """Test MarkItDownBackend with mocked MarkItDown library."""

    def test_extraction_success(self, tmp_path):
        """Successful extraction produces expected output structure."""
        from heddle.contrib.docproc.markitdown_backend import MarkItDownBackend

        # Create a source file
        source = tmp_path / "test.pdf"
        source.write_bytes(b"fake pdf content")

        # Mock MarkItDown conversion
        mock_result = MagicMock()
        mock_result.text_content = (
            "# Introduction\n\n"
            "This is the first paragraph.\n\n"
            "## Methods\n\n"
            "| Col1 | Col2 |\n| --- | --- |\n| a | b |\n"
        )

        mock_md_class = MagicMock()
        mock_md_instance = MagicMock()
        mock_md_instance.convert.return_value = mock_result
        mock_md_class.return_value = mock_md_instance

        backend = MarkItDownBackend(workspace_dir=str(tmp_path))

        with patch.dict("sys.modules", {"markitdown": MagicMock(MarkItDown=mock_md_class)}):
            result = backend.process_sync(
                {"file_ref": "test.pdf"},
                {"workspace_dir": str(tmp_path)},
            )

        assert result["model_used"] == "markitdown"
        output = result["output"]
        assert output["page_count"] >= 1
        assert output["has_tables"] is True
        assert "Introduction" in output["sections"]
        assert "Methods" in output["sections"]
        assert len(output["text_preview"]) > 0

        # Verify extracted JSON was written
        extracted_path = tmp_path / "test_extracted.json"
        assert extracted_path.exists()
        data = json.loads(extracted_path.read_text())
        assert "text" in data

    def test_missing_file_raises(self, tmp_path):
        """Missing source file raises FileNotFoundError."""
        from heddle.contrib.docproc.markitdown_backend import MarkItDownBackend

        backend = MarkItDownBackend(workspace_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError):
            backend.process_sync(
                {"file_ref": "nonexistent.pdf"},
                {"workspace_dir": str(tmp_path)},
            )

    def test_path_traversal_raises(self, tmp_path):
        """Path traversal in file_ref is rejected."""
        from heddle.contrib.docproc.markitdown_backend import MarkItDownBackend

        backend = MarkItDownBackend(workspace_dir=str(tmp_path))
        with pytest.raises(ValueError, match="traversal"):
            backend.process_sync(
                {"file_ref": "../../../etc/passwd"},
                {"workspace_dir": str(tmp_path)},
            )

    def test_conversion_error_wrapped(self, tmp_path):
        """MarkItDown errors are wrapped in MarkItDownConversionError."""
        from heddle.contrib.docproc.markitdown_backend import (
            MarkItDownBackend,
            MarkItDownConversionError,
        )

        source = tmp_path / "bad.pdf"
        source.write_bytes(b"corrupt")

        mock_md_class = MagicMock()
        mock_md_instance = MagicMock()
        mock_md_instance.convert.side_effect = RuntimeError("parse failed")
        mock_md_class.return_value = mock_md_instance

        backend = MarkItDownBackend(workspace_dir=str(tmp_path))

        with (
            patch.dict("sys.modules", {"markitdown": MagicMock(MarkItDown=mock_md_class)}),
            pytest.raises(MarkItDownConversionError),
        ):
            backend.process_sync(
                {"file_ref": "bad.pdf"},
                {"workspace_dir": str(tmp_path)},
            )


# ---------------------------------------------------------------------------
# Docling backend
# ---------------------------------------------------------------------------


class TestDoclingBackend:
    """Test DoclingBackend.process_sync — _extract / _build_converter are
    pragma-no-cover (Docling is a heavy, optional, native-deps dependency),
    so these tests focus on the wrapper logic: path resolution, error
    classification, and the response envelope.
    """

    def test_init_uses_default_workspace_dir(self):
        from heddle.contrib.docproc.docling_backend import DoclingBackend

        backend = DoclingBackend()
        assert str(backend.workspace_dir) == "/tmp/docman-workspace"

    def test_init_accepts_custom_workspace_dir(self, tmp_path):
        from heddle.contrib.docproc.docling_backend import DoclingBackend

        backend = DoclingBackend(workspace_dir=str(tmp_path))
        assert backend.workspace_dir == tmp_path

    def test_process_sync_happy_path(self, tmp_path):
        """process_sync returns the _extract result wrapped with model_used."""
        from heddle.contrib.docproc.docling_backend import DoclingBackend

        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4 fake")

        backend = DoclingBackend(workspace_dir=str(tmp_path))
        expected_output = {
            "file_ref": "doc_extracted.json",
            "page_count": 3,
            "has_tables": True,
            "sections": ["Intro"],
            "text_preview": "preview text",
        }
        with patch.object(backend, "_extract", return_value=expected_output) as mock_extract:
            result = backend.process_sync(
                {"file_ref": "doc.pdf"},
                {"workspace_dir": str(tmp_path)},
            )

        assert result == {"output": expected_output, "model_used": "docling"}
        # _extract receives the resolved absolute path, not the raw file_ref.
        called_path = mock_extract.call_args.args[0]
        assert called_path == source.resolve()

    def test_process_sync_uses_constructor_workspace_when_config_omits_it(self, tmp_path):
        """Falls back to self.workspace_dir when config has no workspace_dir."""
        from heddle.contrib.docproc.docling_backend import DoclingBackend

        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4 fake")

        backend = DoclingBackend(workspace_dir=str(tmp_path))
        with patch.object(backend, "_extract", return_value={"file_ref": "x.json"}):
            result = backend.process_sync({"file_ref": "doc.pdf"}, {})  # no workspace_dir
        assert result["model_used"] == "docling"

    def test_process_sync_passes_through_docling_conversion_error(self, tmp_path):
        """DoclingConversionError from _extract propagates unchanged
        (the outer try/except must not re-wrap it).
        """
        from heddle.contrib.docproc.docling_backend import (
            DoclingBackend,
            DoclingConversionError,
        )

        source = tmp_path / "doc.pdf"
        source.write_bytes(b"corrupt")

        inner = DoclingConversionError("inner reason")
        backend = DoclingBackend(workspace_dir=str(tmp_path))
        with (
            patch.object(backend, "_extract", side_effect=inner),
            pytest.raises(DoclingConversionError) as exc_info,
        ):
            backend.process_sync(
                {"file_ref": "doc.pdf"},
                {"workspace_dir": str(tmp_path)},
            )
        # Identity check: pass-through, not a re-wrap.
        assert exc_info.value is inner

    def test_process_sync_wraps_unexpected_exception(self, tmp_path):
        """Any non-DoclingConversionError from _extract is wrapped, with the
        original exception attached via __cause__ so the operator can find
        the underlying failure.
        """
        from heddle.contrib.docproc.docling_backend import (
            DoclingBackend,
            DoclingConversionError,
        )

        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4 fake")

        original = RuntimeError("torch.cuda OOM")
        backend = DoclingBackend(workspace_dir=str(tmp_path))
        with (
            patch.object(backend, "_extract", side_effect=original),
            pytest.raises(DoclingConversionError) as exc_info,
        ):
            backend.process_sync(
                {"file_ref": "doc.pdf"},
                {"workspace_dir": str(tmp_path)},
            )
        assert "doc.pdf" in str(exc_info.value)
        assert exc_info.value.__cause__ is original

    def test_process_sync_rejects_path_traversal(self, tmp_path):
        """WorkspaceManager.resolve raises ValueError for traversal attempts;
        process_sync must surface that without wrapping (no _extract call).
        """
        from heddle.contrib.docproc.docling_backend import DoclingBackend

        backend = DoclingBackend(workspace_dir=str(tmp_path))
        with (
            patch.object(backend, "_extract") as mock_extract,
            pytest.raises(ValueError, match="traversal"),
        ):
            backend.process_sync(
                {"file_ref": "../../../etc/passwd"},
                {"workspace_dir": str(tmp_path)},
            )
        mock_extract.assert_not_called()

    def test_process_sync_raises_for_missing_file(self, tmp_path):
        """WorkspaceManager.resolve raises FileNotFoundError when the source
        does not exist; process_sync surfaces it without invoking _extract.
        """
        from heddle.contrib.docproc.docling_backend import DoclingBackend

        backend = DoclingBackend(workspace_dir=str(tmp_path))
        with (
            patch.object(backend, "_extract") as mock_extract,
            pytest.raises(FileNotFoundError),
        ):
            backend.process_sync(
                {"file_ref": "nonexistent.pdf"},
                {"workspace_dir": str(tmp_path)},
            )
        mock_extract.assert_not_called()


# ---------------------------------------------------------------------------
# SmartExtractor backend
# ---------------------------------------------------------------------------


class TestSmartExtractorBackend:
    """Test SmartExtractorBackend with mocked inner backends."""

    def _make_backend(self, tmp_path):
        from heddle.contrib.docproc.markitdown_backend import MarkItDownBackend
        from heddle.contrib.docproc.smart_extractor import SmartExtractorBackend

        backend = SmartExtractorBackend(workspace_dir=str(tmp_path))
        mock_mit = MagicMock(spec=MarkItDownBackend)
        mock_doc = MagicMock(spec=MarkItDownBackend)  # same interface
        backend._markitdown = mock_mit
        backend._docling = mock_doc
        return backend, mock_mit, mock_doc

    def test_markitdown_accepted(self, tmp_path):
        """When MarkItDown returns enough text, it's accepted."""
        backend, mock_mit, _mock_doc = self._make_backend(tmp_path)
        mock_mit.process_sync.return_value = {
            "output": {
                "text_preview": (
                    "This is a sufficiently long text preview that should easily "
                    "pass the default minimum text length threshold of fifty characters."
                ),
            },
            "model_used": "markitdown",
        }
        result = backend.process_sync({"file_ref": "test.pdf"}, {})
        assert result["model_used"] == "markitdown"

    def test_fallback_on_short_text(self, tmp_path):
        """When MarkItDown returns too little text, falls back to Docling."""
        backend, mock_mit, mock_doc = self._make_backend(tmp_path)
        mock_mit.process_sync.return_value = {
            "output": {"text_preview": "x"},
            "model_used": "markitdown",
        }
        mock_doc.process_sync.return_value = {
            "output": {"text_preview": "Full OCR text from Docling."},
            "model_used": "docling",
        }
        result = backend.process_sync({"file_ref": "scan.pdf"}, {})
        assert result["model_used"] == "docling"
        mock_doc.process_sync.assert_called_once()

    def test_fallback_on_error(self, tmp_path):
        """When MarkItDown errors, falls back to Docling."""
        backend, mock_mit, mock_doc = self._make_backend(tmp_path)
        mock_mit.process_sync.side_effect = RuntimeError("crash")
        mock_doc.process_sync.return_value = {
            "output": {"text_preview": "Docling output"},
            "model_used": "docling",
        }
        result = backend.process_sync({"file_ref": "test.pdf"}, {})
        assert result["model_used"] == "docling"

    def test_force_docling_extension(self, tmp_path):
        """Extensions in force_docling_extensions bypass MarkItDown."""
        backend, mock_mit, mock_doc = self._make_backend(tmp_path)
        mock_doc.process_sync.return_value = {
            "output": {"text_preview": "Forced Docling"},
            "model_used": "docling",
        }
        result = backend.process_sync(
            {"file_ref": "scan.tiff"},
            {"force_docling_extensions": [".tiff"]},
        )
        assert result["model_used"] == "docling"
        mock_mit.process_sync.assert_not_called()

    def test_custom_min_text_length(self, tmp_path):
        """Custom min_text_length threshold is respected."""
        backend, mock_mit, _mock_doc = self._make_backend(tmp_path)
        mock_mit.process_sync.return_value = {
            "output": {"text_preview": "Short but enough for threshold"},
            "model_used": "markitdown",
        }
        result = backend.process_sync({"file_ref": "test.pdf"}, {"min_text_length": 10})
        assert result["model_used"] == "markitdown"

    def test_lazy_init(self):
        """Inner backends are not created until first use."""
        from heddle.contrib.docproc.smart_extractor import SmartExtractorBackend

        backend = SmartExtractorBackend()
        assert backend._markitdown is None
        assert backend._docling is None


# ---------------------------------------------------------------------------
# Worker config validation (smoke tests for shipped configs)
# ---------------------------------------------------------------------------


class TestShippedWorkerConfigs:
    """Validate that new shipped worker configs pass validation."""

    @pytest.fixture
    def configs_dir(self):
        return Path(__file__).parents[3] / "configs" / "workers"

    def _validate_config(self, path: Path) -> list[str]:
        import yaml

        from heddle.core.config import validate_worker_config

        with path.open() as f:
            config = yaml.safe_load(f)
        return validate_worker_config(config, path=str(path))

    def test_translator_config(self, configs_dir):
        errors = self._validate_config(configs_dir / "translator.yaml")
        assert errors == [], errors

    def test_qa_config(self, configs_dir):
        errors = self._validate_config(configs_dir / "qa.yaml")
        assert errors == [], errors

    def test_reviewer_config(self, configs_dir):
        errors = self._validate_config(configs_dir / "reviewer.yaml")
        assert errors == [], errors
