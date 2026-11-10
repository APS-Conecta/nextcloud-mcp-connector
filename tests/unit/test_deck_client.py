"""Unit tests for the Deck REST v1.0 client, all paths.

Two properties of this client are contract and not implementation detail, so they are
tested first: every request, including a plain GET, carries ``OCS-APIRequest: true`` and
``Content-Type: application/json`` (pitfall 9), and a failure is read as Deck's own
``{"status": 4xx, "message": "..."}`` body rather than through the OCS envelope parser.

The rest is the usual catalogue: happy path, the local guards that keep a doomed request
from ever leaving (title length, empty title, unusable date, non-numeric id), the two
permission-relevant statuses 403 and 404, and the HTML login page that an instance answers
when the mandatory header is missing.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mcp_connector.errors import ToolError
from mcp_connector.nextcloud.clients import deck as deck_client
from mcp_connector.nextcloud.credentials import Credentials

BASE = "http://nc.test"
USER = "alice"
SECRET = "app-password-test"

DECK_BASE = f"{BASE}/index.php/apps/deck/api/v1.0"
BOARDS_URL = f"{DECK_BASE}/boards"
STACKS_URL = f"{DECK_BASE}/boards/2/stacks"
CARDS_URL = f"{DECK_BASE}/boards/2/stacks/11/cards"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def creds() -> Credentials:
    return Credentials(BASE, USER, SECRET)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False)


CREATED_CARD = {
    "id": 104,
    "title": "Neue Karte",
    "description": "",
    "stackId": 11,
    "type": "plain",
    "order": 999,
    "duedate": None,
    "owner": "alice",
    "archived": False,
}


@pytest.mark.anyio
async def test_a_get_carries_both_mandatory_headers(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    """D-18 and pitfall 9: Deck wants both headers even where there is no body."""
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(BOARDS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_boards.json"))
        )
        await deck_client.get_boards(client, creds)

    request = route.calls[0].request
    assert request.headers["OCS-APIRequest"] == "true"
    assert request.headers["Content-Type"] == "application/json"


@pytest.mark.anyio
async def test_get_boards_returns_id_title_and_permissions(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(BOARDS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_boards.json"))
        )
        boards = await deck_client.get_boards(client, creds)

    assert [board["id"] for board in boards] == [2, 5]
    assert boards[0]["title"] == "Projekt MCP"
    assert boards[0]["permissions"]["PERMISSION_EDIT"] is True
    assert boards[1]["permissions"]["PERMISSION_EDIT"] is False
    assert "details" not in route.calls[0].request.url.params


@pytest.mark.anyio
async def test_get_boards_with_details_asks_for_them(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(BOARDS_URL).mock(return_value=httpx.Response(200, json=[]))
        boards = await deck_client.get_boards(client, creds, details=True)

    assert boards == []
    assert route.calls[0].request.url.params["details"] == "true"


@pytest.mark.anyio
async def test_get_stacks_delivers_the_cards_of_a_board_in_one_request(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    """The whole reason deck_browse needs no N+1: the stacks answer carries the cards."""
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(STACKS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_stacks.json"))
        )
        stacks = await deck_client.get_stacks(client, creds, 2)

    assert route.call_count == 1
    assert [stack["title"] for stack in stacks] == ["To Do", "Erledigt"]
    assert [card["id"] for card in stacks[0]["cards"]] == [101, 102]
    assert stacks[0]["cards"][1]["duedate"] == "2026-09-01T10:00:00+00:00"
    assert stacks[1]["cards"][0]["description"] is None


@pytest.mark.anyio
async def test_get_board_reads_one_board(client: httpx.AsyncClient, creds: Credentials) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BOARDS_URL}/2").mock(
            return_value=httpx.Response(200, json=fixture("deck_boards.json")[0])
        )
        board = await deck_client.get_board(client, creds, 2)

    assert board["title"] == "Projekt MCP"


@pytest.mark.anyio
async def test_get_card_reads_one_card(client: httpx.AsyncClient, creds: Credentials) -> None:
    card = fixture("deck_stacks.json")[0]["cards"][0]
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{CARDS_URL}/101").mock(return_value=httpx.Response(200, json=card))
        result = await deck_client.get_card(client, creds, 2, 11, 101)

    assert result["title"] == "Deck-Client bauen"
    assert route.calls[0].request.headers["OCS-APIRequest"] == "true"


@pytest.mark.anyio
async def test_create_card_posts_a_plain_card(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(CARDS_URL).mock(return_value=httpx.Response(200, json=CREATED_CARD))
        created = await deck_client.create_card(client, creds, 2, 11, title="Neue Karte")

    assert created["id"] == 104
    body = json.loads(route.calls[0].request.content)
    assert body == {"title": "Neue Karte", "type": "plain", "order": 999}
    assert route.calls[0].request.headers["Content-Type"] == "application/json"
    assert route.calls[0].request.headers["OCS-APIRequest"] == "true"


@pytest.mark.anyio
async def test_create_card_passes_description_and_duedate_through(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(CARDS_URL).mock(return_value=httpx.Response(200, json=CREATED_CARD))
        await deck_client.create_card(
            client,
            creds,
            2,
            11,
            title="Übergabe vorbereiten",
            description="Straßenplanung prüfen",
            duedate="2026-09-01T10:00:00+00:00",
        )

    body = json.loads(route.calls[0].request.content)
    assert body["description"] == "Straßenplanung prüfen"
    assert body["duedate"] == "2026-09-01T10:00:00+00:00"


@pytest.mark.anyio
async def test_a_title_over_255_characters_never_reaches_nextcloud(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    """Deck answers 400 here; a local check costs nothing and names the real limit."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__startswith=DECK_BASE)
        with pytest.raises(ToolError) as excinfo:
            await deck_client.create_card(client, creds, 2, 11, title="x" * 256)

    assert route.call_count == 0
    assert "255" in f"{excinfo.value.message} {excinfo.value.hint}"


