"""Unit tests for the two deck tools, all paths.

The two properties that make ``deck_browse`` worth being one tool instead of three are
pinned first: ``level="cards"`` costs exactly **one** Deck request, because the stacks
answer already carries the cards (no N+1), and every level answers with the same envelope
so the model does not have to learn three shapes.

Everything else is the honest-failure catalogue of D-15 and SRV-04: a missing board id
names the parameter, an unknown level is refused, a missing Deck app stops both tools
before the first Deck request, and a user without board rights is told so instead of being
walked into a 403.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mcp_connector.errors import REASON_PERMISSION_DENIED, AppMissingError, ToolError
from mcp_connector.nextcloud import NcClients, capabilities
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import deck as deck_tools

BASE = "http://nc.test"
USER = "alice"
SECRET = "app-password-test"

CAPABILITIES_URL = f"{BASE}/ocs/v2.php/cloud/capabilities"
DECK_BASE = f"{BASE}/index.php/apps/deck/api/v1.0"
BOARDS_URL = f"{DECK_BASE}/boards"
STACKS_URL = f"{DECK_BASE}/boards/2/stacks"
CARDS_URL = f"{DECK_BASE}/boards/2/stacks/11/cards"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

DECK_INSTALLED = {"version": "1.18.3", "canCreateBoards": True, "apiVersions": ["1.0", "1.1"]}
DECK_WITHOUT_BOARD_RIGHTS = {**DECK_INSTALLED, "canCreateBoards": False}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def envelope(data: Any) -> dict:
    return {"ocs": {"meta": {"status": "ok", "statuscode": 200, "message": "OK"}, "data": data}}


def capabilities_payload(*, deck: dict | None = None) -> dict:
    section: dict[str, Any] = {"core": {}}
    if deck is not None:
        section["deck"] = deck
    return envelope({"capabilities": section})


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    capabilities.clear_cache()


@pytest.fixture
def clients() -> NcClients:
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False),
        creds=Credentials(BASE, USER, SECRET),
    )


def mock_capabilities(mock: respx.MockRouter, *, deck: dict | None = DECK_INSTALLED) -> None:
    mock.get(CAPABILITIES_URL).mock(
        return_value=httpx.Response(200, json=capabilities_payload(deck=deck))
    )


@pytest.mark.anyio
async def test_browse_boards_returns_id_title_and_the_write_permission(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        boards = mock.get(BOARDS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_boards.json"))
        )
        result = await deck_tools.browse(clients)

    assert boards.call_count == 1
    assert result["level"] == "boards"
    assert result["count"] == 2
    assert result["results"][0] == {"id": 2, "title": "Projekt MCP", "can_edit": True}
    assert result["results"][1]["can_edit"] is False


@pytest.mark.anyio
async def test_browse_boards_without_a_single_board_says_so(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(BOARDS_URL).mock(return_value=httpx.Response(200, json=[]))
        result = await deck_tools.browse(clients, level="boards")

    assert result["count"] == 0
    assert result["results"] == []


@pytest.mark.anyio
async def test_browse_stacks_reports_card_counts_and_not_card_lists(
    clients: NcClients,
) -> None:
    """Stacks are the navigation level; the card payload would be pure token noise here."""
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        stacks = mock.get(STACKS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_stacks.json"))
        )
        result = await deck_tools.browse(clients, level="stacks", board_id="2")

    assert stacks.call_count == 1
    assert result["results"] == [
        {"id": 11, "title": "To Do", "cards": 2},
        {"id": 12, "title": "Erledigt", "cards": 1},
    ]


@pytest.mark.anyio
async def test_browse_cards_needs_exactly_one_http_request(clients: NcClients) -> None:
    """D-06 and the N+1 proof: the stacks answer already carries every card."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        stacks = mock.get(STACKS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_stacks.json"))
        )
        deck_calls = mock.route(url__startswith=DECK_BASE)

        result = await deck_tools.browse(clients, level="cards", board_id="2")

    assert stacks.call_count == 1
    assert deck_calls.call_count == 0, "no second request per stack or per card (no N+1)"
    assert result["count"] == 3
    assert [card["id"] for card in result["results"]] == [
        "card:2:11:101",
        "card:2:11:102",
        "card:2:12:103",
    ]
    assert result["results"][0]["stack"] == "To Do"
    assert result["results"][0]["url"] == f"{BASE}/index.php/apps/deck/card/101"
    assert "duedate" not in result["results"][0]
    assert result["results"][1]["duedate"] == "2026-09-01T10:00:00+00:00"


