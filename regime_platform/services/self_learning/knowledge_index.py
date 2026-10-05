"""
Local knowledge index — Phase 1 of the self-learning scope
(worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md §3.3).

Builds and queries a semantic index over this codebase + worklogs +
``nt_roadmap`` notes, so a future anomaly-triage pass (Phase 3) — or a
human, or any future agent session — can ask "have we seen a bug shaped
like this before" and get grounded hits instead of re-deriving context
from scratch via grep every time, the pattern this session repeatedly
fell back on.

STORAGE: a local SQLite file (same "local mutated runtime state"
convention as ``signal_stream_cache.sqlite`` / ``tuning_registry.yaml``
/ ``brick_params.yaml``), NOT a Supabase table. This is deliberate —
the embeddings data is the one part of this whole subsystem that could
grow large (thousands of chunks), and it is only ever read from the
box that builds it; there is no reason for it to touch the production
database at all. See the worklog section above for the full reasoning
(and why ``nt_roadmap`` itself stays in Supabase instead).

NO VECTOR-EXTENSION DEPENDENCY (e.g. sqlite-vec): at this corpus's
scale — one codebase + its worklogs, expected order of a few thousand
chunks — a plain BLOB column holding a serialized float32 vector, with
cosine similarity computed via numpy at query time, is simpler, has
zero C-extension packaging/compatibility risk between the Windows dev
box and the Linux Studio deployment, and is not meaningfully slower:
loading a few thousand 768-dim float32 vectors is a few milliseconds.
Revisit only if the corpus grows by orders of magnitude.

EMBEDDING MODEL: ``sentence-transformers/all-MiniLM-L6-v2`` (~90MB,
384-dim) — NOT ``jinaai/jina-embeddings-v2-base-code`` as originally
chosen 2026-09-10. Swapped 2026-09-11 after the first real Studio
deploy attempt: jina-embeddings-v2's custom ``modeling_bert.py``
(loaded via HF's dynamic-module / trust-remote-code path) imports
``find_pruneable_heads_and_indices`` from ``transformers.pytorch_utils``
— a function ``transformers`` removed in its v5 major-version internal
refactor. Studio has ``transformers==5.12.1`` (confirmed via SSH);
pinning it down to accommodate one model risked breaking whatever else
in the stack needs v5 (TimesFM's own dependency chain), so the model
choice was changed instead of the shared dependency. ``all-MiniLM-L6-v2``
is a standard, uncustomized architecture (official sentence-transformers
reference model) — verified loading cleanly under the exact same
Studio venv/transformers version before switching. Trade-off accepted
knowingly: loses jina's code-domain fine-tuning in exchange for zero
custom-code compatibility risk against future ``transformers`` upgrades.
Not a build-blocking loss — semantic retrieval for "have we seen a bug
shaped like this before" over Python/Markdown text works adequately
with a general-purpose sentence embedding model; it doesn't need
code-execution-level understanding.

Runs on CPU: confirmed via direct SSH check (2026-09-10) that the
Lightning Studio instance has no GPU at all — ``nvidia-smi``/
``/proc/driver/nvidia`` absent, and the TimesFM gateway's own
``/health`` endpoint reports ``"device":"cpu"``. Loaded via
``sentence-transformers``, which reuses this repo's EXISTING ``torch``/
``huggingface-hub`` dependencies (both already installed for TimesFM)
— no new heavy dependency stack, just the one new package.

HF_HUB_OFFLINE (2026-09-11 finding): Lightning Studio sets
``HF_HUB_OFFLINE=1`` platform-wide (confirmed via
``/proc/<pid>/environ`` on the live orchestrator process) — this blocks
ANY first-time model download, network-reachability to huggingface.co
notwithstanding (a plain ``curl`` succeeds; the block is
huggingface_hub's own offline-mode gate, not a firewall). TimesFM's
model predates this restriction (or was staged some other way) so it
was never affected; a brand-new model never gets a chance to download
under the live process's own environment. Worked around by manually
running ``build_index()`` once over SSH with ``HF_HUB_OFFLINE=0``
overridden for that single invocation — this populates the same
on-disk cache (``HF_HOME=/teamspace/studios/this_studio/.cache/
huggingface`` on the live process; confirmed the same physical storage
this module's own repo-relative ``.cache/huggingface`` path resolves
to) that the regular ``HF_HUB_OFFLINE=1`` nightly cron reads from, so
every future scheduled run finds the model locally and never needs
network access again. This is a one-time bootstrap, not a recurring
manual step.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

logger = logging.getLogger("regime.self_learning.knowledge_index")

# ── HuggingFace cache — must be set before importing sentence_transformers
# / huggingface_hub, same constraint server.py documents for TimesFM. ──────
_REPO_ROOT = Path(__file__).resolve().parents[3]
_HF_CACHE_DIR = _REPO_ROOT / ".cache" / "huggingface"
os.environ.setdefault("HF_HOME", str(_HF_CACHE_DIR))
os.environ.setdefault("HF_HUB_CACHE", str(_HF_CACHE_DIR / "hub"))
Path(os.environ["HF_HUB_CACHE"]).mkdir(parents=True, exist_ok=True)

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

DB_PATH = _REPO_ROOT / "config" / "knowledge_index.sqlite"

# Directories under the repo root walked for ``.py`` sources. Deliberately
# excludes tests/ (lower signal for a root-cause-retrieval use case — a
# test asserts behavior, it rarely explains WHY a decision was made the
# way the code and worklogs do) and vendored/build directories.
DEFAULT_CODE_ROOTS: tuple[str, ...] = ("regime_platform", "renko", "scripts")
DEFAULT_WORKLOG_DIR = "worklog"

# Chunk-size guardrails for the fallback fixed-window splitter (used when
# a Python file fails to parse, or for markdown sections that are too
# long). Measured in characters, not tokens — a simple, dependency-free
# proxy that's good enough for a first version.
MAX_CHUNK_CHARS = 4000
CHUNK_OVERLAP_CHARS = 400


@dataclass
class Chunk:
    source_type: str  # "code" | "worklog" | "roadmap_note"
    path: str
    chunk_index: int
    text: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class SearchResult:
    path: str
    source_type: str
    chunk_index: int
    text: str
    score: float


# ── Chunking ────────────────────────────────────────────────────────────


def _fixed_window_chunks(text: str) -> list[str]:
    """Fallback splitter: fixed-size windows with overlap, on whole lines
    where possible so a chunk boundary doesn't cut a line in half."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text] if text.strip() else []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + MAX_CHUNK_CHARS, n)
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        if end >= n:
            break
        start = end - CHUNK_OVERLAP_CHARS
    return chunks


