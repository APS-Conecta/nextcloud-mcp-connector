"""The three update tools against a real Nextcloud: alice may, bob may not (gestion-aio#94).

Each family is proven in both directions in one session. The positive half is alice
changing her own object through the tool, which also proves that the precondition this
server sends is the one the app honours (the quoted Notes ETag, the sabre ETag, Deck's
full card body). The negative half is bob aiming the same tool at that object, and the
object reading back as alice left it:

* a note of alice is unknown to bob, so the update is refused as not found;
* a Deck board shared with bob read-only is refused by the board permission before any
  write, and Deck itself refuses the write as well;
* a calendar shared with bob read-only is refused by its ``oc:read-only`` flag before any
  write, for an update and for a new event alike.

Board share, calendar and calendar share are test scaffolding, not connector features:
the connector can neither share nor create a calendar, so those calls go to Nextcloud
directly and every share is taken back at the end, because the permission tests rely on
alice and bob sharing nothing.

Run it with::

    docker compose -f compose.test.yml up -d --wait
    bash scripts/bootstrap_test_nc.sh
    set -a && . ./.env.test && set +a && uv run pytest -m integration -q
"""

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from mcp_connector.config import normalize_base_url
from mcp_connector.errors import REASON_PERMISSION_DENIED, REASON_UNKNOWN_ID, ToolError
from mcp_connector.nextcloud import NcClients, capabilities
from mcp_connector.nextcloud.clients import caldav
from mcp_connector.nextcloud.clients import deck as deck_client
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import calendar as calendar_tools
from mcp_connector.tools import deck as deck_tools
from mcp_connector.tools import notes as notes_tools

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

#: alice's calendar that is shared with bob read-only for the length of one test.
SHARED_CALENDAR = "mcp-readonly-share"


def _clients(base_url: str, user: str, secret: str) -> NcClients:
    capabilities.clear_cache()
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False, timeout=30.0),
        creds=Credentials(base_url=normalize_base_url(base_url), user=user, secret=secret),
    )


@pytest.fixture
async def alice(live_env: dict[str, str | None]) -> AsyncIterator[NcClients]:
    missing = [name for name, value in live_env.items() if not value]
    if missing:
        pytest.skip(f"no test Nextcloud configured (missing: {', '.join(sorted(missing))})")
    assert live_env["user"] != "admin", "integration tests run as a normal user, never as admin"
    clients = _clients(str(live_env["base_url"]), str(live_env["user"]), str(live_env["secret"]))
    async with clients.client:
        yield clients


@pytest.fixture
async def bob(live_env: dict[str, str | None]) -> AsyncIterator[NcClients]:
    base_url = live_env["base_url"]
    user = os.environ.get("NC_MCP_TEST_USER2")
    secret = os.environ.get("NC_MCP_TEST_APP_PASSWORD2")
    if not base_url or not user or not secret:
        pytest.skip("no second test user configured (NC_MCP_TEST_USER2)")
    clients = _clients(str(base_url), user, secret)
    async with clients.client:
        yield clients


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


