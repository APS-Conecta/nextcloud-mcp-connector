"""The APS app tools built from an app's OpenAPI document (gestion-aio#93), at their seam.

``server.apps.register`` turns every document of a directory into tools on the server it is
given. These tests hand it a fixture app, ``tests/fixtures/openapi_muestra.json``, whose
traps are an unmarked read, a marked DELETE, a marked admin route and a PUT marked as a
read, and call the tools through an in-memory MCP client.
"""

import base64
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mcp import Client
from mcp.server import MCPServer
from mcp.types import TextContent

from mcp_connector import config
from mcp_connector.server import apps

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "openapi_muestra.json"
BASE = "http://nc.test"
NOTA = f"{BASE}/ocs/v2.php/apps/muestra/api/v1/notas/7"
ALICE = b"alice:app-password-test"


def muestra() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def server_with(tmp_path: Path, document: dict[str, Any]) -> MCPServer:
    (tmp_path / "muestra.json").write_text(json.dumps(document), encoding="utf-8")
    server = MCPServer("test")
    apps.register(server, tmp_path)
    return server


async def listed(server: MCPServer) -> dict[str, Any]:
    async with Client(server, raise_exceptions=True) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}


@pytest.mark.anyio
async def test_only_the_marked_operations_become_tools(tmp_path: Path) -> None:
    tools = await listed(server_with(tmp_path, muestra()))

    assert set(tools) == {"muestra_api_notas", "muestra_api_crear_nota", "muestra_api_editar_nota"}


@pytest.mark.anyio
async def test_marking_one_more_operation_adds_exactly_one_tool(tmp_path: Path) -> None:
    before = set(await listed(server_with(tmp_path, muestra())))
    marked = copy.deepcopy(muestra())
    marked["paths"]["/ocs/v2.php/apps/muestra/api/v1/notas/{id}"]["get"]["x-aps-ai"] = "read"

    after = set(await listed(server_with(tmp_path, marked)))

    assert after - before == {"muestra_api_nota"}
    assert before <= after


@pytest.mark.anyio
async def test_reads_are_read_only_and_writes_are_classified_as_writes(tmp_path: Path) -> None:
    tools = await listed(server_with(tmp_path, muestra()))
    hints = {
        name: (tool.annotations.read_only_hint, tool.annotations.destructive_hint)
        for name, tool in tools.items()
    }

    assert hints == {
        "muestra_api_notas": (True, None),
        "muestra_api_crear_nota": (False, False),
        "muestra_api_editar_nota": (False, True),
    }


@pytest.mark.anyio
async def test_the_name_and_the_description_come_from_the_operation(tmp_path: Path) -> None:
    tools = await listed(server_with(tmp_path, muestra()))
    edit = tools["muestra_api_editar_nota"]

    assert tools["muestra_api_notas"].description == "Todas las notas del usuario."
    assert edit.description == "Cambia una nota."
    assert set(edit.input_schema["properties"]) == {"id", "titulo"}
    assert edit.input_schema["required"] == ["id"]
    assert edit.input_schema["properties"]["titulo"]["description"] == "el título nuevo"


@respx.mock
@pytest.mark.anyio
async def test_a_tool_sends_its_operation_with_only_the_declared_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.ENV_URL, BASE)
    monkeypatch.setenv(config.ENV_USER, "alice")
    monkeypatch.setenv(config.ENV_APP_PASSWORD, "app-password-test")
    answer = {"ocs": {"meta": {"statuscode": 200}, "data": {"id": 7, "titulo": "Nueva"}}}
    route = respx.put(NOTA).mock(return_value=httpx.Response(200, json=answer))
    server = server_with(tmp_path, muestra())

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "muestra_api_editar_nota", {"id": 7, "titulo": "Nueva", "user": "bob"}
        )

    sent = route.calls.last.request
    assert json.loads(sent.content) == {"titulo": "Nueva"}
    assert sent.headers["OCS-APIRequest"] == "true"
    assert sent.headers["authorization"] == "Basic " + base64.b64encode(ALICE).decode()
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    assert [json.loads(text) for text in texts] == [{"id": 7, "titulo": "Nueva"}]
