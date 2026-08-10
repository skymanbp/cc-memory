"""Canonical runtime version for cc-memory.

This is THE single source of truth for the version string in Python code.

Why it lives in `core/` and not in `cc_memory/__init__.py`:
every entry point (hooks, CLI, MCP, UI) bootstraps by inserting the *package
directory* on `sys.path` and then importing flat — `from core.db import MemoryDB`
— because the standalone installer lays the tree out FLAT (`TARGET_DIR/core/…`,
with no `cc_memory/` segment at all).  Under that layout `import cc_memory`
raises ModuleNotFoundError, so `from cc_memory import __version__` cannot be used
inside any module that has to run from both layouts.  `from core.version import
__version__` resolves under both.

`cc_memory/__init__.py` re-exports this value, so the packaged/importable form
(`from cc_memory import __version__`) keeps working for consumers that install
the wheel.

Bump this together with the non-importable manifests, which cannot read it:
    cc_memory/config.json          "version"
    .claude-plugin/plugin.json     "version"
    .claude-plugin/marketplace.json  plugins[0].version
    pyproject.toml                 [project] version
`tests/smoke_test.py` asserts all of them agree with this value.
"""
__version__ = "2.8.0"
