"""Each write over the Context Agent's way in leaves one audit.log entry (gestion-aio#106).

Live against ``compose.exapp.yml`` with APS-Conecta/auditoria installed by
``scripts/install_auditoria.sh``. A call signed for alice goes through Nextcloud's AppAPI
proxy (docs/context-agent.md), the connector writes, then calls auditoria's route as alice,
and admin_audit writes the entry. The entries are read back from audit.log inside the
Nextcloud container, from the size the case started at.

    bash scripts/install_auditoria.sh
    set -a && . ./.env.exapp && set +a
    uv run pytest tests/integration/test_audit_log_live.py -m integration
"""

import json
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from topology import NC_CONTAINER

from mcp_connector.nextcloud.credentials import appapi_auth_headers

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PROXY_MCP_PATH = "/index.php/apps/app_api/proxy/mcp_connector/mcp"
AUDIT_LOG = "/var/www/html/data/audit.log"


def _in_nextcloud(script: str) -> str:
    completed = subprocess.run(  # noqa: S603 - a fixed command line of this file, no shell
        ["docker", "exec", "--user", "www-data", NC_CONTAINER, "sh", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout


def _ai_writes_since(offset: int) -> list[dict[str, Any]]:
    """The entries auditoria added to audit.log after ``offset`` bytes."""
    tail = _in_nextcloud(f"tail -c +{offset + 1} {AUDIT_LOG}")
    entries = [json.loads(line) for line in tail.splitlines() if line.strip()]
    return [entry for entry in entries if entry["message"].startswith("AI write")]


@pytest.fixture
def audit_log() -> int:
    """The size of audit.log before the case, the offset its entries start at."""
    if shutil.which("docker") is None:
        pytest.skip("no docker on this host: audit.log stays unread")
    return int(_in_nextcloud(f"if [ -f {AUDIT_LOG} ]; then wc -c < {AUDIT_LOG}; else echo 0; fi"))


@asynccontextmanager
async def _signed_for(exapp_env: dict[str, str], user: str) -> AsyncIterator[Client]:
    """An MCP session over the AppAPI proxy, signed for ``user`` the way the agent signs."""
    headers = appapi_auth_headers(
        user,
        app_id=exapp_env["app_id"],
        app_version=exapp_env["app_version"],
        aa_version=exapp_env["aa_version"],
        app_secret=exapp_env["app_secret"],
    )
    url = exapp_env["base_url"].rstrip("/") + PROXY_MCP_PATH
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        Client(streamable_http_client(url, http_client=http_client)) as client,
    ):
        yield client


def _payload(result: CallToolResult) -> dict[str, Any]:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    assert not result.is_error, texts
    return json.loads(texts[0])


async def test_a_write_leaves_one_entry_with_the_user_the_tool_and_the_object(
    exapp_env: dict[str, str], audit_log: int
) -> None:
    """The argument naming bob changes nothing, and no entry holds the note's content."""
    alice, marker = exapp_env["alice"], uuid.uuid4().hex
    arguments = {"title": f"Acta {marker}", "content": marker, "user": exapp_env["bob"]}
    async with _signed_for(exapp_env, alice) as client:
        written = _payload(await client.call_tool("notes_create", arguments))

    assert "audit" not in written, written
    entries = _ai_writes_since(audit_log)
    message = f'AI write confirmed: user "{alice}", tool "notes_create", object "{written["id"]}"'
    assert [(entry["user"], entry["message"]) for entry in entries] == [(alice, message)]
    assert marker not in json.dumps(entries)


async def test_a_read_leaves_none(exapp_env: dict[str, str], audit_log: int) -> None:
    async with _signed_for(exapp_env, exapp_env["alice"]) as client:
        _payload(await client.call_tool("notes_search", {"query": "Acta"}))

    assert _ai_writes_since(audit_log) == []
