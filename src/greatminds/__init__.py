"""greatminds — file-based multi-agent coordination protocol.

See https://github.com/veryviolet/greatminds for full docs.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("greatminds")
except PackageNotFoundError:
    # Running from a source checkout without the package installed
    # (e.g. ``python -c 'import greatminds'`` in a venv that hasn't
    # been ``pip install -e .``-d). 0144: don't crash; return a
    # placeholder so callers like report-upstream still produce a
    # diag line, just with a value that signals "uninstalled".
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