def _chunk_python_source(text: str) -> list[str]:
    """Split a Python file into per-function/per-class chunks via ``ast``
    (semantically meaningful boundaries), falling back to fixed-window
    chunking for files that don't parse (e.g. a syntax error, or a
    non-Python file with a .py extension) or have no top-level defs
    (e.g. a pure-constants module)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _fixed_window_chunks(text)

    lines = text.splitlines(keepends=True)
    top_level = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if not top_level:
        return _fixed_window_chunks(text)

    chunks: list[str] = []
    # Module-level preamble (imports, module docstring, constants) before
    # the first def/class — usually where the "why" comments live.
    first_start = top_level[0].lineno - 1
    preamble = "".join(lines[:first_start]).strip()
    if preamble:
        chunks.extend(_fixed_window_chunks(preamble))

    for i, node in enumerate(top_level):
        start = node.lineno - 1
        end = top_level[i + 1].lineno - 1 if i + 1 < len(top_level) else len(lines)
        segment = "".join(lines[start:end]).strip()
        if not segment:
            continue
        if len(segment) > MAX_CHUNK_CHARS:
            chunks.extend(_fixed_window_chunks(segment))
        else:
            chunks.append(segment)
    return chunks


def _chunk_markdown(text: str) -> list[str]:
    """Split a worklog/markdown file on top-level (``#``/``##``) headings
    — each section is usually one coherent finding or decision, which is
    exactly the retrieval granularity a root-cause search wants."""
    lines = text.splitlines(keepends=True)
    sections: list[str] = []
    current: list[str] = []
    found_heading = False
    for line in lines:
        if line.startswith("# ") or line.startswith("## "):
            found_heading = True
            if current:
                sections.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("".join(current))
    if not found_heading:
        # No headings at all -- the trailing flush above still produces
        # one "section" (the whole text), which would otherwise silently
        # skip the fixed-window fallback for anything non-empty.
        return _fixed_window_chunks(text)

    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) > MAX_CHUNK_CHARS:
            chunks.extend(_fixed_window_chunks(section))
        else:
            chunks.append(section)
    return chunks


def chunk_text(text: str, source_type: str) -> list[str]:
    """Dispatch to the right splitter for ``source_type``."""
    if source_type == "code":
        return _chunk_python_source(text)
    return _chunk_markdown(text)


# ── Source discovery ────────────────────────────────────────────────────


def _iter_code_files(repo_root: Path, code_roots: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for root_name in code_roots:
        root = repo_root / root_name
        if not root.is_dir():
            continue
        files.extend(sorted(root.rglob("*.py")))
    return files


def _iter_worklog_files(repo_root: Path, worklog_dir: str) -> list[Path]:
    root = repo_root / worklog_dir
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.md"))


async def _fetch_roadmap_chunks() -> list[Chunk]:
    """Fetch nt_roadmap's mechanism/current_state/notes as chunks (2026-09-10):
    a lot of this session's own institutional memory (incident history,
    root causes, decisions) lives in that table's ``notes`` column, not
    just in worklog files — see worklog
    20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md §3.3. This
    is the ONE place the local index still talks to Supabase, as a
    read-only source, never a destination. Best-effort: returns ``[]`` on
    any failure rather than breaking the whole re-index run.
    """
    try:
        from regime_platform.core.settings import settings
        import httpx

        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            return []
        url = f"{settings.SUPABASE_URL}/rest/v1/nt_roadmap"
        params = {"select": "mechanism,status,current_state,notes"}
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:
        logger.warning("knowledge_index: nt_roadmap fetch failed: %s", exc)
        return []

    chunks: list[Chunk] = []
    for row in rows if isinstance(rows, list) else []:
        mechanism = row.get("mechanism") or "unknown"
        text = (
            f"# nt_roadmap: {mechanism} (status={row.get('status')})\n\n"
            f"current_state: {row.get('current_state') or ''}\n\n"
            f"notes: {row.get('notes') or ''}"
        )
        path = f"nt_roadmap:{mechanism}"
        for idx, piece in enumerate(_fixed_window_chunks(text)):
            chunks.append(Chunk("roadmap_note", path, idx, piece))
    return chunks


# ── Embedding ───────────────────────────────────────────────────────────

_embedder = None


def get_embedder():
    """Lazily load and cache the sentence-transformers model. Import is
    deferred to here (not module top-level) so importing this module —
    e.g. for the chunking unit tests — never requires the ``sentence-
    transformers``/``torch`` install or a model download."""
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        logger.info("knowledge_index: loading embedding model %s (CPU)", EMBEDDING_MODEL_NAME)
        _embedder = SentenceTransformer(
            EMBEDDING_MODEL_NAME, device="cpu", trust_remote_code=True,
        )
    return _embedder


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed a batch of texts. Returns an (n, EMBEDDING_DIM) float32
    array, L2-normalized so cosine similarity reduces to a dot product.

    Tries Cloudflare Workers AI first (when `CF_EMBEDDINGS_URL` is set, via
    the `cloudflare/worker.js` Cloudflare Worker), falls back to local
    MiniLM-L6-v2 on any failure. This allows a zero-downtime migration: deploy
    the worker, set the Studio secret, verify results, then unset the
    local-model dependency at leisure.
    """
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    # Option B: Cloudflare Workers AI proxy (shadow-first — enabled via env)
    if _cf_enabled():
        cf_vectors = _embed_texts_cf_sync(texts)
        if cf_vectors is not None and cf_vectors.shape[1] == EMBEDDING_DIM:
            logger.debug("knowledge_index: embedding via Cloudflare Workers AI (BGE-small)")
            return cf_vectors.astype(np.float32)

    # Fallback: local MiniLM-L6-v2 (CPU)
    model = get_embedder()
    vectors = model.encode(
        texts, batch_size=16, show_progress_bar=False,
        normalize_embeddings=True, convert_to_numpy=True,
    )
    return vectors.astype(np.float32)