@pytest.mark.anyio
async def test_an_empty_title_never_reaches_nextcloud(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__startswith=DECK_BASE)
        with pytest.raises(ToolError):
            await deck_client.create_card(client, creds, 2, 11, title="   ")

    assert route.call_count == 0


@pytest.mark.anyio
async def test_a_duedate_that_is_not_iso_8601_never_reaches_nextcloud(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__startswith=DECK_BASE)
        with pytest.raises(ToolError) as excinfo:
            await deck_client.create_card(client, creds, 2, 11, title="Karte", duedate="01.09.2026")

    assert route.call_count == 0
    assert "2026-09-01T10:00:00+00:00" in excinfo.value.hint


@pytest.mark.anyio
async def test_a_board_id_that_is_not_numeric_never_reaches_nextcloud(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    """Ids go into the path; anything but digits is a bug or an attempt (threat T-01-63)."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__startswith=DECK_BASE)
        with pytest.raises(ToolError):
            await deck_client.get_stacks(client, creds, "2/../../boards")

    assert route.call_count == 0


@pytest.mark.anyio
async def test_a_403_becomes_a_permission_hint(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    """Deck's own error format, not the OCS envelope (pitfall 9)."""
    with respx.mock(assert_all_called=True) as mock:
        mock.post(CARDS_URL).mock(
            return_value=httpx.Response(403, json={"status": 403, "message": "Permission denied"})
        )
        with pytest.raises(ToolError) as excinfo:
            await deck_client.create_card(client, creds, 2, 11, title="Neue Karte")

    assert "Permission denied" in excinfo.value.message
    assert "permission" in excinfo.value.hint.lower()


@pytest.mark.anyio
async def test_a_404_names_the_unknown_board_or_stack(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(STACKS_URL).mock(
            return_value=httpx.Response(404, json={"status": 404, "message": "Board not found"})
        )
        with pytest.raises(ToolError) as excinfo:
            await deck_client.get_stacks(client, creds, 2)

    assert "not find" in excinfo.value.message.lower()
    assert "board 2" in excinfo.value.message.lower()


@pytest.mark.anyio
async def test_a_login_page_is_explained_instead_of_crashing(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    """Without the mandatory header Nextcloud answers HTML with status 200."""
    with respx.mock(assert_all_called=True) as mock:
        mock.get(BOARDS_URL).mock(
            return_value=httpx.Response(
                200,
                html="<!DOCTYPE html><html><body>Login</body></html>",
            )
        )
        with pytest.raises(ToolError) as excinfo:
            await deck_client.get_boards(client, creds)

    assert "HTML" in excinfo.value.message
    assert excinfo.value.hint


@pytest.mark.anyio
async def test_a_rate_limited_instance_asks_for_a_pause(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(BOARDS_URL).mock(return_value=httpx.Response(429, text="slow down"))
        with pytest.raises(ToolError) as excinfo:
            await deck_client.get_boards(client, creds)

    assert "rate limiting" in excinfo.value.message.lower()


@pytest.mark.anyio
async def test_an_expired_app_password_is_reported_once_and_not_retried(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    """Nextcloud throttles per source IP, so a failed authentication is never repeated."""
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(BOARDS_URL).mock(
            return_value=httpx.Response(401, json={"status": 401, "message": "Unauthorised"})
        )
        with pytest.raises(ToolError) as excinfo:
            await deck_client.get_boards(client, creds)

    assert route.call_count == 1
    assert "app password" in excinfo.value.message.lower()


@pytest.mark.anyio
async def test_a_board_list_that_is_not_a_list_is_reported_as_such(
    client: httpx.AsyncClient, creds: Credentials
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(BOARDS_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
        with pytest.raises(ToolError) as excinfo:
            await deck_client.get_boards(client, creds)

    assert excinfo.value.hint


def test_the_module_has_no_delete_path_and_exactly_one_update() -> None:
    """The server promise: this client never removes anything (T-01-62), and its one PUT is
    the card update of gestion-aio#94."""
    source = Path(deck_client.__file__).read_text(encoding="utf-8")
    for forbidden in (".delete(", ".patch("):
        assert forbidden not in source, f"{forbidden} has no place in this client"
    assert source.count(".put(") == 1, "the card update is the only PUT of this client"


def test_the_module_reads_deck_errors_with_the_app_json_parser() -> None:
    """pitfall 9: an ocs.meta parser on a Deck answer produces KeyErrors, not messages."""
    source = Path(deck_client.__file__).read_text(encoding="utf-8")
    assert "parse_app_json" in source
    assert "parse_ocs(" not in source
