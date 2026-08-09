#!/usr/bin/env python3
"""#57 + P6: every version literal is single-sourced so nothing can silently
drift again.

The decision this test encodes:
  * ``ledger_agent.__version__`` is the ONE package/tool version. It feeds the
    wheel metadata (pyproject ``dynamic``) AND both standalone tools
    (``ledger.py`` / ``ledger_route.py`` resolve it, with a stdlib fallback so
    they still run uninstalled).
  * ``openapi.yaml`` ``info.version`` tracks the FROZEN ``/v1`` contract, pinned
    to ``ledger_agent.__api_version__`` on purpose — independent of the package
    version, bumped only on a wire change.

If any literal is edited without updating its single source, one of these fails.
"""
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import ledger_agent

_PYPROJECT = os.path.join(_ROOT, "pyproject.toml")
_SEMVER = r"^\d+\.\d+\.\d+"


def _read(name):
    with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
        return f.read()


class TestVersionSingleSource(unittest.TestCase):
    def setUp(self):
        self.text = _read("pyproject.toml")

    # --- package version single-sources the wheel metadata (#57) -------------
    def test_pyproject_declares_version_dynamic(self):
        self.assertRegex(self.text, r'dynamic\s*=\s*\[[^\]]*"version"')

    def test_pyproject_has_no_static_version(self):
        # No `version = "x.y.z"` inside [project] — that would shadow the dynamic
        # source and reintroduce drift.
        self.assertNotRegex(self.text, r'(?m)^\s*version\s*=\s*"\d')

    def test_dynamic_source_points_at_dunder_version(self):
        self.assertIn('attr = "ledger_agent.__version__"', self.text)

    def test_dunder_version_is_resolvable(self):
        self.assertRegex(ledger_agent.__version__, _SEMVER)

    # --- standalone tools resolve the package version, not a literal (P3/P6) --
    def test_ledger_monitor_has_no_hardcoded_version(self):
        src = _read("ledger.py")
        self.assertNotRegex(
            src, r'(?m)^\s*VERSION\s*=\s*"\d',
            "ledger.py must resolve its version from ledger_agent.__version__, "
            "not hardcode a literal")

    def test_ledger_router_has_no_hardcoded_version(self):
        src = _read("ledger_route.py")
        self.assertNotRegex(
            src, r'(?m)^\s*VERSION\s*=\s*"\d',
            "ledger_route.py must resolve its version from "
            "ledger_agent.__version__, not hardcode a literal")

    def test_monitor_reports_package_version(self):
        import ledger
        self.assertEqual(ledger.VERSION, ledger_agent.__version__)

    def test_router_reports_package_version(self):
        import ledger_route
        self.assertEqual(ledger_route.VERSION, ledger_agent.__version__)

    # --- openapi tracks the frozen /v1 contract version (P4) -----------------
    def test_api_contract_version_is_semver(self):
        self.assertRegex(ledger_agent.__api_version__, _SEMVER)

    def test_openapi_version_matches_api_contract(self):
        m = re.search(r'(?m)^\s*version:\s*"([^"]+)"', _read("openapi.yaml"))
        self.assertIsNotNone(m, "openapi.yaml info.version not found")
        self.assertEqual(
            m.group(1), ledger_agent.__api_version__,
            "openapi.yaml version must equal ledger_agent.__api_version__ "
            "(the frozen /v1 contract line); bump both together, on purpose")


if __name__ == "__main__":
    unittest.main()
