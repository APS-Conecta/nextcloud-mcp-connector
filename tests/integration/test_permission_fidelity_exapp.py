"""The permission promise proven over the whole ExApp chain (AUTH-05, D-28).

``tests/integration/test_permission_fidelity.py`` asks the same question this file asks:
does a tool refuse what a user does not own. The difference is where the credentials go and
where the identity is born. There the credentials reach our own client layer and the user id
is a field we set. Here the credentials are an ordinary Nextcloud app password that travels
to HaRP, and the identity is resolved by HaRP out of that password, injected as an AppAPI
header and only then trusted by the ExApp. The request runs the full path a real user runs:

    MCP client  ->  HaRP  ->  ExApp  ->  impersonation  ->  Nextcloud ACLs

No check in this file builds an ``httpx.BasicAuth`` for Nextcloud. In the chain cases the
only credential is each user's app password in a Basic header on the transport client, the
same header ``tests/compat/modern_client_check.py`` uses. Everything past the header is the
deployed topology: HaRP, the reverse proxy, the ExApp container and Nextcloud's own
permission check. A green row therefore cannot be explained by our client layer; it can only
be explained by the chain resolving the right user and Nextcloud enforcing the right ACLs.

The order of the checks is deliberate, so an empty answer can never pass for a boundary:

1. Guard: alice and bob are really two different accounts.
2. ``tools/list`` over the chain answers the full tool surface for both, so the chain carries.
3. Positive control (files): alice creates a file and finds it again.
4. Leak (``files_search``): bob does not find alice's file.
5. Positive control (notes) plus leak (``notes_search``): alice finds her note, bob does not.
6. Positive control and leak (``unified_search``): alice finds her content, bob finds neither.
7. Direct access (``files_read``): bob, knowing the exact path, is refused, not served content.

The Tables block at the end of the file asks the same question one layer lower, and it says
so rather than pretending otherwise. A table and its column are not connector capabilities
(create-only, threat T-08-11), so the scaffolding has to call Nextcloud directly anyway; and
the interesting boundary of that family is the impersonation seam itself, the way
``tests/integration/test_exapp_dav_matrix.py`` measures it. Both identities are therefore
built as ``Credentials`` in ``MODE_APPAPI`` with ``APP_SECRET`` as the only credential, one
object per account, and no app password is read as a credential source for those three
checks. The properties that make them proof stay the same: the positive half runs in the
same session as the negative half, and the negative half uses the real table id rather than
a guessed one.

The Talk block after it asks the same question a third time, on the same seam and with the
same two credential objects (T-09-40): alice reads the history of her own test conversation,
bob does not have it in his list at all, and bob reaches neither its history nor a send with
the real token. The comparison point there is the message count from alice's side before and
after bob's two attempts, because no tool of this server can remove a message again.

The AppAPI proxy block asks it over the way in of Nextcloud's Context Agent
(docs/context-agent.md): an ExApp signature for one user, authenticated by Nextcloud and
forwarded by its proxy. Alice finds her file, bob does not even when an argument names
alice, and a signature without a user is refused.

The app id ``mcp_connector`` is frozen (docs/app-id-freeze.md), so the HaRP route
``/exapps/mcp_connector/mcp`` is a constant here rather than an interpolation.

Run it against the running HaRP topology::

    export HP_SHARED_KEY="$(openssl rand -hex 32)"
    docker compose -p nc-mcp-exapp -f compose.exapp.yml up -d --wait
    bash scripts/bootstrap_exapp.sh
    set -a && . ./.env.exapp && set +a
    uv run pytest tests/integration/test_permission_fidelity_exapp.py -m integration -q
"""

import base64
import json
import os
import uuid
import warnings
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_connector.config import normalize_base_url
from mcp_connector.errors import ToolError
from mcp_connector.nextcloud import NcClients
from mcp_connector.nextcloud.clients import ocs
from mcp_connector.nextcloud.clients import tables as tables_client
from mcp_connector.nextcloud.clients import talk as talk_client
from mcp_connector.nextcloud.credentials import MODE_APPAPI, Credentials, appapi_auth_headers
from mcp_connector.tools import tables as tables_tools
from mcp_connector.tools import talk as talk_tools

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# The app id is frozen (docs/app-id-freeze.md); the HaRP route never changes, so it is a
# literal here. The env carries the same value in APP_ID and the fixture asserts they agree.
EXAPP_MCP_PATH = "/exapps/mcp_connector/mcp"

