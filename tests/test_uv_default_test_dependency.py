"""The documented plain ``uv run pytest`` gate must be self-provisioning."""
from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_plain_uv_run_provisions_pytest_from_the_default_dev_group():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    metadata = tomllib.loads(text)
    groups = metadata.get("dependency-groups", {})
    dev = groups.get("dev") if isinstance(groups, dict) else None

    assert isinstance(dev, list), "pyproject.toml must declare a default uv development group"
    requirements = [str(requirement).lower() for requirement in dev]
    assert any(requirement.startswith("pytest") for requirement in requirements)
