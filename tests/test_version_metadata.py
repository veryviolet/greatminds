"""Tests for ``greatminds.__version__`` parity with package metadata.

Task 0144: ``__version__`` was a literal string ``"1.2.2"`` that the
1.2.3/1.2.4 release cuts forgot to bump. ``greatminds.__version__``
and ``importlib.metadata.version("greatminds")`` drifted by two
patch releases; ``report-upstream`` then published the wrong number
in upstream issues. Fix: derive ``__version__`` from
``importlib.metadata.version`` so the source of truth is pyproject.
"""
from __future__ import annotations

import importlib.metadata
from pathlib import Path


def _pyproject_version() -> str:
    # tomllib (3.11+) is in stdlib; tests already run on 3.11+.
    import tomllib
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with path.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def test_greatminds_version_matches_installed_metadata() -> None:
    """The 0144 contract: ``greatminds.__version__`` must equal
    ``importlib.metadata.version("greatminds")``. Pre-fix the literal
    string lagged behind every release cut by however many releases
    happened between forgotten bumps."""
    import greatminds
    assert greatminds.__version__ == importlib.metadata.version("greatminds")


def test_greatminds_version_matches_pyproject_toml() -> None:
    """And pyproject is the source of truth those metadata reads from.
    Three-way parity (module / installed metadata / pyproject) means
    one bump-pyproject-and-rebuild ritual updates all consumers
    atomically — no more 'oops forgot __init__.py' regressions."""
    import greatminds
    assert greatminds.__version__ == _pyproject_version()


def test_version_is_non_empty_string() -> None:
    """Negative pin against accidental None / empty / non-str types
    (e.g. if someone mistakenly imports the package object instead
    of the string)."""
    import greatminds
    assert isinstance(greatminds.__version__, str)
    assert greatminds.__version__.strip() != ""