# A confidential line that only alice's file carries, kept ASCII so a leak assertion is exact
# (the same choice tests/integration/test_exapp_dav_matrix.py makes for its negative case).
SECRET_LINE = "Vertrauliche Notiz von alice, Strassenbudget."


def _basic(user: str, secret: str) -> str:
    """A Basic header, exactly what a client hands to HaRP; HaRP resolves the identity."""
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


@asynccontextmanager
async def _mcp_session_at(url: str, headers: dict[str, str]) -> AsyncIterator[Client]:
    """An MCP session over Streamable HTTP at ``url``, every request carrying ``headers``.

    The headers live on the httpx client (mcp 2.x has no headers argument on the
    transport), so every request of the session carries the same credential.
    """
    async with httpx2.AsyncClient(
        headers=headers, timeout=httpx2.Timeout(30.0, read=300.0)
    ) as http_client:
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport) as client:
            yield client


def _mcp_session(base: str, user: str, secret: str) -> AbstractAsyncContextManager[Client]:
    """An MCP session against the ExApp over HaRP, with this user's app password."""
    return _mcp_session_at(
        base.rstrip("/") + EXAPP_MCP_PATH, {"Authorization": _basic(user, secret)}
    )


def _payload(result: Any) -> dict[str, Any]:
    """Decode the compact JSON a tool answers with (structured_output=False)."""
    assert not result.is_error, f"the tool call ended in an error: {_texts(result)!r}"
    texts = _texts(result)
    assert texts, f"the tool answered without any text content: {result!r}"
    data = json.loads(texts[0])
    assert isinstance(data, dict), f"the tool did not answer with an object: {data!r}"
    return data


def _texts(result: Any) -> list[str]:
    return [c.text for c in result.content if getattr(c, "text", None) is not None]


@pytest.fixture
def chain_env() -> dict[str, str]:
    """The values scripts/bootstrap_exapp.sh writes into .env.exapp for the full chain test.

    ``NC_MCP_URL`` is the reverse proxy front where the HaRP path lives; the two app passwords
    are what each user hands to HaRP. When one is missing the test skips with the variable
    named, the shape the rest of the integration suite uses, so the default run stays green
    without the topology.
    """
    required = {
        "base": "NC_MCP_URL",
        "app_id": "APP_ID",
        "alice": "NC_MCP_TEST_USER",
        "alice_pw": "NC_MCP_TEST_APP_PASSWORD",
        "bob": "NC_MCP_TEST_USER2",
        "bob_pw": "NC_MCP_TEST_APP_PASSWORD2",
    }
    values = {key: (os.environ.get(name) or "").strip() for key, name in required.items()}
    missing = sorted(required[key] for key, value in values.items() if not value)
    if missing:
        pytest.skip(f"no ExApp topology configured (missing: {', '.join(missing)})")
    assert values["app_id"] == "mcp_connector", (
        f"the app id is frozen as mcp_connector but APP_ID is {values['app_id']!r}"
    )
    assert values["alice"] != "admin", "the chain test runs as normal users, never as admin"
    return values


@pytest.fixture
async def alices_content(chain_env: dict[str, str]) -> dict[str, str]:
    """One file and one note, created by alice over the full chain, each with a unique marker.

    The creates are themselves part of the proof: they run as impersonated alice through HaRP,
    so a later find by alice and a later miss by bob both speak about objects the chain made.
    """
    marker = f"nurfueralice{uuid.uuid4().hex[:10]}"
    file_path = f"/{marker}.md"
    note_title = f"alicenote{uuid.uuid4().hex[:10]}"
    async with _mcp_session(chain_env["base"], chain_env["alice"], chain_env["alice_pw"]) as c:
        uploaded = _payload(
            await c.call_tool(
                "files_upload",
                {"path": file_path, "content": f"# {marker}\n{SECRET_LINE}\n"},
            )
        )
        assert uploaded.get("path") == file_path, f"alice's upload did not land: {uploaded!r}"
        note = _payload(
            await c.call_tool(
                "notes_create",
                {"title": note_title, "content": "created over the ExApp chain as alice"},
            )
        )
        assert note.get("id"), f"alice's note was not created: {note!r}"
    return {"marker": marker, "file_path": file_path, "note_title": note_title}


