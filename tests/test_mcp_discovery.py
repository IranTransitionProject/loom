"""Tests for heddle.mcp.discovery — tool definition generation."""

import os

import yaml

from heddle.mcp.discovery import (
    discover_pipeline_tools,
    discover_query_tools,
    discover_worker_tools,
    make_tool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(dir_path: str, filename: str, data: dict) -> str:
    path = os.path.join(dir_path, filename)
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# make_tool
# ---------------------------------------------------------------------------


class TestMakeTool:
    def test_basic(self):
        tool = make_tool("my_tool", "Does stuff", {"type": "object"})
        assert tool["name"] == "my_tool"
        assert tool["description"] == "Does stuff"
        assert tool["inputSchema"] == {"type": "object"}


# ---------------------------------------------------------------------------
# Worker tools
# ---------------------------------------------------------------------------


class TestDiscoverWorkerTools:
    def test_basic_worker(self, tmp_path):
        worker_cfg = {
            "name": "summarizer",
            "system_prompt": "Summarize the input document.\nBe concise.",
            "input_schema": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                },
            },
            "default_model_tier": "standard",
            "timeout_seconds": 120,
        }
        config_path = _write_yaml(str(tmp_path), "summarizer.yaml", worker_cfg)

        entries = [{"config": config_path}]
        tools = discover_worker_tools(entries)

        assert len(tools) == 1
        tool = tools[0]
        assert tool["name"] == "summarizer"
        assert tool["description"] == "Summarize the input document."
        assert tool["inputSchema"]["required"] == ["text"]

        # Verify _heddle metadata.
        heddle_meta_val = tool["_heddle"]
        assert heddle_meta_val["kind"] == "worker"
        assert heddle_meta_val["worker_type"] == "summarizer"
        assert heddle_meta_val["tier"] == "standard"
        assert heddle_meta_val["timeout"] == 120

    def test_name_and_description_override(self, tmp_path):
        worker_cfg = {
            "name": "classifier",
            "system_prompt": "Original description.",
            "input_schema": {"type": "object"},
        }
        config_path = _write_yaml(str(tmp_path), "classifier.yaml", worker_cfg)

        entries = [
            {
                "config": config_path,
                "name": "classify_document",
                "description": "Custom description",
                "tier": "local",
            }
        ]
        tools = discover_worker_tools(entries)

        assert tools[0]["name"] == "classify_document"
        assert tools[0]["description"] == "Custom description"
        assert tools[0]["_heddle"]["tier"] == "local"

    def test_description_from_worker_config(self, tmp_path):
        """Worker config 'description' field is used when no MCP override."""
        worker_cfg = {
            "name": "summarizer",
            "description": "Compresses text to a structured summary.",
            "system_prompt": "You are a text summarizer.\nBe concise.",
            "input_schema": {"type": "object"},
        }
        config_path = _write_yaml(str(tmp_path), "summarizer.yaml", worker_cfg)

        entries = [{"config": config_path}]
        tools = discover_worker_tools(entries)

        # Should use 'description' not first line of system_prompt.
        assert tools[0]["description"] == "Compresses text to a structured summary."

    def test_missing_config_skips_worker(self):
        entries = [{"config": "/nonexistent/worker.yaml"}]
        tools = discover_worker_tools(entries)
        assert tools == []

    def test_bare_schema_gets_wrapped(self, tmp_path):
        """If input_schema lacks 'type', it gets wrapped as properties."""
        worker_cfg = {
            "name": "bare",
            "system_prompt": "Test",
            "input_schema": {
                "text": {"type": "string"},
            },
        }
        config_path = _write_yaml(str(tmp_path), "bare.yaml", worker_cfg)
        entries = [{"config": config_path}]
        tools = discover_worker_tools(entries)

        schema = tools[0]["inputSchema"]
        assert schema["type"] == "object"
        assert "text" in schema["properties"]


# ---------------------------------------------------------------------------
# Pipeline tools
# ---------------------------------------------------------------------------


