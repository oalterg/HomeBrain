"""Import smoke test for the five MCP servers.

CI ran `compileall`, which proves a file parses and nothing more. Every one of
these servers is a standalone stdio process that OpenClaw spawns on demand, so
a module-level error — a bad import, a typo in a constant, a decorator that
throws — does not fail anything at build time. It surfaces later as "the
agent's Nextcloud tools stopped working", with the traceback buried in
OpenClaw's subprocess log. That is the same invisible-failure shape as the
Fernet key bug, and this is the cheapest possible guard against it.

Also checks the one invariant that can silently drift: TOOLS advertises the
tool list to the model, DISPATCH routes the calls, and they are maintained by
hand in two different places in the same file. A tool in TOOLS with no DISPATCH
entry is advertised to the agent and errors when called.

Run:  python3 -m pytest scripts/tests/test_mcp_servers_load.py
"""
import importlib.util
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SCRIPTS)

SERVERS = [
    "mcp-email.py",
    "mcp-homeassistant.py",
    "mcp-homebrain.py",
    "mcp-nextcloud.py",
    "mcp-vault.py",
]


def _load(filename):
    """Import a hyphenated script as a module. `serve()` sits behind
    `if __name__ == "__main__"`, so importing does not start a server."""
    path = os.path.join(SCRIPTS, filename)
    name = filename[:-3].replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module", params=SERVERS)
def server(request):
    return request.param, _load(request.param)


def test_module_imports(server):
    name, mod = server
    assert mod is not None, f"{name} failed to import"


def test_advertises_tools(server):
    name, mod = server
    assert isinstance(mod.TOOLS, list) and mod.TOOLS, f"{name} advertises no tools"
    for tool in mod.TOOLS:
        assert tool.get("name"), f"{name} has a tool with no name"
        assert tool.get("description"), f"{name}: {tool.get('name')} has no description"
        assert isinstance(tool.get("inputSchema"), dict), \
            f"{name}: {tool.get('name')} has no inputSchema"


def test_tool_descriptions_stay_short_for_the_model(server):
    """tools/list is injected into every agent turn. Verbose copy here is
    permanent context tax; keep the catalogue terse."""
    name, mod = server
    for tool in mod.TOOLS:
        desc = tool["description"]
        assert len(desc) <= 160, f"{name}: {tool['name']} description is {len(desc)} chars"
        for key, prop in (tool.get("inputSchema") or {}).get("properties", {}).items():
            pdesc = prop.get("description") or ""
            assert len(pdesc) <= 80, (
                f"{name}: {tool['name']}.{key} description is {len(pdesc)} chars"
            )


def test_every_advertised_tool_is_dispatchable(server):
    """The regression this guards: a tool the model can see and cannot call."""
    name, mod = server
    advertised = {t["name"] for t in mod.TOOLS}
    routed = set(mod.DISPATCH)
    assert advertised - routed == set(), \
        f"{name} advertises tools with no DISPATCH entry: {sorted(advertised - routed)}"
    assert routed - advertised == set(), \
        f"{name} routes tools it never advertises: {sorted(routed - advertised)}"


def test_dispatch_rejects_an_unknown_tool(server):
    name, mod = server
    out = mod.dispatch("nope.not_a_tool", {})
    assert out.get("ok") is False, f"{name} did not reject an unknown tool"
