"""Contract test of the APS app tools (gestion-aio#93): only what an app marked is a tool.

Every shipped app document and the fixture app with its traps are built exactly as the
server builds them, and every resulting tool is held against the raw document: an
unmarked operation, a DELETE or an admin route behind a tool fails here, naming it.

Estadística's tools are frozen by name, split into reads and writes: the always-allow list
of the Context Agent names the reads, and a renamed or reclassified tool is a review event.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer

from mcp_connector.server import apps
from mcp_connector.tools import openapi

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "openapi_muestra.json"

ESTADISTICA_READS = frozenset(
    {
        "estadistica_api_fuentes",
        "estadistica_api_fuente",
        "estadistica_api_consultas",
        "estadistica_api_preguntas",
        "estadistica_api_pregunta",
        "estadistica_api_tableros",
        "estadistica_api_tablero",
        "estadistica_api_indicadores",
        "estadistica_api_resumen",
    }
)
ESTADISTICA_WRITES = frozenset(
    {
        "estadistica_api_crear_pregunta",
        "estadistica_api_editar_pregunta",
        "estadistica_api_crear_tablero",
        "estadistica_api_editar_tablero",
    }
)
ESTADISTICA_TOOLS = ESTADISTICA_READS | ESTADISTICA_WRITES


def _documents() -> list[tuple[str, dict[str, Any]]]:
    shipped = sorted(apps.DOCUMENTS.glob("*.json"))
    found = [(path.stem, json.loads(path.read_text(encoding="utf-8"))) for path in shipped]
    return [*found, ("muestra", json.loads(FIXTURE.read_text(encoding="utf-8")))]


def test_no_tool_stands_on_an_unmarked_operation_a_delete_or_an_admin_route() -> None:
    for app, document in _documents():
        for operation in openapi.operations(app, document):
            raw = document["paths"][f"/ocs/v2.php{operation.path}"][operation.method]
            where = f"{operation.name} ({operation.method} {operation.path})"
            assert raw.get("x-aps-ai") in {"read", "write"}, f"{where} is not marked"
            assert operation.method != "delete", f"{where} is a delete"
            prose = raw.get("summary", "") + raw.get("description", "")
            assert "This endpoint requires admin access" not in prose, f"{where} is an admin route"
            assert operation.access == raw["x-aps-ai"], f"{where} lost its marking"


@pytest.mark.anyio
async def test_estadistica_builds_its_marked_reads_as_reads_and_its_writes_as_writes() -> None:
    server = MCPServer("contract")
    apps.register(server)

    async with Client(server, raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools

    reads = {tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint}
    assert reads == ESTADISTICA_READS
    assert {tool.name for tool in tools} - reads == ESTADISTICA_WRITES