class TestDiscoverPipelineTools:
    def test_basic_pipeline(self, tmp_path):
        pipeline_cfg = {
            "name": "doc_pipeline",
            "pipeline_stages": [
                {
                    "name": "extract",
                    "worker_type": "doc_extractor",
                    "input_mapping": {
                        "file_ref": "goal.context.file_ref",
                    },
                },
                {
                    "name": "classify",
                    "worker_type": "doc_classifier",
                    "input_mapping": {
                        "text": "extract.output.text",
                    },
                },
            ],
            "timeout_seconds": 600,
        }
        config_path = _write_yaml(str(tmp_path), "pipeline.yaml", pipeline_cfg)

        entries = [
            {
                "config": config_path,
                "name": "process_document",
                "description": "Full doc pipeline",
            }
        ]
        tools = discover_pipeline_tools(entries)

        assert len(tools) == 1
        tool = tools[0]
        assert tool["name"] == "process_document"
        assert tool["description"] == "Full doc pipeline"

        # Input schema should derive from goal.context.* references.
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "file_ref" in schema["properties"]
        assert "file_ref" in schema["required"]

        heddle_meta_val = tool["_heddle"]
        assert heddle_meta_val["kind"] == "pipeline"
        assert heddle_meta_val["timeout"] == 600

    def test_multi_stage_context_fields(self, tmp_path):
        """goal.context.* refs in later stages should also appear in schema."""
        pipeline_cfg = {
            "name": "multi",
            "pipeline_stages": [
                {
                    "name": "stage1",
                    "worker_type": "w1",
                    "input_mapping": {"file_ref": "goal.context.file_ref"},
                },
                {
                    "name": "stage2",
                    "worker_type": "w2",
                    "input_mapping": {
                        "text": "stage1.output.text",
                        "lang": "goal.context.lang",
                    },
                },
            ],
        }
        config_path = _write_yaml(str(tmp_path), "multi.yaml", pipeline_cfg)
        entries = [{"config": config_path, "name": "multi_tool"}]
        tools = discover_pipeline_tools(entries)

        schema = tools[0]["inputSchema"]
        assert "file_ref" in schema["properties"]
        assert "lang" in schema["properties"]

    def test_no_stages_returns_open_schema(self, tmp_path):
        pipeline_cfg = {"name": "empty", "pipeline_stages": []}
        config_path = _write_yaml(str(tmp_path), "empty.yaml", pipeline_cfg)
        entries = [{"config": config_path, "name": "empty_tool"}]
        tools = discover_pipeline_tools(entries)
        assert tools[0]["inputSchema"] == {"type": "object"}

    def test_missing_config_skips(self):
        entries = [{"config": "/nonexistent/pipeline.yaml", "name": "missing"}]
        tools = discover_pipeline_tools(entries)
        assert tools == []

    def test_gateway_configs_dir_resolves_worker_schema(self, tmp_path, monkeypatch):
        """F3: ``gateway_configs_dir`` lets discovery find worker configs
        when the pipeline-anchored path doesn't.

        Earlier the second candidate was ``configs/workers/...``
        resolved against CWD — under Claude Desktop / Cursor (CWD ≈
        ``~``) that became ``~/configs/workers/...``, never existed,
        and every property type silently degraded to ``string``.
        The new ``gateway_configs_dir`` parameter anchors lookup on
        the gateway YAML's directory so non-standard layouts still
        resolve.
        """
        # Build a non-standard layout: pipeline at ``/tmp/mcp/p.yaml``
        # but the worker config lives at ``{gateway_dir}/workers/
        # extractor.yaml`` rather than ``../workers/extractor.yaml``
        # relative to the pipeline.
        gateway_dir = tmp_path / "gateway"
        gateway_dir.mkdir()
        workers_dir = gateway_dir / "workers"
        workers_dir.mkdir()
        worker_cfg = {
            "name": "extractor",
            "input_schema": {
                "type": "object",
                "properties": {
                    "page_count": {"type": "integer", "minimum": 1},
                    "file_ref": {"type": "string"},
                },
            },
        }
        _write_yaml(str(workers_dir), "extractor.yaml", worker_cfg)

        pipeline_dir = tmp_path / "elsewhere"
        pipeline_dir.mkdir()
        pipeline_cfg = {
            "name": "with_int_field",
            "pipeline_stages": [
                {
                    "name": "extract",
                    "worker_type": "extractor",
                    "input_mapping": {
                        "page_count": "goal.context.page_count",
                    },
                },
            ],
        }
        pipeline_path = _write_yaml(str(pipeline_dir), "pipeline.yaml", pipeline_cfg)

        entries = [{"config": pipeline_path, "name": "do_thing"}]

        # Without gateway_configs_dir, the pipeline-anchored fallback
        # looks at ``tmp_path/workers/extractor.yaml`` which doesn't
        # exist → schema degrades to default ``string``.
        tools_without = discover_pipeline_tools(entries)
        assert tools_without[0]["inputSchema"]["properties"]["page_count"] == {
            "type": "string",
        }

        # With gateway_configs_dir, the real schema is found.
        tools_with = discover_pipeline_tools(entries, gateway_configs_dir=str(gateway_dir))
        page_prop = tools_with[0]["inputSchema"]["properties"]["page_count"]
        assert page_prop["type"] == "integer"
        assert page_prop["minimum"] == 1

    def test_cwd_fallback_no_longer_used(self, tmp_path, monkeypatch):
        """F3: ``configs/workers/...`` is no longer resolved against CWD.

        Pins the regression: even if a worker config exists at
        ``{cwd}/configs/workers/{worker_type}.yaml``, the discovery
        loader must not pick it up — that path resolved to
        ``~/configs/workers/...`` under MCP hosts and is the bug.
        """
        # Build a config tree at tmp_path that, if CWD-relative
        # resolution were still in effect, would resolve.
        bad_cwd = tmp_path / "bad_cwd"
        (bad_cwd / "configs" / "workers").mkdir(parents=True)
        # An "extractor" worker config at the CWD-relative path with
        # a distinctive schema we can detect.
        bait_cfg = {
            "name": "extractor",
            "input_schema": {
                "properties": {"page_count": {"type": "boolean"}},
            },
        }
        _write_yaml(
            str(bad_cwd / "configs" / "workers"),
            "extractor.yaml",
            bait_cfg,
        )

        # Pipeline at an unrelated path; pipeline-anchored fallback
        # also can't resolve.
        pipeline_dir = tmp_path / "elsewhere2"
        pipeline_dir.mkdir()
        pipeline_cfg = {
            "name": "p",
            "pipeline_stages": [
                {
                    "name": "extract",
                    "worker_type": "extractor",
                    "input_mapping": {"page_count": "goal.context.page_count"},
                },
            ],
        }
        pipeline_path = _write_yaml(str(pipeline_dir), "p.yaml", pipeline_cfg)

        # Run with CWD set to ``bad_cwd``.  If the old fallback were
        # still in effect, ``configs/workers/extractor.yaml`` would
        # resolve and we'd see ``type: boolean`` — instead we should
        # see the default string.
        monkeypatch.chdir(bad_cwd)
        tools = discover_pipeline_tools([{"config": pipeline_path, "name": "p_tool"}])
        page_prop = tools[0]["inputSchema"]["properties"]["page_count"]
        assert page_prop == {"type": "string"}


