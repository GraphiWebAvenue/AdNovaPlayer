"""
The version the fleet reports must be the version the fleet is running.

`__version__` is a literal, deliberately: the venv is an editable install, so
importlib.metadata only refreshes when pip re-runs, and a value read on every
heartbeat must not depend on that having happened. The cost of a literal is
that it can drift — and it did. pyproject.toml went 1.7.0 -> 1.8.0 -> 1.8.1
while `__init__.py` stayed at 1.7.0, so every device in the field reported a
version it had not run for two releases. Dashboard's fleet view uses that
string to decide which stands are stale, and an operator staring at a stand
that has already updated cannot tell the difference from one that has not.

This test is what makes the literal safe to keep.
"""

from __future__ import annotations

import re
from pathlib import Path

from adnova_player import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_version() -> str:
    """The version in pyproject.toml — the single source of truth."""
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        match = re.match(r'^version\s*=\s*"([^"]+)"', line.strip())
        if match:
            return match.group(1)
    raise AssertionError("pyproject.toml declares no version")


def test_the_reported_version_matches_the_packaged_one():
    assert __version__ == _declared_version(), (
        f"adnova_player.__version__ is {__version__!r} but pyproject.toml says "
        f"{_declared_version()!r}. The heartbeat reports the former, so the fleet "
        "would claim to be running a release it is not. Bump both."
    )


def test_the_version_is_a_release_number():
    """Guards against a placeholder or a stray suffix reaching the fleet."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), (
        f"{__version__!r} is not a plain MAJOR.MINOR.PATCH release number"
    )
