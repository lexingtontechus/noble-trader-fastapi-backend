# Backend version bump — files that MUST move together (2026-08-11)

Bumping `noble-trader-fastapi-backend` version metadata is NOT a one-file
edit. The version-consistency tests assert the EXACT string, and there are
TWO openapi.yaml copies.

## Files to bump (7.7.0 → 7.7.2 example)

| File | Occurrences |
|---|---|
| `pyproject.toml` | 1 (`version = "x.y.z"`) |
| `main.py` | 4 (banner line 3, `Starting up v…` log ~141, FastAPI `version=` ~422, `/health` JSON ~593) |
| `docs/openapi.yaml` | 2 (line 5 + example ~124) |
| `docs/Setup/openapi.yaml` | **2 — the SECOND copy, easy to miss** (line 5 + example ~124) |
| `tests/test_version_consistency.py` | **3 tests assert the exact version** — update the docstring + all assertions |

## The pitfall (hit live 2026-08-11)

Bumping only pyproject/main.py/docs/openapi.yaml → full suite shows
`3 failed`:

```
FAILED tests/test_version_consistency.py::test_main_py_version_is_7_7_0
FAILED tests/test_version_consistency.py::test_openapi_versions_are_7_7_0
FAILED tests/test_version_consistency.py::test_pyproject_version_is_7_7_0
```

The 3rd failure was `docs/Setup/openapi.yaml` (the test loops BOTH openapi
files). The tests exist to catch drift — update them IN THE SAME commit as
the bump, then re-run:

```bash
PYTHONPATH="$PWD" .venv/Scripts/python.exe -m pytest tests/test_version_consistency.py -q
# full suite: 348 passed / 23 skipped
```

## Gotchas

- `docs/Setup/openapi.yaml` contains a TAB character (~line 2994) — the
  `patch` tool's YAML validator refuses the whole file. Use python
  read/replace/write instead of patch.
- `grep -rn "7\.7\.0"` over the 5 files should return NOTHING after the
  bump (use it as the completion check).
- CHANGELOG `worklog/CHANGELOG.md` top entry may run AHEAD of metadata
  (7.7.1 entry existed while metadata was 7.7.0); the bump realigns them.