# ---------------------------------------------------------------------------
# Query tools
# ---------------------------------------------------------------------------


class _MockQueryBackend:
    """Minimal mock of a query backend for testing discovery."""

    table_name = "documents"
    filter_fields = {
        "doc_type": "doc_type = ?",
        "has_tables": "has_tables::BOOLEAN",
        "page_count": "page_count >= ?",
    }
    stats_groups = {"doc_type", "status"}
    id_column = "doc_id"

    def _get_handlers(self):
        return {
            "search": self._search,
            "filter": self._filter,
            "stats": self._stats,
            "get": self._get,
        }

    def _search(self, **kw):
        pass

    def _filter(self, **kw):
        pass

    def _stats(self, **kw):
        pass

    def _get(self, **kw):
        pass


class TestDiscoverQueryTools:
    def test_basic_query_tools(self, monkeypatch):
        """Test query tool discovery with a mock backend."""
        # Monkeypatch _instantiate_backend to return our mock.
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _MockQueryBackend())

        entries = [
            {
                "backend": "mock.MockQueryBackend",
                "backend_config": {},
                "actions": ["search", "filter", "stats", "get"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)

        names = {t["name"] for t in tools}
        assert names == {"docs_search", "docs_filter", "docs_stats", "docs_get"}

    def test_search_schema(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _MockQueryBackend())

        entries = [
            {
                "backend": "mock.MockQueryBackend",
                "backend_config": {},
                "actions": ["search"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)
        schema = tools[0]["inputSchema"]
        assert schema["required"] == ["query"]
        assert "query" in schema["properties"]

    def test_filter_schema_infers_types(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _MockQueryBackend())

        entries = [
            {
                "backend": "mock.MockQueryBackend",
                "backend_config": {},
                "actions": ["filter"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)
        schema = tools[0]["inputSchema"]

        # has_tables should be boolean (BOOLEAN in SQL)
        assert schema["properties"]["has_tables"]["type"] == "boolean"
        # page_count should be integer (>= in SQL)
        assert schema["properties"]["page_count"]["type"] == "integer"
        # doc_type should be string (default)
        assert schema["properties"]["doc_type"]["type"] == "string"

    def test_stats_schema(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _MockQueryBackend())

        entries = [
            {
                "backend": "mock.MockQueryBackend",
                "backend_config": {},
                "actions": ["stats"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)
        schema = tools[0]["inputSchema"]
        assert "group_by" in schema["properties"]

    def test_get_schema_uses_id_column(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _MockQueryBackend())

        entries = [
            {
                "backend": "mock.MockQueryBackend",
                "backend_config": {},
                "actions": ["get"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)
        schema = tools[0]["inputSchema"]
        assert "doc_id" in schema["required"]
        assert "doc_id" in schema["properties"]

    def test_unknown_action_skipped(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _MockQueryBackend())

        entries = [
            {
                "backend": "mock.MockQueryBackend",
                "backend_config": {},
                "actions": ["search", "nonexistent"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)
        assert len(tools) == 1
        assert tools[0]["name"] == "docs_search"

    def test_backend_instantiation_failure_skips(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: None)

        entries = [
            {
                "backend": "bad.Backend",
                "backend_config": {},
                "actions": ["search"],
                "name_prefix": "bad",
            }
        ]
        tools = discover_query_tools(entries)
        assert tools == []

    def test_heddle_metadata(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _MockQueryBackend())

        entries = [
            {
                "backend": "mock.MockQueryBackend",
                "backend_config": {"db_path": "/tmp/test.db"},
                "actions": ["search"],
                "name_prefix": "docs",
                "worker_type": "docs_query",
                "timeout": 45,
            }
        ]
        tools = discover_query_tools(entries)
        heddle_meta_val = tools[0]["_heddle"]
        assert heddle_meta_val["kind"] == "query"
        assert heddle_meta_val["worker_type"] == "docs_query"
        assert heddle_meta_val["action"] == "search"
        assert heddle_meta_val["timeout"] == 45


# ---------------------------------------------------------------------------
# Coverage: _pipeline_entry_schema — no context fields (line 175)
# ---------------------------------------------------------------------------


class TestPipelineEntrySchemaNoContextFields:
    def test_stages_without_goal_context_refs(self, tmp_path):
        """Stages whose input_mapping has no goal.context.* refs → open schema."""
        pipeline_cfg = {
            "name": "no_ctx",
            "pipeline_stages": [
                {
                    "name": "stage1",
                    "worker_type": "w1",
                    "input_mapping": {
                        "text": "previous_stage.output.text",
                    },
                },
            ],
        }
        config_path = _write_yaml(str(tmp_path), "no_ctx.yaml", pipeline_cfg)
        entries = [{"config": config_path, "name": "no_ctx_tool"}]
        tools = discover_pipeline_tools(entries)
        assert tools[0]["inputSchema"] == {"type": "object"}


# ---------------------------------------------------------------------------
# Coverage: _load_stage_property_types — empty worker_type (line 207)
# ---------------------------------------------------------------------------


class TestLoadStagePropertyTypes:
    def test_no_worker_type_returns_empty(self, tmp_path):
        """Stage with no worker_type → property types default to string."""
        pipeline_cfg = {
            "name": "no_wt",
            "pipeline_stages": [
                {
                    "name": "stage1",
                    "worker_type": "",
                    "input_mapping": {
                        "file_ref": "goal.context.file_ref",
                    },
                },
            ],
        }
        config_path = _write_yaml(str(tmp_path), "no_wt.yaml", pipeline_cfg)
        entries = [{"config": config_path, "name": "no_wt_tool"}]
        tools = discover_pipeline_tools(entries)

        schema = tools[0]["inputSchema"]
        assert schema["properties"]["file_ref"] == {"type": "string"}

    def test_worker_config_found_enriches_types(self, tmp_path):
        """When worker YAML is loadable, its property types enrich the schema."""
        # Create directory structure: workers/my_worker.yaml
        workers_dir = tmp_path / "workers"
        workers_dir.mkdir()
        worker_cfg = {
            "name": "my_worker",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_ref": {"type": "string", "description": "Path to the file"},
                    "count": {"type": "integer"},
                },
            },
        }
        _write_yaml(str(workers_dir), "my_worker.yaml", worker_cfg)

        # Pipeline config sits in orchestrators/ (sibling of workers/)
        orch_dir = tmp_path / "orchestrators"
        orch_dir.mkdir()
        pipeline_cfg = {
            "name": "enriched",
            "pipeline_stages": [
                {
                    "name": "stage1",
                    "worker_type": "my_worker",
                    "input_mapping": {
                        "file_ref": "goal.context.file_ref",
                    },
                },
            ],
        }
        config_path = _write_yaml(str(orch_dir), "enriched.yaml", pipeline_cfg)
        entries = [{"config": config_path, "name": "enriched_tool"}]
        tools = discover_pipeline_tools(entries)

        schema = tools[0]["inputSchema"]
        # Should pick up the richer type from the worker config
        assert schema["properties"]["file_ref"]["type"] == "string"
        assert "description" in schema["properties"]["file_ref"]


# ---------------------------------------------------------------------------
# Coverage: _instantiate_backend — all error branches (lines 301-321)
# ---------------------------------------------------------------------------


class TestInstantiateBackend:
    def test_no_dot_in_path_returns_none(self):
        from heddle.mcp.discovery import _instantiate_backend

        result = _instantiate_backend("NoDotPath", {})
        assert result is None

    def test_import_error_returns_none(self):
        from heddle.mcp.discovery import _instantiate_backend

        result = _instantiate_backend("nonexistent.module.ClassName", {})
        assert result is None

    def test_class_not_found_returns_none(self):
        from heddle.mcp.discovery import _instantiate_backend

        # os module exists but has no class named 'FakeClassName'
        result = _instantiate_backend("os.FakeClassName", {})
        assert result is None

    def test_init_failure_returns_none(self):
        from heddle.mcp.discovery import _instantiate_backend

        # int() with bad kwargs will raise TypeError
        result = _instantiate_backend("builtins.int", {"bad_kwarg": "value"})
        assert result is None


# ---------------------------------------------------------------------------
# Coverage: backend without _get_handlers (lines 262-268)
# ---------------------------------------------------------------------------


class _NoHandlersBackend:
    """Backend that lacks the _get_handlers method."""

    pass


class TestQueryNoHandlers:
    def test_backend_without_get_handlers_skipped(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _NoHandlersBackend())

        entries = [
            {
                "backend": "mock.NoHandlers",
                "backend_config": {},
                "actions": ["search"],
                "name_prefix": "bad",
            }
        ]
        tools = discover_query_tools(entries)
        assert tools == []


# ---------------------------------------------------------------------------
# Coverage: _query_action_schema — vector_search + unknown action (lines 388-399)
# ---------------------------------------------------------------------------


class _VectorBackend:
    """Mock backend that supports vector_search."""

    table_name = "documents"
    filter_fields = {}
    stats_groups = set()
    id_column = "id"

    def _get_handlers(self):
        return {
            "vector_search": lambda: None,
            "custom_action": lambda: None,
        }


class TestQueryActionSchemaVectorAndUnknown:
    def test_vector_search_schema(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _VectorBackend())

        entries = [
            {
                "backend": "mock.VectorBackend",
                "backend_config": {},
                "actions": ["vector_search"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)
        schema = tools[0]["inputSchema"]
        assert schema["required"] == ["query"]
        assert "query" in schema["properties"]
        assert "limit" in schema["properties"]

    def test_unknown_action_schema_fallback(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _VectorBackend())

        entries = [
            {
                "backend": "mock.VectorBackend",
                "backend_config": {},
                "actions": ["custom_action"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)
        schema = tools[0]["inputSchema"]
        assert schema == {"type": "object"}


# ---------------------------------------------------------------------------
# Coverage: stats schema with no groups (line 371->376)
# ---------------------------------------------------------------------------


class _NoStatsGroupsBackend:
    table_name = "documents"
    filter_fields = {}
    stats_groups = set()
    id_column = "id"

    def _get_handlers(self):
        return {"stats": lambda: None}


class TestStatsSchemaNoGroups:
    def test_stats_schema_empty_groups(self, monkeypatch):
        import heddle.mcp.discovery as disc

        monkeypatch.setattr(disc, "_instantiate_backend", lambda path, cfg: _NoStatsGroupsBackend())

        entries = [
            {
                "backend": "mock.NoStatsGroups",
                "backend_config": {},
                "actions": ["stats"],
                "name_prefix": "docs",
            }
        ]
        tools = discover_query_tools(entries)
        schema = tools[0]["inputSchema"]
        # No groups → no group_by property
        assert "group_by" not in schema.get("properties", {})


# ---------------------------------------------------------------------------
# Coverage: _first_line — empty/whitespace-only text (lines 411->409, 413)
# ---------------------------------------------------------------------------


class TestFirstLine:
    def test_empty_string(self):
        from heddle.mcp.discovery import _first_line

        assert _first_line("") == ""

    def test_whitespace_only(self):
        from heddle.mcp.discovery import _first_line

        assert _first_line("   \n\n  \n") == ""

    def test_leading_blank_lines(self):
        from heddle.mcp.discovery import _first_line

        assert _first_line("\n\n  Hello world\nSecond") == "Hello world"
