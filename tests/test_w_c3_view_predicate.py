"""W-C3 (2026-10-19 weaknesses evaluation, P0-3): the published-book
calibration view must carry the era/cutover predicate.

The defect: migration 209 defined ``v_calibration_records_published`` with
ONLY ``qualified = true`` and dropped the era filter every other calibration
reader applies (migration 192's ``v_calibration_records_current`` and
``v_calibration_records_tiered``). Three evidence populations disagreed —
the published-book standard mixed close-walk-era and intrabar-era rows in
the same bias/Brier window (mixed-semantics contamination: survivable base
rate 0.2728 close-walk vs 0.057-0.069 intrabar).

The fix: migration 216 re-creates the view with 209's body PLUS the
migration-192 cutover predicate, keyed on the same resolution-time basis
(``COALESCE(s.resolved_at, c.entered_at) < cutover_at`` → excluded).

These tests parse the SQL files (no DB) and pin:
  * 216 defines v_calibration_records_published (CREATE OR REPLACE VIEW);
  * 209's qualified=true filter is preserved;
  * the WHERE predicate exists and references nt_calibration_cutover;
  * the predicate's time basis matches migration 192's era filter exactly
    (resolved_at with entered_at fallback) — the column semantics 209
    dropped are the ones 192 established;
  * the column list matches 209's (no silent column drift).
"""
from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"

SQL_209 = (MIGRATIONS / "209_calibration_published_book_view.sql").read_text()
SQL_216 = (MIGRATIONS / "216_calibration_published_cutover_predicate.sql").read_text()
SQL_192 = (MIGRATIONS / "192_calibration_cutover.sql").read_text()


def _view_body(sql: str, view: str) -> str:
    """The CREATE ... VIEW <view> AS ... statement's body (up to the
    terminating semicolon at statement level)."""
    m = re.search(
        rf"CREATE\s+OR\s+REPLACE\s+VIEW\s+{re.escape(view)}\s+AS(.*?);",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert m, f"view {view} not defined in SQL"
    return m.group(1)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def test_216_defines_the_published_book_view():
    assert re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+v_calibration_records_published\s+AS",
        SQL_216,
        re.IGNORECASE,
    ), "216 must (re)define v_calibration_records_published"


def test_209_had_no_era_predicate_the_defect():
    """Pin the defect itself: 209's view filtered ONLY on qualified=true —
    the era filter migration 192 established was dropped."""
    body = _view_body(SQL_209, "v_calibration_records_published")
    body_nc = _strip_comments(body)
    assert "nt_calibration_cutover" not in body_nc
    assert re.search(r"qualified\s*=\s*true", body_nc, re.IGNORECASE)


def test_216_restores_the_cutover_predicate():
    body = _view_body(SQL_216, "v_calibration_records_published")
    body_nc = _strip_comments(body)
    assert re.search(r"qualified\s*=\s*true", body_nc, re.IGNORECASE), (
        "209's published-book filter must be preserved"
    )
    assert "nt_calibration_cutover" in body_nc, (
        "the era/cutover predicate must be present in 216"
    )
    assert re.search(r"NOT\s+EXISTS\s*\(", body_nc, re.IGNORECASE)


def test_216_predicate_time_basis_matches_migration_192():
    """The predicate column semantics must match 192's era filter: keyed on
    COALESCE(<signal_sim alias>.resolved_at, <records alias>.entered_at)
    compared against cutover_at. 209's inner join aliases c/s are the same
    shape 192's tiered view uses (c/k for the marker)."""
    body216 = _strip_comments(_view_body(SQL_216, "v_calibration_records_published"))
    m216 = re.search(
        r"COALESCE\(\s*(\w+)\.resolved_at\s*,\s*(\w+)\.entered_at\s*\)\s*<\s*(\w+)\.cutover_at",
        body216,
    )
    assert m216, "216 must key the era filter on COALESCE(resolved_at, entered_at) < cutover_at"
    sig_alias, rec_alias, marker_alias = m216.groups()
    assert rec_alias == "c", "records alias should stay 209's 'c'"
    assert marker_alias == "k", "cutover marker alias should be 'k'"

    # the SAME expression shape exists in 192's views (era-filter authority)
    body192 = _strip_comments(SQL_192)
    m192 = re.search(
        r"COALESCE\(\s*(\w+)\.resolved_at\s*,\s*(\w+)\.entered_at\s*\)\s*<\s*(\w+)\.cutover_at",
        body192,
    )
    assert m192, "192 must contain the canonical era-filter expression"
    assert m192.group(1) == "s" and m192.group(2) == "r", (
        "192's canonical predicate keys on s.resolved_at / r.entered_at"
    )
    assert sig_alias == "s", "216 must key the signal_sim side on the same column (resolved_at)"


def test_216_marker_singleton_guard_matches_192():
    """The marker row guard (k.id = 1) matches 192's singleton convention."""
    body216 = _strip_comments(_view_body(SQL_216, "v_calibration_records_published"))
    assert re.search(r"\bk\.id\s*=\s*1\b", body216)
    assert re.search(r"\bk\.id\s*=\s*1\b", _strip_comments(SQL_192))


def test_216_column_list_matches_209():
    """No silent column drift: 216 selects exactly 209's columns."""
    def _cols(body: str) -> list[str]:
        # only the SELECT list (between SELECT and FROM), not join/predicate refs
        select_list = _strip_comments(body).split("FROM")[0]
        return re.findall(r"c\.(\w+)", select_list)

    cols_209 = _cols(_view_body(SQL_209, "v_calibration_records_published"))
    cols_216 = _cols(_view_body(SQL_216, "v_calibration_records_published"))
    assert cols_209 == cols_216, (
        f"column drift: 209={cols_209} 216={cols_216}"
    )
    # the join key stays in the projection
    assert "signal_id" in cols_216


def test_216_keeps_the_join_shape():
    """209's inner join to nt_signal_sim on signal_id is what makes the
    qualified=true filter and the resolved_at basis well-defined — 216
    must keep it."""
    body216 = _strip_comments(_view_body(SQL_216, "v_calibration_records_published"))
    assert re.search(
        r"JOIN\s+nt_signal_sim\s+s\s+ON\s+s\.signal_id\s*=\s*c\.signal_id",
        body216,
        re.IGNORECASE,
    )


def test_216_header_documents_the_restoration():
    """Conventions: header explains WHY (mixed-era contamination) like 209/
    192 do, and the file ends with the pgrst schema-reload NOTIFY."""
    header = "\n".join(SQL_216.splitlines()[:40])
    assert "2026-10-19" in header
    assert re.search(r"(mix| MIX)", SQL_216, re.IGNORECASE) or "mixing" in SQL_216.lower()
    assert "192" in header
    assert SQL_216.rstrip().endswith("NOTIFY pgrst, 'reload schema';")
