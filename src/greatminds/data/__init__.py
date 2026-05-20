"""Package data namespace.

This is a regular Python package (not a namespace package) so that
``importlib.resources.files('greatminds.data')`` resolves to the directory
containing ``plugins/``, ``mcp/``, ``codex/profiles/``, ``templates/``,
``schema.yaml``, role docs and the unified ``command_START.yaml``.

Modules under ``data/`` (if any) should remain importable, but the primary
content here is non-Python data files shipped verbatim with the wheel.
"""