async def test_alice_and_bob_are_two_different_accounts(chain_env: dict[str, str]) -> None:
    """Guard against a false pass: two identical accounts make every leak test below empty."""
    assert chain_env["alice"] != chain_env["bob"], (
        "the negative proof needs two accounts; NC_MCP_TEST_USER2 points at the same user"
    )
    assert chain_env["alice_pw"] != chain_env["bob_pw"], (
        "both accounts carry the same app password; the chain would resolve the same identity"
    )


async def test_the_chain_carries_the_full_tool_surface_for_both(chain_env: dict[str, str]) -> None:
    """tools/list over HaRP, the ExApp and back, answers the full surface for alice and bob.

    Without this, an empty leak result could mean a broken chain rather than an enforced
    boundary. Both accounts reach the same tool count, so the chain is live for each of them.
    """
    async with _mcp_session(chain_env["base"], chain_env["alice"], chain_env["alice_pw"]) as c:
        alice_tools = {tool.name for tool in (await c.list_tools()).tools}
    async with _mcp_session(chain_env["base"], chain_env["bob"], chain_env["bob_pw"]) as c:
        bob_tools = {tool.name for tool in (await c.list_tools()).tools}

    assert alice_tools == bob_tools, "the two accounts see different tools over the chain"
    assert "files_search" in alice_tools
    assert "unified_search" in alice_tools
    assert len(alice_tools) >= 14, f"the chain served a short tool list: {sorted(alice_tools)}"