@pytest.mark.anyio
async def test_browse_cards_can_be_narrowed_to_one_stack(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(STACKS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_stacks.json"))
        )
        result = await deck_tools.browse(clients, level="cards", board_id="2", stack_id="12")

    assert [card["id"] for card in result["results"]] == ["card:2:12:103"]


@pytest.mark.anyio
async def test_browse_cards_of_an_unknown_stack_says_which_stacks_exist(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(STACKS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_stacks.json"))
        )
        with pytest.raises(ToolError) as excinfo:
            await deck_tools.browse(clients, level="cards", board_id="2", stack_id="99")

    assert "99" in excinfo.value.message
    assert "11" in excinfo.value.hint


@pytest.mark.anyio
async def test_browse_caps_the_result_and_says_that_it_did(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(STACKS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_stacks.json"))
        )
        result = await deck_tools.browse(clients, level="cards", board_id="2", limit=2)

    assert result["count"] == 2
    assert result["truncated"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("level", ["stacks", "cards"])
async def test_a_level_below_boards_without_a_board_id_names_the_parameter(
    clients: NcClients, level: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        deck_calls = mock.route(url__startswith=DECK_BASE)

        with pytest.raises(ToolError) as excinfo:
            await deck_tools.browse(clients, level=level)

    assert deck_calls.call_count == 0
    assert "board_id" in excinfo.value.message
    assert "level=boards" in excinfo.value.hint


@pytest.mark.anyio
async def test_an_unknown_level_is_refused_before_any_request(clients: NcClients) -> None:
    """The schema rejects it first; the function stays honest when called directly."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        deck_calls = mock.route(url__startswith=DECK_BASE)

        with pytest.raises(ToolError) as excinfo:
            await deck_tools.browse(clients, level="labels")

    assert deck_calls.call_count == 0
    assert "boards" in excinfo.value.hint


@pytest.mark.anyio
async def test_create_card_returns_the_canonical_long_id(clients: NcClients) -> None:
    created = {"id": 104, "title": "Übergabe vorbereiten", "stackId": 11, "duedate": None}
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        post = mock.post(CARDS_URL).mock(return_value=httpx.Response(200, json=created))

        result = await deck_tools.create_card(
            clients, board_id="2", stack_id="11", title="Übergabe vorbereiten"
        )

    assert result["id"] == "card:2:11:104"
    assert result["title"] == "Übergabe vorbereiten"
    assert result["url"] == f"{BASE}/index.php/apps/deck/card/104"
    body = json.loads(post.calls[0].request.content)
    assert body["type"] == "plain"
    assert body["title"] == "Übergabe vorbereiten"


@pytest.mark.anyio
async def test_create_card_reports_the_due_date_the_server_stored(clients: NcClients) -> None:
    created = {
        "id": 105,
        "title": "Fristsache",
        "stackId": 11,
        "duedate": "2026-09-01T10:00:00+00:00",
    }
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.post(CARDS_URL).mock(return_value=httpx.Response(200, json=created))

        result = await deck_tools.create_card(
            clients,
            board_id="2",
            stack_id="11",
            title="Fristsache",
            description="Straßenplanung",
            duedate="2026-09-01T10:00:00+00:00",
        )

    assert result["duedate"] == "2026-09-01T10:00:00+00:00"


@pytest.mark.anyio
async def test_a_user_without_board_rights_is_told_before_the_post(clients: NcClients) -> None:
    """SRV-04: canCreateBoards false plus a read-only board means no card, and it says why."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock, deck=DECK_WITHOUT_BOARD_RIGHTS)
        mock.get(BOARDS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_boards.json"))
        )
        post = mock.post(url__startswith=DECK_BASE)

        with pytest.raises(ToolError) as excinfo:
            await deck_tools.create_card(clients, board_id="5", stack_id="20", title="Neue Karte")

    assert post.call_count == 0, "a request that can only end in 403 must never leave"
    assert "board 5" in excinfo.value.message.lower()
    assert excinfo.value.hint


@pytest.mark.anyio
async def test_a_restricted_user_may_still_write_to_a_board_that_grants_it(
    clients: NcClients,
) -> None:
    """canCreateBoards only governs new boards; an edit permission on a board still counts."""
    created = {"id": 106, "title": "Neue Karte", "stackId": 11}
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock, deck=DECK_WITHOUT_BOARD_RIGHTS)
        mock.get(BOARDS_URL).mock(
            return_value=httpx.Response(200, json=fixture("deck_boards.json"))
        )
        mock.post(CARDS_URL).mock(return_value=httpx.Response(200, json=created))

        result = await deck_tools.create_card(
            clients, board_id="2", stack_id="11", title="Neue Karte"
        )

    assert result["id"] == "card:2:11:106"


@pytest.mark.anyio
async def test_a_server_side_403_still_becomes_a_permission_hint(clients: NcClients) -> None:
    """Deck decides, not us: the permission check is a courtesy, never the guarantee."""
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.post(CARDS_URL).mock(
            return_value=httpx.Response(403, json={"status": 403, "message": "Permission denied"})
        )
        with pytest.raises(ToolError) as excinfo:
            await deck_tools.create_card(clients, board_id="2", stack_id="11", title="Neue Karte")

    assert "Permission denied" in excinfo.value.message


@pytest.mark.anyio
async def test_create_card_without_a_title_never_reaches_nextcloud(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        deck_calls = mock.route(url__startswith=DECK_BASE)

        with pytest.raises(ToolError):
            await deck_tools.create_card(clients, board_id="2", stack_id="11", title="  ")

    assert deck_calls.call_count == 0


@pytest.mark.anyio
async def test_create_card_with_an_overlong_title_never_reaches_nextcloud(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        deck_calls = mock.route(url__startswith=DECK_BASE)

        with pytest.raises(ToolError) as excinfo:
            await deck_tools.create_card(clients, board_id="2", stack_id="11", title="x" * 300)

    assert deck_calls.call_count == 0
    assert "255" in excinfo.value.message


@pytest.mark.anyio
async def test_create_card_reports_a_missing_id_instead_of_inventing_one(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.post(CARDS_URL).mock(return_value=httpx.Response(200, json={"title": "Neue Karte"}))

        with pytest.raises(ToolError) as excinfo:
            await deck_tools.create_card(clients, board_id="2", stack_id="11", title="Neue Karte")

    assert "id" in excinfo.value.message
    assert excinfo.value.hint


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["browse_boards", "browse_cards", "create_card"])
async def test_a_missing_deck_app_stops_both_tools_before_the_first_request(
    clients: NcClients, tool: str
) -> None:
    """SRV-04 and D-15: one sentence with an alternative, and zero Deck requests."""
    calls = {
        "browse_boards": lambda: deck_tools.browse(clients),
        "browse_cards": lambda: deck_tools.browse(clients, level="cards", board_id="2"),
        "create_card": lambda: deck_tools.create_card(
            clients, board_id="2", stack_id="11", title="Neue Karte"
        ),
    }
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock, deck=None)
        deck_calls = mock.route(url__startswith=DECK_BASE)

        with pytest.raises(AppMissingError) as excinfo:
            await calls[tool]()

    assert deck_calls.call_count == 0, "no request may go to an app that is not installed"
    assert excinfo.value.message == "The Deck app is not installed on this Nextcloud."
    assert "Deck app" in excinfo.value.hint


@pytest.mark.anyio
async def test_a_server_error_while_browsing_is_a_degraded_answer(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(BOARDS_URL).mock(return_value=httpx.Response(502, text="bad gateway"))

        with pytest.raises(ToolError) as excinfo:
            await deck_tools.browse(clients)

    assert "502" in excinfo.value.message
    assert excinfo.value.hint


# --- deck_update_card (APS-Conecta/gestion-aio#94) ------------------------------------------

BOARD_URL = f"{DECK_BASE}/boards/2"
CARD_URL = f"{CARDS_URL}/101"
CARD_101 = {
    "id": 101,
    "title": "Deck-Client bauen",
    "description": "REST v1.0 mit Pflichtheadern",
    "stackId": 11,
    "type": "plain",
    "owner": {"primaryKey": "alice", "uid": "alice", "displayname": "Alice", "type": 0},
    "order": 3,
    "archived": False,
    "duedate": "2026-09-01T10:00:00+00:00",
}


def board_with(*, edit: bool) -> dict:
    board = fixture("deck_boards.json")[0]
    return {**board, "permissions": {**board["permissions"], "PERMISSION_EDIT": edit}}


@pytest.mark.anyio
async def test_update_card_refuses_a_card_the_user_cannot_edit_and_writes_nothing(
    clients: NcClients,
) -> None:
    """A board shared read-only is refused before any write leaves this server."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        mock.get(BOARD_URL).mock(return_value=httpx.Response(200, json=board_with(edit=False)))
        mock.get(CARD_URL).mock(return_value=httpx.Response(200, json=CARD_101))
        write = mock.put(CARD_URL)
        with pytest.raises(ToolError) as excinfo:
            await deck_tools.update_card(clients, card_id="card:2:11:101", title="Neu")

    assert write.call_count == 0
    assert excinfo.value.reason == REASON_PERMISSION_DENIED
    assert excinfo.value.message == "No permission to change cards on board 2 (Projekt MCP)."


@pytest.mark.anyio
async def test_update_card_writes_back_every_field_it_read_with_only_the_named_one_changed(
    clients: NcClients,
) -> None:
    """Deck's PUT replaces the card, so a left out field would be cleared."""
    stored = {**CARD_101, "title": "Neu"}
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(BOARD_URL).mock(return_value=httpx.Response(200, json=board_with(edit=True)))
        mock.get(CARD_URL).mock(return_value=httpx.Response(200, json=CARD_101))
        write = mock.put(CARD_URL).mock(return_value=httpx.Response(200, json=stored))
        result = await deck_tools.update_card(clients, card_id="card:2:11:101", title=" Neu ")

    assert json.loads(write.calls[0].request.content) == {
        "title": "Neu",
        "type": "plain",
        "owner": "alice",
        "description": "REST v1.0 mit Pflichtheadern",
        "order": 3,
        "duedate": "2026-09-01T10:00:00+00:00",
        "startdate": None,
        "archived": False,
    }
    assert result == {
        "id": "card:2:11:101",
        "title": "Neu",
        "url": f"{BASE}/index.php/apps/deck/card/101",
        "duedate": "2026-09-01T10:00:00+00:00",
    }


@pytest.mark.anyio
async def test_update_card_reports_a_write_deck_itself_refuses(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(BOARD_URL).mock(return_value=httpx.Response(200, json=board_with(edit=True)))
        mock.get(CARD_URL).mock(return_value=httpx.Response(200, json=CARD_101))
        mock.put(CARD_URL).mock(
            return_value=httpx.Response(403, json={"status": 403, "message": "Permission denied"})
        )
        with pytest.raises(ToolError) as excinfo:
            await deck_tools.update_card(clients, card_id="card:2:11:101", description="x")

    assert excinfo.value.reason == REASON_PERMISSION_DENIED


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("card_id", "fields"),
    [("card:101", {"title": "Neu"}), ("note:101", {"title": "Neu"}), ("card:2:11:101", {})],
)
async def test_update_card_refuses_a_short_id_or_nothing_to_change_before_any_request(
    clients: NcClients, card_id: str, fields: dict[str, str]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        deck_api = mock.route(url__startswith=DECK_BASE)
        with pytest.raises(ToolError):
            await deck_tools.update_card(clients, card_id=card_id, **fields)

    assert deck_api.call_count == 0