# ── Cloudflare Workers AI embedding proxy ────────────────────────────
#
# Remote embedding via a Cloudflare Worker (free tier, env.AI binding — no
# token needed). The worker wraps @cf/baai/bge-small-en-v1.5 (384-dim,
# exact dimension match to MiniLM-L6-v2 → no index rebuild needed).
#
# Enabled when CF_EMBEDDINGS_URL is set (Studio Secret). The worker URL
# is the deployed Worker endpoint, e.g.
#   https://embeddings-worker.<subaccount>.workers.dev
#
# If CF_WORKER_SECRET is set on the worker, pass it via CF_WORKER_SECRET
# (same Studio Secret name → x-worker-secret header). If unset on the
# worker side, the header is optional.
#
# Env vars (Studio Secrets, NOT in feature_flags.yaml):
#   CF_EMBEDDINGS_URL  — Worker endpoint URL (e.g. https://embeddings-worker.acme.workers.dev)
#   CF_WORKER_SECRET   — optional shared secret for x-worker-secret header
#
_CF_WARNED_NO_CONFIG = False


def _cf_worker_url() -> str:
    """Cloudflare Worker endpoint URL, e.g. https://embeddings-worker.acme.workers.dev"""
    return os.getenv("CF_EMBEDDINGS_URL", "").strip().rstrip("/")


