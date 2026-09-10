"""Tests for the local knowledge index (Phase 1 of the self-learning
scope, worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md).

All tests inject a fake ``embed_fn`` — never load the real
sentence-transformers model or hit the network, matching this
codebase's existing convention of not letting tests depend on
expensive external calls. ``get_embedder()``/``embed_texts()`` (the
real, model-backed path) are exercised structurally (imports work,
signature is correct) but not invoked with a real model here — the
real model download/first-index happens on deployment, per this
session's established "code-complete vs. deployed are different
states" gate.
"""

import asyncio
import hashlib
from pathlib import Path

import numpy as np
import pytest

from regime_platform.services.self_learning import knowledge_index as ki


# ─── Fake embedder — deterministic, content-derived vectors ─────────────
#
# Hashes each text to a fixed-dim vector so identical text ALWAYS
# produces identical vectors (needed to test the content-hash skip
# logic meaningfully) and different text produces different vectors,
# without needing any real semantic model.

def _fake_embed(texts: list[str]) -> np.ndarray:
    dim = ki.EMBEDDING_DIM
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for i, text in enumerate(texts):
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # Repeat the 32-byte digest to fill `dim` floats in [0, 1).
        seed = np.frombuffer((h * ((dim // len(h)) + 1))[:dim], dtype=np.uint8).astype(np.float32)
        vec = seed / 255.0
        norm = np.linalg.norm(vec)
        out[i] = vec / norm if norm > 0 else vec
    return out


class TestFixedWindowChunks:
    def test_short_text_is_one_chunk(self):
        assert ki._fixed_window_chunks("hello world") == ["hello world"]

    def test_empty_text_is_no_chunks(self):
        assert ki._fixed_window_chunks("   \n  ") == []

    def test_long_text_splits_with_overlap(self):
        text = "x" * (ki.MAX_CHUNK_CHARS * 3)
        chunks = ki._fixed_window_chunks(text)
        assert len(chunks) > 1
        # Every char of the original must appear in some chunk (no gaps).
        assert sum(len(c) for c in chunks) >= len(text)


class TestChunkPythonSource:
    def test_splits_by_top_level_def(self):
        src = (
            "import os\n\n"
            "def foo():\n    return 1\n\n\n"
            "def bar():\n    return 2\n"
        )
        chunks = ki._chunk_python_source(src)
        assert any("def foo" in c for c in chunks)
        assert any("def bar" in c for c in chunks)
        # foo and bar must be in DIFFERENT chunks, not merged.
        foo_chunk = next(c for c in chunks if "def foo" in c)
        assert "def bar" not in foo_chunk

    def test_splits_by_class(self):
        src = "class A:\n    def m(self):\n        pass\n\nclass B:\n    pass\n"
        chunks = ki._chunk_python_source(src)
        assert any("class A" in c for c in chunks)
        assert any("class B" in c for c in chunks)

    def test_syntax_error_falls_back_to_fixed_window(self):
        src = "def broken(:\n    this is not python\n"
        chunks = ki._chunk_python_source(src)
        assert chunks == ki._fixed_window_chunks(src)

    def test_no_top_level_defs_falls_back(self):
        src = "X = 1\nY = 2\n"
        chunks = ki._chunk_python_source(src)
        assert chunks == ki._fixed_window_chunks(src)

    def test_preamble_kept_as_its_own_chunk(self):
        src = (
            '"""Module docstring explaining WHY this exists."""\n'
            "import os\n\n"
            "def foo():\n    return 1\n"
        )
        chunks = ki._chunk_python_source(src)
        assert any("Module docstring" in c for c in chunks)


class TestChunkMarkdown:
    def test_splits_on_headings(self):
        text = "# Title\n\nintro\n\n## Section 1\n\nbody 1\n\n## Section 2\n\nbody 2\n"
        chunks = ki._chunk_markdown(text)
        assert any("Section 1" in c and "body 1" in c for c in chunks)
        assert any("Section 2" in c and "body 2" in c for c in chunks)
        # Section 1's body must not leak into Section 2's chunk.
        s1 = next(c for c in chunks if "Section 1" in c)
        assert "body 2" not in s1

    def test_no_headings_falls_back_to_fixed_window(self):
        text = "just a paragraph with no headings at all\n" * 5
        chunks = ki._chunk_markdown(text)
        assert chunks == ki._fixed_window_chunks(text)


# ─── build_index / semantic_search integration (fake embedder, temp dirs) ──


def _write(root: Path, rel_path: str, content: str) -> None:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestBuildIndexAndSearch:
    def _build(self, root: Path, db_path: Path, **kwargs):
        return asyncio.run(ki.build_index(
            repo_root=root, db_path=db_path, embed_fn=_fake_embed,
            include_roadmap=False, **kwargs,
        ))

    def test_indexes_code_and_worklog_files(self, tmp_path):
        _write(tmp_path, "regime_platform/foo.py", "def foo():\n    return 1\n")
        _write(tmp_path, "worklog/20260101_TEST.md", "# Test\n\nSome finding.\n")
        db_path = tmp_path / "index.sqlite"

        summary = self._build(
            tmp_path, db_path,
            code_roots=("regime_platform",), worklog_dir="worklog",
        )
        assert summary["chunks_embedded"] >= 2
        assert summary["errors"] == 0

        results = ki.semantic_search("foo", k=5, db_path=db_path, embed_fn=_fake_embed)
        assert any("def foo" in r.text for r in results)

    def test_rerun_with_no_changes_embeds_nothing_new(self, tmp_path):
        _write(tmp_path, "regime_platform/foo.py", "def foo():\n    return 1\n")
        db_path = tmp_path / "index.sqlite"

        first = self._build(tmp_path, db_path, code_roots=("regime_platform",), worklog_dir="nope")
        assert first["chunks_embedded"] > 0

        second = self._build(tmp_path, db_path, code_roots=("regime_platform",), worklog_dir="nope")
        assert second["chunks_embedded"] == 0
        assert second["chunks_seen"] == first["chunks_seen"]

    def test_changed_file_only_reembeds_changed_chunk(self, tmp_path):
        _write(
            tmp_path, "regime_platform/foo.py",
            "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n",
        )
        db_path = tmp_path / "index.sqlite"
        first = self._build(tmp_path, db_path, code_roots=("regime_platform",), worklog_dir="nope")
        assert first["chunks_embedded"] >= 2

        # Change only foo's body — bar is untouched.
        _write(
            tmp_path, "regime_platform/foo.py",
            "def foo():\n    return 999\n\n\ndef bar():\n    return 2\n",
        )
        second = self._build(tmp_path, db_path, code_roots=("regime_platform",), worklog_dir="nope")
        assert second["chunks_embedded"] == 1  # only foo's chunk changed

    def test_deleted_file_is_pruned(self, tmp_path):
        _write(tmp_path, "regime_platform/foo.py", "def foo():\n    return 1\n")
        _write(tmp_path, "regime_platform/bar.py", "def bar():\n    return 2\n")
        db_path = tmp_path / "index.sqlite"
        self._build(tmp_path, db_path, code_roots=("regime_platform",), worklog_dir="nope")

        (tmp_path / "regime_platform" / "bar.py").unlink()
        summary = self._build(tmp_path, db_path, code_roots=("regime_platform",), worklog_dir="nope")
        assert summary["chunks_pruned"] >= 1

        results = ki.semantic_search("bar", k=10, db_path=db_path, embed_fn=_fake_embed)
        assert not any("def bar" in r.text for r in results)

    def test_search_respects_source_type_filter(self, tmp_path):
        _write(tmp_path, "regime_platform/foo.py", "def foo():\n    return 1\n")
        _write(tmp_path, "worklog/20260101_TEST.md", "# Test\n\nfoo related finding.\n")
        db_path = tmp_path / "index.sqlite"
        self._build(tmp_path, db_path, code_roots=("regime_platform",), worklog_dir="worklog")

        code_only = ki.semantic_search(
            "foo", k=10, source_type="code", db_path=db_path, embed_fn=_fake_embed,
        )
        assert all(r.source_type == "code" for r in code_only)

    def test_search_on_empty_index_returns_empty(self, tmp_path):
        db_path = tmp_path / "index.sqlite"
        ki._connect(db_path).close()  # create empty schema, no rows
        results = ki.semantic_search("anything", db_path=db_path, embed_fn=_fake_embed)
        assert results == []

    def test_search_ranks_more_similar_text_higher(self, tmp_path):
        _write(tmp_path, "regime_platform/a.py", "def apple_pie():\n    return 'apple'\n")
        _write(tmp_path, "regime_platform/b.py", "def banana_split():\n    return 'banana'\n")
        db_path = tmp_path / "index.sqlite"
        self._build(tmp_path, db_path, code_roots=("regime_platform",), worklog_dir="nope")

        # The fake embedder is content-hash-derived, not semantic — so
        # query with the EXACT stored chunk text (stripped, per
        # _chunk_python_source), which must score highest against itself
        # (cosine similarity of a vector with itself is 1.0).
        target_text = "def apple_pie():\n    return 'apple'"
        results = ki.semantic_search(target_text, k=2, db_path=db_path, embed_fn=_fake_embed)
        assert results[0].text.strip() == target_text.strip()
        assert results[0].score == pytest.approx(1.0, abs=1e-4)


class TestFetchRoadmapChunks:
    def test_best_effort_returns_empty_on_failure(self, monkeypatch):
        mock_settings = type("S", (), {"SUPABASE_URL": "", "SUPABASE_KEY": ""})()
        monkeypatch.setattr(
            "regime_platform.core.settings.settings", mock_settings,
        )
        result = asyncio.run(ki._fetch_roadmap_chunks())
        assert result == []

    def test_builds_one_chunk_group_per_mechanism(self, monkeypatch):
        from unittest.mock import AsyncMock, patch

        class _FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return [
                    {"mechanism": "foo_mechanism", "status": "resolved",
                     "current_state": "built", "notes": "some notes"},
                ]

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None):
                return _FakeResp()

        mock_settings = type("S", (), {
            "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "k",
        })()
        with (
            patch("regime_platform.core.settings.settings", mock_settings),
            patch("httpx.AsyncClient", return_value=_FakeClient()),
        ):
            result = asyncio.run(ki._fetch_roadmap_chunks())
        assert len(result) == 1
        assert result[0].path == "nt_roadmap:foo_mechanism"
        assert result[0].source_type == "roadmap_note"
        assert "foo_mechanism" in result[0].text


class TestOrchestratorReindexWiring:
    """_run_knowledge_reindex is a thin, best-effort wrapper around
    build_index -- same shape as _run_auto_tuning's own wrapper."""

    def _orch(self):
        from unittest.mock import AsyncMock
        from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator

        return object.__new__(SweepOrchestrator)

    def test_calls_build_index(self):
        from unittest.mock import AsyncMock, patch

        build_mock = AsyncMock(return_value={"chunks_embedded": 3})
        orch = self._orch()
        with patch(
            "regime_platform.services.self_learning.knowledge_index.build_index",
            new=build_mock,
        ):
            asyncio.run(orch._run_knowledge_reindex())
        build_mock.assert_awaited_once()

    def test_failure_is_best_effort_never_raises(self):
        from unittest.mock import AsyncMock, patch

        build_mock = AsyncMock(side_effect=RuntimeError("boom"))
        orch = self._orch()
        with patch(
            "regime_platform.services.self_learning.knowledge_index.build_index",
            new=build_mock,
        ):
            asyncio.run(orch._run_knowledge_reindex())  # must not raise