async def test_alice_finds_her_own_file_over_the_chain(
    chain_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Positive control (files): without it, an empty answer for bob would prove nothing."""
    async with _mcp_session(chain_env["base"], chain_env["alice"], chain_env["alice_pw"]) as c:
        result = _payload(await c.call_tool("files_search", {"query": alices_content["marker"]}))

    paths = {item["path"] for item in result["items"]}
    assert alices_content["file_path"] in paths, f"alice cannot find her own file: {result!r}"


async def test_bob_does_not_find_alices_file_over_the_chain(
    chain_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Leak test (files_search): bob's WebDAV SEARCH runs in bob's home, never in alice's."""
    async with _mcp_session(chain_env["base"], chain_env["bob"], chain_env["bob_pw"]) as c:
        result = _payload(await c.call_tool("files_search", {"query": alices_content["marker"]}))

    assert result["items"] == [], f"bob sees a file that is not his over the chain: {result!r}"
    assert result["count"] == 0


async def test_alice_finds_her_own_note_over_the_chain(
    chain_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Positive control (notes): the note provider answers for the account that owns it."""
    title = alices_content["note_title"]
    async with _mcp_session(chain_env["base"], chain_env["alice"], chain_env["alice_pw"]) as c:
        result = _payload(await c.call_tool("notes_search", {"query": title}))

    titles = {entry.get("title") for entry in result["results"]}
    assert title in titles, f"alice cannot find her own note: {result!r}"


async def test_bob_does_not_find_alices_note_over_the_chain(
    chain_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Leak test (notes_search): the note provider must not answer across accounts."""
    title = alices_content["note_title"]
    async with _mcp_session(chain_env["base"], chain_env["bob"], chain_env["bob_pw"]) as c:
        result = _payload(await c.call_tool("notes_search", {"query": title}))

    assert result["results"] == [], f"bob sees a note that is not his over the chain: {result!r}"
    assert result["count"] == 0


async def test_alice_finds_her_content_over_unified_search(
    chain_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Positive control (unified_search): the widest read path answers for the owner."""
    async with _mcp_session(chain_env["base"], chain_env["alice"], chain_env["alice_pw"]) as c:
        by_file = _payload(await c.call_tool("unified_search", {"query": alices_content["marker"]}))

    assert by_file["count"] >= 1, f"alice's own content is invisible to unified search: {by_file!r}"


async def test_bob_finds_neither_over_unified_search(
    chain_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Leak test (unified_search): the provider fan-out is the widest path and stays honest.

    Both of alice's markers are queried, so a leak from any single provider would show up.
    """
    async with _mcp_session(chain_env["base"], chain_env["bob"], chain_env["bob_pw"]) as c:
        by_file = _payload(await c.call_tool("unified_search", {"query": alices_content["marker"]}))
        by_note = _payload(
            await c.call_tool("unified_search", {"query": alices_content["note_title"]})
        )

    assert by_file["results"] == [], f"a provider leaked alice's file to bob: {by_file!r}"
    assert by_note["results"] == [], f"a provider leaked alice's note to bob: {by_note!r}"


async def test_bob_cannot_read_alices_file_even_knowing_the_exact_path(
    chain_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Direct access: the case where search hygiene does not help, the path is already known.

    Every request of bob's session carries bob's app password, so the chain resolves bob and
    Nextcloud resolves the path inside bob's home, where the file is not. The tool refuses
    instead of reaching into another home, and the refusal never carries the file content.
    """
    file_path = alices_content["file_path"]
    async with _mcp_session(chain_env["base"], chain_env["bob"], chain_env["bob_pw"]) as c:
        try:
            result = await c.call_tool("files_read", {"path": file_path})
        except Exception as exc:  # a raised protocol error is also a refusal, not content
            message = str(exc)
        else:
            assert result.is_error, f"bob read alice's file over the chain: {_texts(result)!r}"
            message = " ".join(_texts(result))
    assert SECRET_LINE not in message, "the refusal carried the content of alice's file"
    assert "Vertrauliche" not in message, "the refusal carried the content of alice's file"


# ---------------------------------------------------------------------------------------
# The AppAPI proxy, the way in of Nextcloud's Context Agent (docs/context-agent.md,
# gestion-aio#89). The caller signs as an ExApp for one user, Nextcloud authenticates that
# signature and forwards the call with its own signature for the user. The signing ExApp is
# this one, the only one whose secret the topology hands out; Nextcloud treats the
# signature of every enabled ExApp the same way, the Context Agent's included.
# ---------------------------------------------------------------------------------------

PROXY_MCP_PATH = "/index.php/apps/app_api/proxy/mcp_connector/mcp"


def _signed_for(exapp_env: dict[str, str], user: str) -> dict[str, str]:
    """The four AppAPI headers an ExApp sends to Nextcloud on behalf of ``user``."""
    return appapi_auth_headers(
        user,
        app_id=exapp_env["app_id"],
        app_version=exapp_env["app_version"],
        aa_version=exapp_env["aa_version"],
        app_secret=exapp_env["app_secret"],
    )


async def test_alice_finds_her_own_file_over_the_appapi_proxy(
    exapp_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Positive control: the proxy carries a whole MCP session for the signed user."""
    url = exapp_env["base_url"].rstrip("/") + PROXY_MCP_PATH
    async with _mcp_session_at(url, _signed_for(exapp_env, exapp_env["alice"])) as c:
        result = _payload(await c.call_tool("files_search", {"query": alices_content["marker"]}))

    paths = {item["path"] for item in result["items"]}
    assert alices_content["file_path"] in paths, f"alice cannot find her own file: {result!r}"


async def test_bob_does_not_find_alices_file_over_the_appapi_proxy_even_naming_her(
    exapp_env: dict[str, str], alices_content: dict[str, str]
) -> None:
    """Leak test: a call signed for bob runs as bob, whichever user its arguments name."""
    url = exapp_env["base_url"].rstrip("/") + PROXY_MCP_PATH
    arguments = {"query": alices_content["marker"], "user": exapp_env["alice"]}
    async with _mcp_session_at(url, _signed_for(exapp_env, exapp_env["bob"])) as c:
        result = _payload(await c.call_tool("files_search", arguments))

    assert result["items"] == [], f"bob sees alice's file over the proxy: {result!r}"


async def test_the_appapi_proxy_refuses_a_call_without_a_user(exapp_env: dict[str, str]) -> None:
    """A signature without a user is the app context, and the connector serves it nothing."""
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "app-context", "version": "0"},
        },
    }
    headers = {**_signed_for(exapp_env, ""), "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            exapp_env["base_url"].rstrip("/") + PROXY_MCP_PATH, json=initialize, headers=headers
        )

    assert response.status_code == 401, f"the app context was served: {response.status_code}"
    assert "mcp-session-id" not in response.headers


# ---------------------------------------------------------------------------------------
# Tables (T-08-26). One layer lower than the chain above, on the impersonation seam, and
# with two credential objects in MODE_APPAPI as the only source of both identities.
# ---------------------------------------------------------------------------------------

TABLES_TITLE = "MCP-Test Berechtigungstreue"
TABLES_COLUMN = "Notiz"


def _appapi_clients(exapp_env: dict[str, str], user: str) -> NcClients:
    """Build the impersonating clients for one user, ``APP_SECRET`` as the only credential.

    Mirrors ``deps._credentials_from_appapi`` and the factory of
    ``tests/integration/test_exapp_dav_matrix.py``: same base URL, same fields, same mode.
    The user id is the whole difference between impersonating alice and impersonating bob,
    and that difference is what the three checks below measure.
    """
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False, timeout=30.0),
        creds=Credentials(
            base_url=normalize_base_url(exapp_env["base_url"]),
            user=user,
            secret=exapp_env["app_secret"],
            mode=MODE_APPAPI,
            app_id=exapp_env["app_id"],
            app_version=exapp_env["app_version"],
            aa_version=exapp_env["aa_version"],
        ),
    )