def _cf_worker_secret() -> str:
    return os.getenv("CF_WORKER_SECRET", "").strip()


def _cf_enabled() -> bool:
    return bool(_cf_worker_url())


def _embed_texts_cf_sync(texts: list[str]) -> Optional[np.ndarray]:
    """Synchronous wrapper for _embed_texts_cf. Called from the synchronous
    _build_index_sync path (which already runs in a worker thread via
    asyncio.to_thread, so asyncio.run() is safe here).
    """
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_embed_texts_cf(texts))
        finally:
            loop.close()
    except RuntimeError:
        # Already in an event loop — use async wrapper instead
        return None


async def _embed_texts_cf(texts: list[str]) -> Optional[np.ndarray]:
    """Embed texts via Cloudflare Workers AI BGE-small through the
    Cloudflare Worker. Returns None on any failure (caller falls back
    to local MiniLM). Response vectors are already L2-normalized by the
    worker.
    """
    global _CF_WARNED_NO_CONFIG
    if not _cf_enabled():
        return None

    base_url = _cf_worker_url()
    secret = _cf_worker_secret()
    if not base_url:
        if not _CF_WARNED_NO_CONFIG:
            logger.warning(
                "knowledge_index: CF_EMBEDDINGS_URL not set — falling back to local MiniLM."
            )
            _CF_WARNED_NO_CONFIG = True
        return None

    try:
        import httpx
        headers: dict = {"Content-Type": "application/json"}
        if secret:
            headers["x-worker-secret"] = secret
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/embeddings",
                headers=headers,
                json={
                    "input": texts,
                    "model": "@cf/baai/bge-small-en-v1.5",
                },
            )
        if not resp.is_success:
            logger.warning(
                "knowledge_index: Cloudflare embedding worker returned HTTP %d — fallback",
                resp.status_code,
            )
            return None
        data = resp.json()
        # Response: { data: [{ embedding: [...] }, ...] } for batch input
        # or { data: { index: 0, embedding: [...] } } for single input
        raw = data.get("data")
        if isinstance(raw, dict):
            vectors = [raw["embedding"]]
        elif isinstance(raw, list):
            vectors = [d["embedding"] for d in raw]
        else:
            return None

        arr = np.array(vectors, dtype=np.float32)
        # The worker already L2-normalizes; re-normalize for safety
        norms = np.sqrt((arr * arr).sum(axis=1, keepdims=True))
        arr = arr / np.maximum(norms, 1e-12)
        return arr
    except Exception as exc:
        logger.warning(
            "knowledge_index: Cloudflare embedding proxy failed (%s: %s) — fallback",
            type(exc).__name__, exc,
        )
        return None


# ── Storage ─────────────────────────────────────────────────────────────


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding BLOB NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(path, chunk_index)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_source_type "
        "ON knowledge_chunks(source_type)"
    )
    conn.commit()


def _existing_hashes(conn: sqlite3.Connection, path: str) -> dict[int, str]:
    rows = conn.execute(
        "SELECT chunk_index, content_hash FROM knowledge_chunks WHERE path = ?",
        (path,),
    ).fetchall()
    return {chunk_index: content_hash for chunk_index, content_hash in rows}