async def _request(clients: NcClients, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Test scaffolding only: a direct call as this account, outside the connector."""
    return await clients.client.request(
        method, url, auth=httpx.BasicAuth(clients.creds.user, clients.creds.secret), **kwargs
    )


async def test_bob_cannot_update_a_note_of_alice(alice: NcClients, bob: NcClients) -> None:
    assert alice.creds.user != bob.creds.user, "the negative proof needs two accounts"
    created = await notes_tools.create(alice, title=unique("MCP-Update"), content="erste Fassung")
    first = await notes_tools.read(alice, note_id=created["id"])
    assert first["etag"], "Notes API 1.2+ answers an etag for every note"

    updated = await notes_tools.update(
        alice, note_id=created["id"], content="zweite Fassung", etag=first["etag"]
    )
    assert updated["etag"] != first["etag"]

    with pytest.raises(ToolError) as stale:
        await notes_tools.update(alice, note_id=created["id"], content="x", etag=first["etag"])
    assert "changed since it was read" in stale.value.message, "Notes honours the quoted If-Match"

    with pytest.raises(ToolError) as excinfo:
        await notes_tools.update(
            bob, note_id=created["id"], content="von bob", etag=updated["etag"]
        )
    assert excinfo.value.reason == REASON_UNKNOWN_ID

    after = await notes_tools.read(alice, note_id=created["id"])
    assert after["content"] == "zweite Fassung"


async def test_bob_cannot_update_a_card_on_a_board_shared_read_only(
    alice: NcClients, bob: NcClients
) -> None:
    board = (
        await _request(
            alice,
            "POST",
            deck_client.api_url(alice.creds, "/boards"),
            json={"title": unique("MCP-Update Board"), "color": "0082c9"},
            headers=dict(deck_client.DECK_HEADERS),
        )
    ).json()
    board_id = str(board["id"])
    stack = (
        await _request(
            alice,
            "POST",
            deck_client.api_url(alice.creds, f"/boards/{board_id}/stacks"),
            json={"title": "Stapel", "order": 1},
            headers=dict(deck_client.DECK_HEADERS),
        )
    ).json()
    created = await deck_tools.create_card(
        alice, board_id=board_id, stack_id=str(stack["id"]), title="Karte", description="Text"
    )
    updated = await deck_tools.update_card(alice, card_id=created["id"], title="Karte neu")
    assert updated["title"] == "Karte neu"

    acl_url = deck_client.api_url(alice.creds, f"/boards/{board_id}/acl")
    acl = await _request(
        alice,
        "POST",
        acl_url,
        json={
            "type": 0,
            "participant": bob.creds.user,
            "permissionEdit": False,
            "permissionShare": False,
            "permissionManage": False,
        },
        headers=dict(deck_client.DECK_HEADERS),
    )
    assert acl.status_code == 200, acl.text
    try:
        with pytest.raises(ToolError) as excinfo:
            await deck_tools.update_card(bob, card_id=created["id"], title="von bob")
        assert excinfo.value.reason == REASON_PERMISSION_DENIED

        board_part, stack_part, card_part = created["id"].split(":")[1:]
        current = await deck_client.get_card(
            bob.client, bob.creds, board_part, stack_part, card_part
        )
        with pytest.raises(ToolError) as direct:
            await deck_client.update_card(
                bob.client, bob.creds, board_part, stack_part, card_part, current, title="bob"
            )
        assert direct.value.reason == REASON_PERMISSION_DENIED, "Deck refuses the write itself"
    finally:
        await _request(
            alice, "DELETE", f"{acl_url}/{acl.json()['id']}", headers=dict(deck_client.DECK_HEADERS)
        )

    card = await deck_client.get_card(alice.client, alice.creds, board_part, stack_part, card_part)
    assert card["title"] == "Karte neu"
    assert card["description"] == "Text"


def _share_body(sharee: str, *, remove: bool = False) -> str:
    verb = "remove" if remove else "set"
    return (
        '<o:share xmlns:d="DAV:" xmlns:o="http://owncloud.org/ns">'
        f"<o:{verb}><d:href>principal:principals/users/{sharee}</d:href></o:{verb}>"
        "</o:share>"
    )


async def test_bob_cannot_update_an_event_in_a_calendar_shared_read_only(
    alice: NcClients, bob: NcClients
) -> None:
    calendar_url = caldav.calendar_url(alice.creds, SHARED_CALENDAR)
    made = await _request(
        alice,
        "MKCALENDAR",
        calendar_url,
        content=(
            '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
            "<d:displayname>MCP read-only share</d:displayname>"
            '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
            "</c:supported-calendar-component-set></d:prop></d:set></c:mkcalendar>"
        ),
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )
    assert made.status_code in (201, 405), made.text  # 405: it exists from an earlier run

    created = await calendar_tools.create_event(
        alice,
        summary=unique("MCP-Update Termin"),
        start="2026-11-20T09:00:00+01:00",
        end="2026-11-20T10:00:00+01:00",
        calendar=SHARED_CALENDAR,
    )
    updated = await calendar_tools.update_event(alice, event_id=created["id"], location="Raum 2")
    assert updated["location"] == "Raum 2"
    assert updated["confirmed"] is True

    share_headers = {"Content-Type": "application/xml; charset=utf-8"}
    shared = await _request(
        alice, "POST", calendar_url, content=_share_body(bob.creds.user), headers=share_headers
    )
    assert shared.status_code in (200, 204), shared.text
    try:
        bobs = await caldav.discover_calendars(bob.client, bob.creds)
        uri = next(ref.uri for ref in bobs if ref.uri.startswith(f"{SHARED_CALENDAR}_shared_by_"))
        object_name = created["id"].rsplit(":", 1)[-1]
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.update_event(
                bob, event_id=f"event:{uri}:{object_name}", summary="von bob"
            )
        assert excinfo.value.reason == REASON_PERMISSION_DENIED

        with pytest.raises(ToolError) as create:
            await calendar_tools.create_event(
                bob,
                summary="von bob",
                start="2026-11-21T09:00:00+01:00",
                end="2026-11-21T10:00:00+01:00",
                calendar=uri,
            )
        assert create.value.reason == REASON_PERMISSION_DENIED
    finally:
        await _request(
            alice,
            "POST",
            calendar_url,
            content=_share_body(bob.creds.user, remove=True),
            headers=share_headers,
        )

    after = await caldav.get_event(
        alice.client, alice.creds, SHARED_CALENDAR, created["id"].rsplit(":", 1)[-1]
    )
    assert after[0]["summary"] == created["summary"]
    assert after[0]["location"] == "Raum 2"