@pytest.fixture
async def alice_tables(exapp_env: dict[str, str]) -> AsyncIterator[NcClients]:
    clients = _appapi_clients(exapp_env, exapp_env["alice"])
    async with clients.client:
        yield clients


@pytest.fixture
async def bob_tables(exapp_env: dict[str, str]) -> AsyncIterator[NcClients]:
    clients = _appapi_clients(exapp_env, exapp_env["bob"])
    async with clients.client:
        yield clients


async def _scaffold(clients: NcClients, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Test scaffolding only: the connector creates neither tables nor columns (T-08-11).

    The credential is still the AppAPI one, because the point of this file is that no user
    password authenticates anything here. A status of 400 or above skips instead of failing:
    an account that may not prepare a table says nothing about the boundary under test.
    """
    response = await ocs.ocs_post(clients.client, clients.creds, path, body)
    if response.status_code >= 400:
        pytest.skip(f"this account may not prepare a tables table ({response.status_code})")
    payload = ocs.parse_ocs(response, what="the prepared table or column")
    assert isinstance(payload, dict), f"the setup call did not answer with an object: {payload!r}"
    return payload


@pytest.fixture
async def alices_table(alice_tables: NcClients) -> dict[str, str]:
    """One table of alice's, with one text column and one row nobody else wrote.

    Idempotent by title (threat T-08-30): a tenth run reuses the table and adds one row. The
    row is written through ``tables_create_row`` under alice's impersonation, so the
    positive half below speaks about an object this seam really made.

    The text column carries a ``subtype``, and that is not cosmetic: Tables resolves a
    business class from type plus subtype, ``TextBusiness`` does not exist, and a text column
    created without one turns every read of the whole table into a 500.
    """
    tables = await tables_client.get_tables(alice_tables.client, alice_tables.creds)
    table = next((item for item in tables if item.get("title") == TABLES_TITLE), None)
    if table is None:
        table = await _scaffold(
            alice_tables, f"{tables_client.V2_PREFIX}/tables", {"title": TABLES_TITLE}
        )
    table_id = str(table["id"])

    columns = await tables_client.get_columns(alice_tables.client, alice_tables.creds, table_id)
    if TABLES_COLUMN not in {str(column.get("title")) for column in columns}:
        await _scaffold(
            alice_tables,
            f"{tables_client.V2_PREFIX}/columns/text",
            {
                "baseNodeId": int(table_id),
                "baseNodeType": "table",
                "title": TABLES_COLUMN,
                "subtype": "line",
            },
        )

    marker = f"nurfueralice{uuid.uuid4().hex[:10]}"
    created = await tables_tools.create_row(
        alice_tables,
        table_id=table_id,
        values=json.dumps({TABLES_COLUMN: f"{marker} Grüße aus Hamburg, Straße 1"}),
    )
    assert created["id"], f"alice's row was not created: {created!r}"
    return {"table_id": table_id, "marker": marker}


async def _rows(clients: NcClients, table_id: str) -> list[dict[str, Any]]:
    """Every row of the table under this identity, page by page."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(50):
        page = await tables_tools.browse(
            clients, level="rows", table_id=table_id, limit=tables_tools.MAX_LIMIT, cursor=cursor
        )
        rows.extend(page["results"])
        cursor = page.get("next")
        if not cursor:
            break
    return rows


async def test_alice_finds_her_own_table_and_her_own_row(
    alice_tables: NcClients, alices_table: dict[str, str]
) -> None:
    """Positive control (tables): without it, an empty answer for bob would prove nothing."""
    listed = await tables_tools.browse(alice_tables, level="tables")
    ids = [str(entry["id"]) for entry in listed["results"]]
    assert alices_table["table_id"] in ids, f"alice cannot see her own table: {listed!r}"

    rows = await _rows(alice_tables, alices_table["table_id"])
    values = [str(row.get(TABLES_COLUMN, "")) for row in rows]
    assert any(alices_table["marker"] in value for value in values), (
        f"alice cannot read back her own row: {values!r}"
    )


async def test_bob_neither_lists_alices_table_nor_reads_it_by_its_real_id(
    bob_tables: NcClients, alices_table: dict[str, str]
) -> None:
    """The leak test, and the id is the real one rather than a guessed one.

    Two halves, because each of them alone would leave a way out: the table must not appear
    in bob's own list, and asking for it by its exact id must not answer with alice's
    content. The refusal comes out of Nextcloud's own permission layer and arrives at the
    model as a sentence with a next step.
    """
    listed = await tables_tools.browse(bob_tables, level="tables")
    ids = [str(entry["id"]) for entry in listed["results"]]
    titles = [entry["title"] for entry in listed["results"]]
    assert alices_table["table_id"] not in ids, f"bob lists a table that is not his: {listed!r}"
    assert TABLES_TITLE not in titles, f"bob lists a table that is not his: {listed!r}"

    with pytest.raises(ToolError) as excinfo:
        await tables_tools.browse(bob_tables, level="rows", table_id=alices_table["table_id"])

    assert excinfo.value.hint, "a refusal without a next step is a dead end for the model"
    said = f"{excinfo.value.message} {excinfo.value.hint}"
    assert alices_table["marker"] not in said, "the refusal carried the content of alice's row"


async def test_bob_cannot_write_into_alices_table_and_leaves_no_row_behind(
    alice_tables: NcClients, bob_tables: NcClients, alices_table: dict[str, str]
) -> None:
    """The write half of the boundary: refused before the first byte, and provably so.

    Counting alice's rows before and after is the part that makes this a proof rather than a
    reading of the error message: a refusal that still wrote a row would be worse than a
    silent failure, because no tool of this server can remove a row again (T-08-11).
    """
    table_id = alices_table["table_id"]
    before = len(await _rows(alice_tables, table_id))

    with pytest.raises(ToolError) as excinfo:
        await tables_tools.create_row(
            bob_tables, table_id=table_id, values=json.dumps({TABLES_COLUMN: "bob war hier"})
        )
    assert excinfo.value.hint, "a refusal without a next step is a dead end for the model"

    after = await _rows(alice_tables, table_id)
    assert len(after) == before, f"bob's refused write still changed alice's table: {after!r}"
    assert not any("bob war hier" in str(row.get(TABLES_COLUMN, "")) for row in after), (
        f"bob's content landed in alice's table: {after!r}"
    )


# ---------------------------------------------------------------------------------------
# Talk (T-09-40). The same layer as the Tables block above and for the same reason: the
# interesting boundary of this family is the impersonation seam itself, so both identities are
# built as ``Credentials`` in ``MODE_APPAPI`` with ``APP_SECRET`` as the only credential, one
# object per account, and no app password authenticates any of the three checks.
#
# A conversation is not a capability of this connector either (create-only, threat T-09-03),
# so the scaffolding is not built here: ``scripts/bootstrap_exapp.sh`` creates alice's two test
# conversations with ``occ`` and publishes their names in ``.env.exapp``. Without them these
# checks skip with the variable named.
# ---------------------------------------------------------------------------------------

TALK_ROOM_ENV = "NC_MCP_TEST_TALK_ROOM"
TALK_INTRUSION = "bob war hier"


def measured(what: str) -> None:
    """Make a measurement visible in the test report of a passing run.

    The status code the instance answers bob with is a measurement and not an expectation, and
    a green run is exactly when that number is wanted.
    """
    warnings.warn(f"measured: {what}", stacklevel=2)


@pytest.fixture
async def alice_talk(exapp_env: dict[str, str]) -> AsyncIterator[NcClients]:
    """One impersonating client per account, and separate objects from the Tables pair.

    The seam is the same, the objects are not: a Talk check that depended on ``alice_tables``
    would drag the table scaffolding of that block into its own preconditions, and a skip
    there would then look like a statement about Talk.
    """
    clients = _appapi_clients(exapp_env, exapp_env["alice"])
    async with clients.client:
        yield clients


@pytest.fixture
async def bob_talk(exapp_env: dict[str, str]) -> AsyncIterator[NcClients]:
    clients = _appapi_clients(exapp_env, exapp_env["bob"])
    async with clients.client:
        yield clients


@pytest.fixture
async def alices_conversation(alice_talk: NcClients) -> dict[str, str]:
    """The token and the name of alice's writable test conversation, or a skip that names it.

    The token comes out of alice's own list and is therefore the real one. That is what makes
    the negative half below a proof: a guessed token would be refused by the shape guard of the
    client and would measure nothing about the boundary.
    """
    name = (os.environ.get(TALK_ROOM_ENV) or "").strip()
    if not name:
        pytest.skip(f"no Talk scaffolding configured (missing: {TALK_ROOM_ENV})")
    listed = await talk_tools.browse(alice_talk, level="conversations", limit=talk_tools.MAX_LIMIT)
    entry = next((item for item in listed["results"] if str(item["name"]) == name), None)
    if entry is None:
        pytest.skip(
            f"the conversation {name!r} does not exist for alice; run scripts/bootstrap_exapp.sh"
        )
    return {"token": str(entry["token"]), "name": name}


async def _message_count(clients: NcClients, token: str) -> int:
    """The number of readable messages of one conversation under this identity."""
    answer = await talk_tools.browse(
        clients, level="messages", token=token, limit=talk_tools.MAX_LIMIT
    )
    return int(answer["count"])


async def _talk_status(clients: NcClients, token: str, message: str | None = None) -> int:
    """What the instance itself answers this identity, as a number and not as an expectation.

    Test scaffolding, and deliberately not what the connector does: ``tools/talk.py`` looks a
    token up in the account's own list and refuses an unknown one with its own sentence before
    any Talk path is built (T10), which is why the refusals below produce no status code at
    all. Phase 8 measured 404 rather than 403 for a foreign Tables object, so Nextcloud does
    not give away the existence of somebody else's object; this call records which of the two
    Talk picks instead of writing it into an assertion.

    A single conversation route with a token bob may not see is also what registers a brute
    force attempt for the source IP, which is why the connector never builds it. One such call
    per run is acceptable here: the bootstrap switches that counter off on this throwaway
    topology, and the comment says so rather than leaving the reader to wonder.
    """
    path = f"{talk_client.CHAT_PREFIX}/{token}"
    if message is None:
        response = await ocs.ocs_get(
            clients.client,
            clients.creds,
            path,
            params={**talk_client.READ_ONLY_PARAMS, "limit": 5},
        )
    else:
        response = await ocs.ocs_post(clients.client, clients.creds, path, {"message": message})
    return response.status_code


async def test_alice_finds_her_own_conversation_and_reads_its_history(
    alice_talk: NcClients, alices_conversation: dict[str, str]
) -> None:
    """Positive control (Talk): without it, an empty answer for bob would prove nothing.

    It also answers the one question this seam had left open for the family: whether Talk's OCS
    routes are reachable at all under pure AppAPI impersonation, without any user password
    anywhere in the request.
    """
    token = alices_conversation["token"]
    answer = await talk_tools.browse(
        alice_talk, level="messages", token=token, limit=talk_tools.MAX_LIMIT
    )

    assert answer["token"] == token
    assert answer["conversation"] == alices_conversation["name"], (
        f"the envelope names another conversation than the one asked for: {answer!r}"
    )
    measured(
        f"alice reads {token} under impersonation: count={answer['count']} "
        f"conversation={answer['conversation']!r}"
    )


async def test_bob_does_not_list_alices_conversation(
    bob_talk: NcClients, alices_conversation: dict[str, str]
) -> None:
    """The first half of the boundary: it is not in his own list at all."""
    listed = await talk_tools.browse(bob_talk, level="conversations", limit=talk_tools.MAX_LIMIT)

    tokens = [str(entry["token"]) for entry in listed["results"]]
    names = [str(entry["name"]) for entry in listed["results"]]
    assert alices_conversation["token"] not in tokens, (
        f"bob lists a conversation that is not his: {listed!r}"
    )
    assert alices_conversation["name"] not in names, (
        f"bob lists a conversation that is not his: {listed!r}"
    )
    measured(f"bob's own conversation list: {len(tokens)} entries, {names}")


async def test_bob_reaches_neither_the_history_nor_the_send_of_alices_conversation(
    alice_talk: NcClients, bob_talk: NcClients, alices_conversation: dict[str, str]
) -> None:
    """The second half, with the real token, and it leaves nothing behind.

    Counting the messages from alice's side before and after is what makes this a proof rather
    than a reading of two error messages: a refusal that still posted would be worse than a
    silent failure, because no tool of this server can remove a message again (threat
    T-09-03).

    Both refusals come out of our own list check, so neither of them carries a status code of
    the instance. The two numbers the instance itself answers bob with are measured beside them
    and named as measurements, in the shape phase 8 established for Tables.
    """
    token = alices_conversation["token"]
    before = await _message_count(alice_talk, token)

    with pytest.raises(ToolError) as read_error:
        await talk_tools.browse(bob_talk, level="messages", token=token, limit=20)
    assert read_error.value.hint, "a refusal without a next step is a dead end for the model"

    with pytest.raises(ToolError) as write_error:
        await talk_tools.send(bob_talk, token=token, message=TALK_INTRUSION)
    assert write_error.value.hint, "a refusal without a next step is a dead end for the model"

    read_status = await _talk_status(bob_talk, token)
    write_status = await _talk_status(bob_talk, token, message=TALK_INTRUSION)
    assert read_status >= 400, (
        f"the instance served alice's history to bob with status {read_status}"
    )
    assert write_status >= 400, (
        f"the instance accepted a message from bob into alice's conversation ({write_status})"
    )

    after = await talk_tools.browse(
        alice_talk, level="messages", token=token, limit=talk_tools.MAX_LIMIT
    )
    assert after["count"] == before, (
        f"bob's refused attempts changed alice's history: {before} -> {after['count']}"
    )
    assert not any(TALK_INTRUSION in str(item["message"]) for item in after["results"]), (
        f"bob's content landed in alice's conversation: {after!r}"
    )
    measured(
        f"bob against {token}: our own refusals were "
        f"{read_error.value.message!r} and {write_error.value.message!r}; the instance itself "
        f"answered GET {read_status} and POST {write_status}; alice's message count stayed "
        f"{before}"
    )