def _upsert_chunk(conn: sqlite3.Connection, chunk: Chunk, embedding: np.ndarray) -> None:
    conn.execute(
        """
        INSERT INTO knowledge_chunks
            (source_type, path, chunk_index, chunk_text, content_hash, embedding, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path, chunk_index) DO UPDATE SET
            source_type = excluded.source_type,
            chunk_text = excluded.chunk_text,
            content_hash = excluded.content_hash,
            embedding = excluded.embedding,
            updated_at = excluded.updated_at
        """,
        (
            chunk.source_type, chunk.path, chunk.chunk_index, chunk.text,
            chunk.content_hash, embedding.tobytes(), time.time(),
        ),
    )


def _prune_stale_chunks(conn: sqlite3.Connection, path: str, kept_indices: set[int]) -> int:
    """Delete rows for ``path`` whose chunk_index no longer exists (the
    file shrank, or a section was removed) — without this, a deleted
    chunk would linger in the index forever."""
    existing = conn.execute(
        "SELECT chunk_index FROM knowledge_chunks WHERE path = ?", (path,)
    ).fetchall()
    stale = [idx for (idx,) in existing if idx not in kept_indices]
    if stale:
        conn.executemany(
            "DELETE FROM knowledge_chunks WHERE path = ? AND chunk_index = ?",
            [(path, idx) for idx in stale],
        )
    return len(stale)


def _prune_missing_paths(conn: sqlite3.Connection, source_type: str, seen_paths: set[str]) -> int:
    """Delete every row for ``source_type`` whose path wasn't seen this
    run (e.g. a deleted file) — without this a removed file's chunks
    would linger indefinitely, contradicting the search results with
    stale/deleted content."""
    existing = conn.execute(
        "SELECT DISTINCT path FROM knowledge_chunks WHERE source_type = ?",
        (source_type,),
    ).fetchall()
    stale_paths = [p for (p,) in existing if p not in seen_paths]
    if stale_paths:
        conn.executemany(
            "DELETE FROM knowledge_chunks WHERE source_type = ? AND path = ?",
            [(source_type, p) for p in stale_paths],
        )
    return len(stale_paths)


# ── Build ───────────────────────────────────────────────────────────────


async def build_index(
    *,
    repo_root: Optional[Path] = None,
    code_roots: tuple[str, ...] = DEFAULT_CODE_ROOTS,
    worklog_dir: str = DEFAULT_WORKLOG_DIR,
    include_roadmap: bool = True,
    db_path: Optional[Path] = None,
    embed_fn=embed_texts,
) -> dict[str, Any]:
    """Incrementally (re)build the local knowledge index.

    Thin async wrapper: fetches roadmap chunks (the only genuinely async
    step — an HTTP call) here, then runs the rest (file I/O, chunking,
    embedding, SQLite writes — all synchronous/blocking) via
    ``asyncio.to_thread`` so a nightly rebuild can never stall the event
    loop the live sweep ticks run on, even on the first, full-corpus
    build. See ``_build_index_sync`` for the actual work.
    """
    roadmap_chunks: list[Chunk] = []
    if include_roadmap:
        roadmap_chunks = await _fetch_roadmap_chunks()
    return await asyncio.to_thread(
        _build_index_sync,
        repo_root=repo_root, code_roots=code_roots, worklog_dir=worklog_dir,
        roadmap_chunks=roadmap_chunks, db_path=db_path, embed_fn=embed_fn,
    )


