"""Contract test for the audit boundary: which parameter names may reach a log entry.

Two threats meet in this file. T-18-01 is a parameter value or a result body in the log, and
the answer to it is that ``audit/allowlist.py`` holds names alone, with the payload names on
a block list so none of them can quietly grow a value beside it one day. T-18-02 is a caller
that writes an invented parameter name into an entry, and the answer to it is that every name
of the allowlist has to exist in the schema of its own tool.

AUDIT-01 asks for the shape of ``scripts/check_tool_budget.py``: one measurement over all
registered tools, never a sample, failing as soon as a single tool crosses the boundary and
naming every finding rather than only the fact that there was one. Nothing here registers a
tool on the module singleton ``mcp``: ``tests/contract/test_tool_surface.py`` compares the
registry against a frozen literal, so a leftover test tool would turn that file red.

The proof that every tool carries the recording decorator lives in the two cases at the
bottom, added with the marker in plan 18-06. They reach through ``mcp._tool_manager``,
because that is the only place a registered tool still has its ``fn``; the boundary gate of
``tests/contract/test_module_boundaries.py`` walks ``src/mcp_connector`` and never ``tests/``,
so this is a source gate doing its job rather than a break of one.
"""

from typing import Any

import pytest
from mcp import Client
from test_app_tool_markings import ESTADISTICA_TOOLS

from mcp_connector.audit.allowlist import FORBIDDEN_PARAMS, PARAM_ALLOWLIST
from mcp_connector.server import mcp


def _argument_names(schema: dict[str, Any] | None) -> set[str]:
    """The names a caller can put into the arguments of a call, top level only.

    A nested model lives under ``$defs`` and its field names are never keys of the arguments
    mapping the recorder sees, so widening this to the nested pass of
    ``tests/contract/test_tool_surface.py`` would compare the allowlist against names that
    can never arrive as an argument.
    """
    properties = (schema or {}).get("properties")
    return set(properties) if isinstance(properties, dict) else set()


async def _measured_surface() -> dict[str, set[str]]:
    """Every registered tool with its argument names, taken over the wire like the gate."""
    async with Client(mcp, raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools
    return {tool.name: _argument_names(tool.input_schema) for tool in tools}


@pytest.mark.anyio
async def test_every_registered_tool_has_an_allowlist_entry() -> None:
    """A tool the allowlist never heard of would be recorded by nobody's decision."""
    # The APS app tools record no argument name, the exception PARAM_ALLOWLIST documents.
    surface = {
        name: names
        for name, names in (await _measured_surface()).items()
        if name not in ESTADISTICA_TOOLS
    }

    findings = [
        f"{name} has no allowlist entry" for name in sorted(surface) if name not in PARAM_ALLOWLIST
    ]
    findings += [
        f"{name} has an allowlist entry but is not registered"
        for name in sorted(PARAM_ALLOWLIST)
        if name not in surface
    ]

    assert findings == [], (
        "the allowlist and the tool registry have to be the same set, or a new tool is "
        "recorded on an assumption nobody made: " + ", ".join(findings)
    )


@pytest.mark.anyio
async def test_no_allowlisted_name_is_absent_from_its_own_schema() -> None:
    """T-18-02: a name that no longer exists points the recorder at a parameter nobody sends."""
    surface = await _measured_surface()

    findings: list[str] = []
    for name in sorted(PARAM_ALLOWLIST):
        for parameter in sorted(PARAM_ALLOWLIST[name] - surface.get(name, set())):
            findings.append(f"{name}.{parameter}")

    assert findings == [], (
        "an allowlisted name that the tool does not take is a rule guarding nothing, and it "
        "stays wrong until somebody reads this list by hand: " + ", ".join(findings)
    )


def test_no_allowlisted_name_is_on_the_block_list() -> None:
    """T-18-01: the payload names carry content by their mere presence, so they stay out."""
    findings = [
        f"{name}.{parameter}"
        for name in sorted(PARAM_ALLOWLIST)
        for parameter in sorted(PARAM_ALLOWLIST[name] & FORBIDDEN_PARAMS)
    ]

    assert findings == [], (
        "recording that a body was handed along says nothing a reader did not already know "
        "from the tool name, and it is the place a value grows next to one day: "
        + ", ".join(findings)
    )


@pytest.mark.anyio
async def test_the_block_list_names_parameters_that_really_exist() -> None:
    """A block list that matches nothing blocks nothing, and nobody would notice."""
    surface = await _measured_surface()

    matched = sorted(
        f"{name}.{parameter}"
        for name in surface
        for parameter in sorted(surface[name] & FORBIDDEN_PARAMS)
    )

    assert matched != [], (
        "no name of FORBIDDEN_PARAMS occurs in the measured tool surface: the list would be "
        "decoration, and the case above would pass for the wrong reason"
    )


async def _undecorated(ctx: Any = None) -> str:
    """The counter proof of the marker case, and it is deliberately never registered.

    ``tests/contract/test_tool_surface.py`` compares the registry against a frozen literal, so
    a test tool left on the module singleton would turn that file red. A plain function proves
    the same thing: the marker comes from ``@graceful`` and from nothing else.
    """
    return "not a tool"


def test_every_registered_tool_carries_the_recording_marker() -> None:
    """D-04: "no tool can slip past the recording" is held here rather than claimed.

    The marker is set by ``graceful`` itself, so a new tool that forgets the decorator makes
    this case red and names itself. The name of the inner function is deliberately not the
    check: every decorator in the world calls its inner function ``wrapper``.
    """
    # ``_tool_manager`` is the only place a registered tool still carries its ``fn``.
    tools = mcp._tool_manager.list_tools()

    findings = sorted(
        tool.name for tool in tools if getattr(tool.fn, "__mcp_audited__", False) is not True
    )

    assert findings == [], (
        "a tool without @graceful is a tool call nobody records, and the decorator is the one "
        "place that sees every call with its outcome (D-04): " + ", ".join(findings)
    )
    assert getattr(_undecorated, "__mcp_audited__", False) is False, (
        "the marker has to come from the decorator: a function without it must not carry it, "
        "or the case above would pass for every function in this repository"
    )


def test_the_two_ways_to_the_tool_name_cannot_drift_apart() -> None:
    """The recorder reads ``params['name']`` and falls back to ``fn.__name__``.

    Both are the tool name today because no tool is registered with ``name=``. That is a
    property of today and not a guarantee, and a tool whose two names differed would be
    recorded under one name and refused under the other.
    """
    # ``_tool_manager`` is the only place a registered tool still carries its ``fn``.
    tools = mcp._tool_manager.list_tools()

    findings = sorted(
        f"{tool.name} is registered on {tool.fn.__name__}"
        for tool in tools
        if tool.name != tool.fn.__name__
    )

    assert findings == [], (
        "the registered name and the name of the function have to stay the same word, or the "
        "recorder writes one of them and the allowlist is read for the other: "
        + ", ".join(findings)
    )
