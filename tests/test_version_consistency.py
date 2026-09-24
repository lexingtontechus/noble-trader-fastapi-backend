"""H2 verification: platform version is uniformly 8.0.0 (no 7.2.0 drift).

Parses the actual files rather than importing main.py (heavy deps).
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_pyproject_version_is_8_0_0():
    txt = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'version\s*=\s*"([^"]+)"', txt)
    assert m, "no version= found in pyproject.toml"
    assert m.group(1) == "8.0.0", f"pyproject version is {m.group(1)}"


def test_main_py_version_is_8_0_0():
    txt = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '"8.0.0"' in txt, "main.py missing 8.0.0"
    assert '"7.2.0"' not in txt, "main.py still contains 7.2.0 drift"


def test_openapi_versions_are_8_0_0():
    for p in ["docs/openapi.yaml", "docs/Setup/openapi.yaml"]:
        txt = (ROOT / p).read_text(encoding="utf-8")
        assert '"8.0.0"' in txt, f"{p} missing 8.0.0"
        assert '"7.2.0"' not in txt, f"{p} still contains 7.2.0"