def _build_index_sync(
    *,
    repo_root: Optional[Path],
    code_roots: tuple[str, ...],
    worklog_dir: str,
    roadmap_chunks: list[Chunk],
    db_path: Optional[Path],
    embed_fn,
) -> dict[str, Any]:
    """Synchronous core of ``build_index`` — safe to run in a worker
    thread. Only new/changed chunks (by content hash) are re-embedded —
    the embedding call is the expensive part, so an unchanged file costs
    almost nothing on a re-run. Deleted files/sections are pruned so the
    index never accumulates stale content. ``embed_fn`` is injectable so
    tests can run this whole pipeline without loading the real model.

    Returns a summary dict: chunks_seen, chunks_embedded (new/changed),
    chunks_pruned, files_processed, duration_ms.
    """
    start_ts = time.time()
    root = repo_root or _REPO_ROOT
    conn = _connect(db_path)
    summary = {
        "chunks_seen": 0, "chunks_embedded": 0, "chunks_pruned": 0,
        "files_processed": 0, "errors": 0,
    }
    try:
        seen_code_paths: set[str] = set()
        for file_path in _iter_code_files(root, code_roots):
            rel_path = str(file_path.relative_to(root))
            seen_code_paths.add(rel_path)
            summary["files_processed"] += 1
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.warning("knowledge_index: read failed %s: %s", rel_path, exc)
                summary["errors"] += 1
                continue
            _index_one_file(conn, "code", rel_path, text, embed_fn, summary)

        seen_worklog_paths: set[str] = set()
        for file_path in _iter_worklog_files(root, worklog_dir):
            rel_path = str(file_path.relative_to(root))
            seen_worklog_paths.add(rel_path)
            summary["files_processed"] += 1
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.warning("knowledge_index: read failed %s: %s", rel_path, exc)
                summary["errors"] += 1
                continue
            _index_one_file(conn, "worklog", rel_path, text, embed_fn, summary)

        summary["chunks_pruned"] += _prune_missing_paths(conn, "code", seen_code_paths)
        summary["chunks_pruned"] += _prune_missing_paths(conn, "worklog", seen_worklog_paths)

        if roadmap_chunks:
            seen_roadmap_paths: set[str] = set()
            by_path: dict[str, list[Chunk]] = {}
            for c in roadmap_chunks:
                by_path.setdefault(c.path, []).append(c)
            for path, chunks in by_path.items():
                seen_roadmap_paths.add(path)
                _index_chunks(conn, path, chunks, embed_fn, summary)
            summary["chunks_pruned"] += _prune_missing_paths(
                conn, "roadmap_note", seen_roadmap_paths
            )

        conn.commit()
    finally:
        conn.close()

    summary["duration_ms"] = int((time.time() - start_ts) * 1000)
    logger.info("knowledge_index: build complete — %s", summary)
    return summary


def _index_one_file(conn, source_type: str, rel_path: str, text: str, embed_fn, summary: dict) -> None:
    pieces = chunk_text(text, source_type)
    chunks = [Chunk(source_type, rel_path, i, piece) for i, piece in enumerate(pieces)]
    _index_chunks(conn, rel_path, chunks, embed_fn, summary)


def _index_chunks(conn, path: str, chunks: list[Chunk], embed_fn, summary: dict) -> None:
    existing = _existing_hashes(conn, path)
    kept_indices: set[int] = set()
    to_embed: list[Chunk] = []
    for chunk in chunks:
        kept_indices.add(chunk.chunk_index)
        summary["chunks_seen"] += 1
        if existing.get(chunk.chunk_index) == chunk.content_hash:
            continue  # unchanged — skip re-embedding
        to_embed.append(chunk)

    if to_embed:
        vectors = embed_fn([c.text for c in to_embed])
        for chunk, vector in zip(to_embed, vectors):
            _upsert_chunk(conn, chunk, vector)
        summary["chunks_embedded"] += len(to_embed)

    summary["chunks_pruned"] += _prune_stale_chunks(conn, path, kept_indices)


# ── Search ──────────────────────────────────────────────────────────────


def semantic_search(
    query: str,
    k: int = 5,
    source_type: Optional[str] = None,
    *,
    db_path: Optional[Path] = None,
    embed_fn=embed_texts,
) -> list[SearchResult]:
    """Embed ``query`` and return the top-``k`` most similar chunks.

    Loads candidate vectors (optionally filtered to one ``source_type``)
    into a numpy matrix and computes cosine similarity directly — see
    module docstring for why this doesn't need a vector-index extension
    at this corpus's scale.
    """
    conn = _connect(db_path)
    try:
        if source_type:
            rows = conn.execute(
                "SELECT id, source_type, path, chunk_index, chunk_text, embedding "
                "FROM knowledge_chunks WHERE source_type = ?",
                (source_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, source_type, path, chunk_index, chunk_text, embedding "
                "FROM knowledge_chunks"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    query_vec = embed_fn([query])[0]  # already L2-normalized by embed_texts
    matrix = np.stack([
        np.frombuffer(row[5], dtype=np.float32) for row in rows
    ])
    scores = matrix @ query_vec  # cosine similarity, both sides normalized
    top_indices = np.argsort(-scores)[:k]

    results: list[SearchResult] = []
    for i in top_indices:
        _, source_type_, path, chunk_index, chunk_text_, _ = rows[i]
        results.append(SearchResult(
            path=path, source_type=source_type_, chunk_index=chunk_index,
            text=chunk_text_, score=float(scores[i]),
        ))
    return results
