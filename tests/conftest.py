"""Session-wide safety net for Supabase I/O during tests.

``regime_platform/core/settings.py`` loads ``.env.local`` — which, in this
workspace, holds the REAL production service-role key for the live
``NobleTrading`` Supabase project. Several tests (found 2026-08-23, see
``worklog/20260823_CALIBRATION_BIAS_CHECK_AUDIT.md`` §5) call real
``supabase_client``/``tuning_registry`` functions directly instead of
mocking the network boundary, believing (per their own docstrings) that
"no live network" is involved. It is: this was confirmed writing directly
to the live ``nt_tuning_params`` table (254 rows of test fixtures were
found there and removed).

This fixture patches ``httpx.AsyncClient.request`` for the whole test
session so that every call to ``/rest/v1/<table>`` is transparently
rewritten to ``/rest/v1/<table>_test``. This is deliberately a network-layer
fix rather than patching every call site: call sites in this codebase build
Supabase URLs in more than one way (some via ``supabase_client._url()``,
some via inline f-strings — e.g. ``eod_correction._fetch_recent_win_rates``),
so a per-call-site fix is easy to miss for the next one added. Tests that
DO mock at a higher level (patching ``supabase_client.fetch_*``/`_insert_row`
directly) are unaffected, since the real ``httpx.AsyncClient`` never gets
called in those tests to begin with.

Tables with no ``_test`` counterpart simply 404 under this redirect. Every
Supabase call site in this codebase already treats I/O as best-effort
(try/except, returns ``None``/``[]``/no-op on failure) — a redirected 404
fails exactly the same way a real outage would, which is safe by
construction. Add a ``<table>_test`` table (``CREATE TABLE ... (LIKE
<table> INCLUDING ALL)``) for any real table a future unmocked test needs
to round-trip against.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

_REST_TABLE_RE = re.compile(r"^(/rest/v1/)([A-Za-z0-9_]+)(.*)$")


def _redirect_to_test_table(url) -> str:
    url_str = str(url)
    parts = urlsplit(url_str)
    match = _REST_TABLE_RE.match(parts.path)
    if not match:
        return url_str
    prefix, table, rest = match.groups()
    if table.endswith("_test"):
        return url_str
    new_path = f"{prefix}{table}_test{rest}"
    return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))


@pytest.fixture(autouse=True, scope="session")
def _no_production_supabase_writes():
    original_request = httpx.AsyncClient.request

    async def patched_request(self, method, url, *args, **kwargs):
        return await original_request(self, method, _redirect_to_test_table(url), *args, **kwargs)

    httpx.AsyncClient.request = patched_request
    try:
        yield
    finally:
        httpx.AsyncClient.request = original_request
