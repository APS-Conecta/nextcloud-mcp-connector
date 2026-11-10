"""Every write that runs is recorded in Nextcloud's audit log (gestion-aio#106), at its seam.

The connector calls APS-Conecta/auditoria's OCS route after each write tool call that ran,
with the call's own credentials, the tool's name and the object's id, never its content.
Reads call nothing. A failed audit call is said in the write's answer and logged, and the
write is never repeated. The tools are called through the real registry (``Client(mcp)``),
with Nextcloud mocked at the HTTP boundary.
"""

import base64
import json
import logging
from typing import Any

import httpx
import pytest
import respx
from mcp import Client
from mcp.types import CallToolResult, TextContent

from mcp_connector import config
from mcp_connector.server import mcp
from mcp_connector.tools import files as files_tools
from mcp_connector.tools import notes as notes_tools

BASE = "http://nc.test"
AUDIT = f"{BASE}/ocs/v2.php/apps/auditoria/api/v1/escrituras"
PREGUNTAS = f"{BASE}/ocs/v2.php/apps/estadistica/api/v1/preguntas"
ALICE = "Basic " + base64.b64encode(b"alice:app-password-test").decode()
SECRET = "Texto confidencial de la nota"


def envelope(data: Any, status: int = 200) -> dict[str, Any]:
    return {"ocs": {"meta": {"statuscode": status}, "data": data}}


@pytest.fixture(autouse=True)
def _alice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_URL, BASE)
    monkeypatch.setenv(config.ENV_USER, "alice")
    monkeypatch.setenv(config.ENV_APP_PASSWORD, "app-password-test")
    monkeypatch.delenv("NC_MCP_STATIC_BEARER", raising=False)


async def call(name: str, arguments: dict[str, Any]) -> CallToolResult:
    async with Client(mcp, raise_exceptions=True) as client:
        return await client.call_tool(name, arguments)


def answer(result: CallToolResult) -> dict[str, Any]:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    return json.loads(texts[0])


def audited(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(sent.request.content) for sent in route.calls]


@respx.mock
@pytest.mark.anyio
async def test_an_app_write_is_recorded_once_as_the_user_with_the_tool_and_the_object_id() -> None:
    created = {"id": 5, "titulo": SECRET, "consulta": {}}
    respx.post(PREGUNTAS).mock(return_value=httpx.Response(201, json=envelope(created, 201)))
    audit = respx.post(AUDIT).mock(return_value=httpx.Response(200, json=envelope([])))

    result = await call(
        "estadistica_api_crear_pregunta", {"titulo": SECRET, "consulta": {"medida": {}}}
    )

    assert audited(audit) == [{"tool": "estadistica_api_crear_pregunta", "objectId": "5"}]
    assert audit.calls.last.request.headers["authorization"] == ALICE
    assert audit.calls.last.request.headers["OCS-APIRequest"] == "true"
    assert answer(result) == created


@pytest.mark.parametrize(
    ("tool", "arguments", "logic", "stored", "object_id"),
    [
        (
            "notes_update",
            {"note_id": "note:12", "content": SECRET, "etag": "e1"},
            (notes_tools, "update"),
            {"id": "note:12", "title": "Acta", "etag": "e2"},
            "note:12",
        ),
        (
            "files_upload",
            {"path": "/Acta.md", "content": SECRET},
            (files_tools, "upload"),
            {"path": "/Acta.md", "etag": "e3", "created": True},
            "/Acta.md",
        ),
    ],
)
@respx.mock
@pytest.mark.anyio
async def test_a_connector_write_is_recorded_with_its_id_or_for_a_file_its_path(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any],
    logic: tuple[Any, str],
    stored: dict[str, Any],
    object_id: str,
) -> None:
    async def wrote(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return stored

    monkeypatch.setattr(*logic, wrote)
    audit = respx.post(AUDIT).mock(return_value=httpx.Response(200, json=envelope([])))

    result = await call(tool, arguments)

    assert audited(audit) == [{"tool": tool, "objectId": object_id}]
    assert answer(result) == stored


@respx.mock
@pytest.mark.anyio
async def test_a_read_records_nothing() -> None:
    respx.get(PREGUNTAS).mock(return_value=httpx.Response(200, json=envelope([])))
    audit = respx.post(AUDIT).mock(return_value=httpx.Response(200, json=envelope([])))

    result = await call("estadistica_api_preguntas", {})

    assert not result.is_error, result.content
    assert not audit.called


@respx.mock
@pytest.mark.anyio
async def test_a_write_that_did_not_run_records_nothing() -> None:
    refused = envelope({"message": "Falta el título."}, 422)
    respx.post(PREGUNTAS).mock(return_value=httpx.Response(422, json=refused))
    audit = respx.post(AUDIT).mock(return_value=httpx.Response(200, json=envelope([])))

    result = await call("estadistica_api_crear_pregunta", {"titulo": "", "consulta": {}})

    assert result.is_error
    assert not audit.called


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(403, json=envelope([], 403)), httpx.ConnectError("refused")],
    ids=["refused", "unreachable"],
)
@respx.mock
@pytest.mark.anyio
async def test_a_failed_audit_is_said_in_the_answer_and_logged_and_the_write_runs_once(
    caplog: pytest.LogCaptureFixture, failure: httpx.Response | Exception
) -> None:
    created = {"id": 5, "titulo": "Atenciones", "consulta": {}}
    write = respx.post(PREGUNTAS).mock(
        return_value=httpx.Response(201, json=envelope(created, 201))
    )
    respx.post(AUDIT).mock(side_effect=[failure])

    with caplog.at_level(logging.WARNING):
        result = await call(
            "estadistica_api_crear_pregunta", {"titulo": "Atenciones", "consulta": {}}
        )

    assert not result.is_error, result.content
    said = answer(result)
    assert {key: said[key] for key in created} == created
    assert "audit log" in said["audit"]
    assert write.call_count == 1
    assert "estadistica_api_crear_pregunta" in caplog.text
