# This package contains Nexus MCP server implementations under mcp.ssh_server.*
#
# The installed `mcp` SDK (Model Context Protocol) also uses the `mcp` namespace.
# We make the local `mcp` package transparently proxy the SDK by:
#   1. Extending __path__ so sub-module imports (mcp.types, mcp.server …) work.
#   2. Making `mcp.types` available as an attribute on this module (for
#      annotations like `mcp.types.ImageContent`).
#   3. Re-exporting all SDK public names from this __init__ so that
#      `from mcp import LoggingLevel, ServerSession` etc. work as fastmcp needs.
from __future__ import annotations

import importlib.util
import os
import sys

# ── Locate the installed mcp SDK ─────────────────────────────────────────────
_venv_lib = os.path.join(
    os.path.dirname(sys.executable),
    "..",
    "lib",
    f"python{sys.version_info.major}.{sys.version_info.minor}",
    "site-packages",
)
_sdk_dir = os.path.normpath(os.path.join(_venv_lib, "mcp"))

# ── Step 1: extend __path__ so `import mcp.types` finds sdk/mcp/types.py ─────
if os.path.isdir(_sdk_dir):
    __path__ = list(__path__) + [_sdk_dir]  # type: ignore[name-defined]


def _import_sdk_submodule(name: str):
    """Import sdk sub-module and register it in sys.modules as mcp.<name>."""
    full = f"mcp.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.find_spec(full)
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── Step 2: eagerly import key SDK sub-modules and attach as attributes ───────
_SDK_SUBMODULES = ["types", "server", "client", "shared"]

for _sub in _SDK_SUBMODULES:
    _m = _import_sdk_submodule(_sub)
    if _m is not None:
        globals()[_sub] = _m

# ── Step 3: re-export all public names from mcp.types into this namespace ─────
# mcp.types does NOT define __all__ so we iterate dir() and skip private names.
_types_mod = sys.modules.get("mcp.types")
if _types_mod is not None:
    for _n in dir(_types_mod):
        if not _n.startswith("_") and _n not in globals():
            globals()[_n] = getattr(_types_mod, _n)

# ── Step 4: pull extra names that fastmcp imports directly from `mcp` ─────────
# These live in sub-sub-modules, not in mcp.types.
for _sub_name, _attrs in [
    ("server.session", ["ServerSession"]),
    ("client.session", ["ClientSession"]),
    ("client.session_group", ["ClientSessionGroup"]),
    ("client.stdio", ["StdioServerParameters", "stdio_client"]),
    ("server.stdio", ["stdio_server"]),
    ("shared.exceptions", ["McpError", "UrlElicitationRequiredError"]),
]:
    _sm = _import_sdk_submodule(_sub_name)
    if _sm is not None:
        for _attr in _attrs:
            if _attr not in globals():
                _v = getattr(_sm, _attr, None)
                if _v is not None:
                    globals()[_attr] = _v
