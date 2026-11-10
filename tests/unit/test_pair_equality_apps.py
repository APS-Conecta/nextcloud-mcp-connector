"""GATE-03 on the unit level, family ``apps``: a tagged target answers like an absent one.

For every pair case of the family ``apps`` in ``tool_classes.PAIR_CASES`` the tool is called
twice through the real registry (``Client(mcp)``): once with a target tagged ``kein-ki`` and
once with a target that does not exist. What is compared is the whole ``CallToolResult``
(text, ``structuredContent``, ``isError``) as ``result.model_dump(mode="json", by_alias=True)``
after ``result_shapes.normalised`` replaced the requested id by ``<ID>``. The requested id is
defined in ``result_shapes.requested_forms``: the argument, its path prefixes and for fetch
the bare id ("angefragte Id = das Argument, seine Pfad-Präfixe und für fetch die nackte Id").

The response time is not part of the comparison (D-28-12); an accepted limit, documented in
phase 29. The guard requests are mocked for real through ``guard_routes`` in five states:
active, 412 once (listed anew, then active), 412 twice, timeout and 5xx of the REPORT.

``notes_create`` with a category is the one named exception of this family (D-28-16): the
Notes app creates a category that does not exist, the guard refuses a tagged one. The test
``test_notes_create_is_a_named_residual_oracle`` pins exactly that difference; in an outage
the two answers are an ordinary pair again.

Test modules do not import each other, so the five guard states are defined here as well
and not imported from ``test_pair_equality_files``.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import guard_routes
import httpx
import pytest
import respx
from guard_routes import BASE, USER
from mcp import Client
from mcp.types import CallToolResult
from result_shapes import normalised
from tool_classes import NAMED_EXCEPTIONS, PAIR_CASES

from mcp_connector.nextcloud import capabilities
from mcp_connector.nextcloud.clients import dav
from mcp_connector.server import mcp
from mcp_connector.tools import withhold

SECRET = "app-password-test"

GUARD_MODES = ("active", "stale_once", "stale_twice", "timeout", "server_error")
#: The guard states in which the check is answered: the tag decides.
ANSWERED_MODES = ("active", "stale_once")
#: The guard states in which the check cannot be answered.
OUTAGE_MODES = ("stale_twice", "timeout", "server_error")

#: Every pair case this module covers, compared or named (D-28-16).
COVERED = {
    ("notes_read", "note"),
    ("fetch", "note"),
    ("notes_create", "category"),
    ("notes_update", "note"),
    ("talk_browse", "messages"),
    ("talk_send", "token"),
    ("fetch", "message"),
    ("fetch", "table"),
}

Node = tuple[str, str, bool]
World = Callable[[respx.MockRouter], Any]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    guard_routes.reset()
    capabilities.clear_cache()
    monkeypatch.setenv("NC_MCP_URL", BASE)
    monkeypatch.setenv("NC_MCP_USER", USER)
    monkeypatch.setenv("NC_MCP_APP_PASSWORD", SECRET)
    monkeypatch.delenv("NC_MCP_STATIC_BEARER", raising=False)
    monkeypatch.delenv("NC_MCP_FILES_ROOT", raising=False)
    monkeypatch.delenv("NC_MCP_TALK_SEND", raising=False)


def envelope(data: Any) -> dict[str, Any]:
    return {"ocs": {"meta": {"status": "ok", "statuscode": 200, "message": "OK"}, "data": data}}


def arm_guard(mock: respx.MockRouter, mode: str, *nodes: Node) -> None:
    """Mock the guard requests of one of the five states, with ``nodes`` tagged."""
    if mode == "active":
        guard_routes.active(mock, *nodes)
    elif mode == "stale_once":
        mock.route(method="PROPFIND", url=guard_routes.TAGS).mock(
            return_value=guard_routes.listed(
                guard_routes.tag_list(guard_routes.KEIN_KI, guard_routes.OTHER_TAG)
            )
        )
        seen: list[httpx.Request] = []

        def report(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(412)
            return guard_routes.listed(guard_routes.report_207(*nodes))

        mock.route(method="REPORT", url=guard_routes.HOME).mock(side_effect=report)
    elif mode == "stale_twice":
        guard_routes.stale(mock)
    elif mode == "timeout":
        guard_routes.timeout(mock)
    elif mode == "server_error":
        guard_routes.unverifiable(mock)
    else:
        raise AssertionError(f"unknown guard mode {mode}")


@dataclass
class Side:
    """One call of a pair: the answer, its normalised form and what the world returned."""

    result: CallToolResult
    text: str
    routes: Any


async def side(
    tool: str,
    args: dict[str, Any],
    requested: tuple[str, ...],
    mode: str,
    nodes: tuple[Node, ...],
    world: World,
) -> Side:
    """Call ``tool`` once through the registry against a fresh mocked instance."""
    guard_routes.reset()
    capabilities.clear_cache()
    with respx.mock(assert_all_called=False) as mock:
        arm_guard(mock, mode, *nodes)
        routes = world(mock)
        async with Client(mcp) as client:
            result = await client.call_tool(tool, args)
    return Side(result=result, text=normalised(result, *requested), routes=routes)


def raw(result: CallToolResult) -> str:
    return json.dumps(result.model_dump(mode="json", by_alias=True), ensure_ascii=False)


def assert_alike(tagged: Side, unknown: Side) -> None:
    assert tagged.text == unknown.text, f"\ntagged:  {tagged.text}\nunknown: {unknown.text}"


# --- notes ---------------------------------------------------------------------------------

CAPABILITIES_URL = f"{BASE}/ocs/v2.php/cloud/capabilities"
NOTES_BASE = f"{BASE}/index.php/apps/notes/api/v1/notes"
SETTINGS_URL = f"{BASE}/index.php/apps/notes/api/v1/settings"

TAGGED_NOTE = "933"
INVENTED_NOTE = "999"
NOTE_NODE: Node = ("Notes/Offen/geheim.md", TAGGED_NOTE, False)
NOTE_CONTENT = "geheimer Inhalt\n"


def mock_notes_app(mock: respx.MockRouter) -> None:
    payload = envelope(
        {"capabilities": {"core": {}, "notes": {"api_version": ["1.3"], "version": "6.1.0"}}}
    )
    mock.get(CAPABILITIES_URL).mock(return_value=httpx.Response(200, json=payload))


def note_world(status: int | None = None) -> World:
    """One instance with note 933 (tagged) and without note 999.

    ``status`` replaces both Notes GET answers by a bare status, for the D-28-17 case.
    """

    def world(mock: respx.MockRouter) -> None:
        mock_notes_app(mock)
        if status is not None:
            mock.get(f"{NOTES_BASE}/{TAGGED_NOTE}").mock(return_value=httpx.Response(status))
            mock.get(f"{NOTES_BASE}/{INVENTED_NOTE}").mock(return_value=httpx.Response(status))
            return
        mock.get(f"{NOTES_BASE}/{TAGGED_NOTE}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": int(TAGGED_NOTE),
                    "title": "Geheime Notiz",
                    "content": NOTE_CONTENT,
                    "category": "Offen",
                    "favorite": False,
                    "modified": 1789584914,
                },
            )
        )
        mock.get(f"{NOTES_BASE}/{INVENTED_NOTE}").mock(
            return_value=httpx.Response(404, json={"status": 404, "message": "Note not found"})
        )

    return world


NOTE_READERS = {
    "notes_read": lambda note_id: {"note_id": note_id},
    "fetch": lambda note_id: {"id": note_id},
    # The update goes through the guard of notes_read before it could write (#94).
    "notes_update": lambda note_id: {"note_id": note_id, "content": "neu", "etag": "e"},
}


async def note_pair(tool: str, mode: str, world: World) -> tuple[Side, Side]:
    args = NOTE_READERS[tool]
    tagged_id = f"note:{TAGGED_NOTE}"
    invented_id = f"note:{INVENTED_NOTE}"
    tagged = await side(tool, args(tagged_id), (tagged_id,), mode, (NOTE_NODE,), world)
    unknown = await side(tool, args(invented_id), (invented_id,), mode, (NOTE_NODE,), world)
    return tagged, unknown


@pytest.mark.anyio
@pytest.mark.parametrize("mode", GUARD_MODES)
async def test_notes_read_answers_a_tagged_note_like_an_invented_id(mode: str) -> None:
    tagged, unknown = await note_pair("notes_read", mode, note_world())

    assert_note_pair(tagged, unknown, mode)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", GUARD_MODES)
async def test_notes_update_answers_a_tagged_note_like_an_invented_id(mode: str) -> None:
    tagged, unknown = await note_pair("notes_update", mode, note_world())

    assert_note_pair(tagged, unknown, mode)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", GUARD_MODES)
async def test_fetch_note_answers_a_tagged_note_like_an_invented_id(mode: str) -> None:
    tagged, unknown = await note_pair("fetch", mode, note_world())

    assert_note_pair(tagged, unknown, mode)


def assert_note_pair(tagged: Side, unknown: Side, mode: str) -> None:
    assert tagged.result.is_error is True
    assert NOTE_CONTENT.strip() not in raw(tagged.result)
    if mode in OUTAGE_MODES:
        assert withhold.EXCLUSION_UNAVAILABLE in raw(unknown.result)
    else:
        assert "did not find the note <ID>." in unknown.text
    assert_alike(tagged, unknown)


@pytest.mark.anyio
@pytest.mark.parametrize("tool", sorted(NOTE_READERS))
@pytest.mark.parametrize("status", [500, 503])
async def test_note_reads_answer_a_failing_notes_app_alike(tool: str, status: int) -> None:
    """D-28-17: a failing Notes GET answers before the tag, for a tagged and an unknown id."""
    tagged, unknown = await note_pair(tool, "active", note_world(status))

    assert tagged.result.is_error is True
    assert_alike(tagged, unknown)


# --- talk ----------------------------------------------------------------------------------

ROOM_URL = f"{BASE}/ocs/v2.php/apps/spreed/api/v4/room"
CHAT_BASE = f"{BASE}/ocs/v2.php/apps/spreed/api/v1/chat"
SPREED = {"features": ["chat-v2"], "config": {"chat": {"max-length": 32000}}}
TOKEN = "abcd1234"
FILE_TOKEN = "fileroom1"
INVENTED_TOKEN = "nosuch99"
MESSAGE_ID = 1
SECRET_FILE = "geheim.txt"
FILE_NODE: Node = ("Docs/geheim.txt", "901", False)


def room(token: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "token": token,
        "type": 2,
        "displayName": "Baustelle Süd",
        "permissions": 254,
        "readOnly": 0,
        "objectType": "",
        "objectId": "",
        "isArchived": False,
        "unreadMessages": 0,
        "lastActivity": 1755180000,
    }
    return {**base, **overrides}


def chat(token: str) -> dict[str, Any]:
    return {
        "id": MESSAGE_ID,
        "token": token,
        "actorType": "users",
        "actorId": "bob",
        "actorDisplayName": "Bob Beispiel",
        "timestamp": 1755180001,
        "message": "Zur Datei",
        "messageParameters": {},
        "messageType": "comment",
    }


def talk_world(mock: respx.MockRouter) -> respx.Route:
    """One instance with a file conversation of the tagged file 901 and an ordinary one.

    The history and the context of the file conversation are there, so only the guard can
    keep them back. Returns the POST route of every chat, which no pair may ever reach.
    """
    rooms = [
        room(
            FILE_TOKEN,
            type=3,
            displayName=SECRET_FILE,
            objectType="file",
            objectId="901",
            lastActivity=1755190000,
        ),
        room(TOKEN),
    ]
    mock.get(CAPABILITIES_URL).mock(
        return_value=httpx.Response(200, json=envelope({"capabilities": {"spreed": SPREED}}))
    )
    mock.get(ROOM_URL).mock(return_value=httpx.Response(200, json=envelope(rooms)))
    mock.get(f"{CHAT_BASE}/{FILE_TOKEN}").mock(
        return_value=httpx.Response(200, json=envelope([chat(FILE_TOKEN)]))
    )
    mock.get(f"{CHAT_BASE}/{FILE_TOKEN}/{MESSAGE_ID}/context").mock(
        return_value=httpx.Response(200, json=envelope([chat(FILE_TOKEN)]))
    )
    return mock.route(method="POST", url__startswith=CHAT_BASE).mock(
        return_value=httpx.Response(201, json=envelope(chat(FILE_TOKEN)))
    )


TALK_CALLS: dict[str, tuple[str, Callable[[str], dict[str, Any]], Callable[[str], str]]] = {
    "talk_browse": (
        "talk_browse",
        lambda token: {"level": "messages", "token": token},
        lambda token: token,
    ),
    "talk_send": (
        "talk_send",
        lambda token: {"token": token, "message": "kanarie"},
        lambda token: token,
    ),
    "fetch_message": (
        "fetch",
        lambda token: {"id": f"message:{token}:{MESSAGE_ID}"},
        lambda token: f"message:{token}:{MESSAGE_ID}",
    ),
}


async def talk_pair(case: str, mode: str) -> tuple[Side, Side]:
    tool, args, requested = TALK_CALLS[case]
    tagged = await side(
        tool, args(FILE_TOKEN), (requested(FILE_TOKEN),), mode, (FILE_NODE,), talk_world
    )
    unknown = await side(
        tool, args(INVENTED_TOKEN), (requested(INVENTED_TOKEN),), mode, (FILE_NODE,), talk_world
    )
    return tagged, unknown


def assert_talk_pair(tagged: Side, unknown: Side, mode: str) -> None:
    assert tagged.result.is_error is True
    assert SECRET_FILE not in raw(tagged.result)
    assert "Zur Datei" not in raw(tagged.result)
    if mode in OUTAGE_MODES:
        # D-28-15: the outage answers a file conversation and an invented token alike.
        assert withhold.EXCLUSION_UNAVAILABLE in raw(tagged.result)
        assert withhold.EXCLUSION_UNAVAILABLE in raw(unknown.result)
    else:
        assert "is not in the conversation list" in raw(unknown.result)
    assert_alike(tagged, unknown)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", GUARD_MODES)
async def test_talk_browse_answers_a_tagged_file_conversation_like_an_invented_token(
    mode: str,
) -> None:
    tagged, unknown = await talk_pair("talk_browse", mode)

    assert_talk_pair(tagged, unknown, mode)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", GUARD_MODES)
async def test_talk_send_answers_a_tagged_file_conversation_like_an_invented_token(
    mode: str,
) -> None:
    tagged, unknown = await talk_pair("talk_send", mode)

    assert tagged.routes.call_count == 0, "nothing may be posted into a tagged conversation"
    assert unknown.routes.call_count == 0
    assert_talk_pair(tagged, unknown, mode)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", GUARD_MODES)
async def test_fetch_message_answers_a_tagged_file_conversation_like_an_invented_token(
    mode: str,
) -> None:
    tagged, unknown = await talk_pair("fetch_message", mode)

    assert_talk_pair(tagged, unknown, mode)


# --- tables --------------------------------------------------------------------------------

V2_BASE = f"{BASE}/ocs/v2.php/apps/tables/api/2"
V1_BASE = f"{BASE}/index.php/apps/tables/api/1"
TABLE_ID = "table:7"
TABLES_INSTALLED = {"enabled": True, "version": "2.2.2", "apiVersions": ["1.0", "2.0"]}
TABLE = {"id": 7, "title": "Verweise", "rowsCount": 1, "columnsCount": 2, "isShared": False}
HEADER = ["Aufgabe", "Verweis"]
LINKED_SECRET = ("geheim-mk2122.txt", "901")
LINKED_OPEN = ("offen.txt", "902")
LINK_NODE: Node = ("Docs/geheim-mk2122.txt", LINKED_SECRET[1], False)


def link(name: str, fileid: str) -> str:
    """A Tables link cell as the app stores it: a JSON string (D-28-18, measured in 28-01)."""
    return json.dumps({"title": name, "value": f"{BASE}/f/{fileid}", "providerId": "files"})


def table_world(cell: str | None) -> World:
    """One table with one row whose link cell is ``cell`` (``None`` is an empty cell)."""

    def world(mock: respx.MockRouter) -> None:
        mock.get(CAPABILITIES_URL).mock(
            return_value=httpx.Response(
                200, json=envelope({"capabilities": {"core": {}, "tables": TABLES_INSTALLED}})
            )
        )
        mock.get(f"{V2_BASE}/tables/7").mock(return_value=httpx.Response(200, json=envelope(TABLE)))
        # PHP's json_encode escapes every slash, so the wire carries an escaped one (28-01).
        wire = json.dumps([HEADER, ["Baulos 3", cell]]).replace("/", "\\/")
        mock.get(f"{V1_BASE}/tables/7/rows/simple").mock(
            return_value=httpx.Response(
                200, content=wire.encode(), headers={"Content-Type": "application/json"}
            )
        )

    return world


async def table_pair(
    mode: str, other: str | None, other_forms: tuple[str, ...]
) -> tuple[Side, Side]:
    """fetch(table:7) with a link to the tagged file against the same table with ``other``.

    The table id is requested on both sides; the name and the file id of the link target are
    added as forms, so only an echo of them would be replaced and the rest must be equal.
    """
    tagged = await side(
        "fetch",
        {"id": TABLE_ID},
        (TABLE_ID, *LINKED_SECRET),
        mode,
        (LINK_NODE,),
        table_world(link(*LINKED_SECRET)),
    )
    unknown = await side(
        "fetch", {"id": TABLE_ID}, (TABLE_ID, *other_forms), mode, (LINK_NODE,), table_world(other)
    )
    return tagged, unknown


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ANSWERED_MODES)
async def test_fetch_table_answers_a_tagged_link_like_an_empty_cell(mode: str) -> None:
    """D-28-14: with the check answered, a tagged link cell reads like an empty one.

    The empty side asks no guard at all (no link, no question); the guard is armed anyway,
    so both sides run against the same mocked instance.
    """
    tagged, empty = await table_pair(mode, None, ())

    assert tagged.result.is_error is not True
    assert LINKED_SECRET[0] not in raw(tagged.result)
    assert withhold.EXCLUSION_UNAVAILABLE not in raw(tagged.result)
    assert_alike(tagged, empty)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", OUTAGE_MODES)
async def test_fetch_table_answers_a_tagged_link_like_an_untagged_one_in_an_outage(
    mode: str,
) -> None:
    """D-28-14/D-28-18: in an outage every file link is withheld and metadata says degraded."""
    tagged, untagged = await table_pair(mode, link(*LINKED_OPEN), LINKED_OPEN)

    assert tagged.result.is_error is not True
    assert LINKED_SECRET[0] not in raw(tagged.result)
    assert LINKED_OPEN[0] not in raw(untagged.result)
    for answer in (tagged, untagged):
        structured = answer.result.structured_content or {}
        assert structured["metadata"]["degraded"] == withhold.EXCLUSION_UNAVAILABLE
    assert_alike(tagged, untagged)


# --- notes_create: the named exception (D-28-16) -------------------------------------------

TAGGED_CATEGORY = "Geheim"
INVENTED_CATEGORY = "Erfunden"
CATEGORY_NODE: Node = ("Notes/Geheim", "932", True)


def create_world(mock: respx.MockRouter) -> respx.Route:
    """The Notes app with its settings; returns the POST route of a new note."""
    mock_notes_app(mock)
    mock.get(SETTINGS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "notesPath": "Notes",
                "fileSuffix": ".md",
                "noteMode": "rich",
                "showHidden": False,
                "loadRecentOnStartUp": True,
            },
        )
    )
    created = {
        "id": 960,
        "title": "Plan",
        "content": "x\n",
        "category": INVENTED_CATEGORY,
        "favorite": False,
        "modified": 1789584914,
    }
    return mock.post(NOTES_BASE).mock(return_value=httpx.Response(200, json=created))


def create_args(category: str) -> dict[str, Any]:
    return {"title": "Plan", "content": "x\n", "category": category}


async def create_pair(mode: str) -> tuple[Side, Side]:
    tagged = await side(
        "notes_create",
        create_args(TAGGED_CATEGORY),
        (TAGGED_CATEGORY,),
        mode,
        (CATEGORY_NODE,),
        create_world,
    )
    invented = await side(
        "notes_create",
        create_args(INVENTED_CATEGORY),
        (INVENTED_CATEGORY,),
        mode,
        (CATEGORY_NODE,),
        create_world,
    )
    return tagged, invented


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ANSWERED_MODES)
async def test_notes_create_is_a_named_residual_oracle(mode: str) -> None:
    """Accepted residual oracle, documented in phase 29 like T-27-16 (D-28-16).

    The Notes app creates a category that does not exist, and the guard refuses a tagged
    one with the sentence of an upload into a missing folder. The two answers differ, and
    this test pins exactly that difference instead of pretending they were a pair.
    """
    assert ("notes_create", "category") in NAMED_EXCEPTIONS

    tagged, invented = await create_pair(mode)

    candidate = f"/Notes/{TAGGED_CATEGORY}/Plan.md"
    assert tagged.result.is_error is True
    assert str(dav.parent_missing(candidate)) in raw(tagged.result)
    assert tagged.routes.call_count == 0, "a tagged category must never be written into"

    assert invented.result.is_error is not True
    assert invented.routes.call_count == 1
    assert "note:960" in raw(invented.result)

    assert tagged.text != invented.text


@pytest.mark.anyio
@pytest.mark.parametrize("mode", OUTAGE_MODES)
async def test_notes_create_refuses_a_tagged_and_an_invented_category_alike_in_an_outage(
    mode: str,
) -> None:
    """Outside the named exception notes_create is an ordinary pair: one refusal, no POST."""
    tagged, invented = await create_pair(mode)

    assert tagged.result.is_error is True
    assert withhold.EXCLUSION_UNAVAILABLE in raw(tagged.result)
    assert tagged.routes.call_count == 0
    assert invented.routes.call_count == 0
    assert_alike(tagged, invented)


# --- coverage ------------------------------------------------------------------------------


def test_the_apps_family_covers_exactly_its_pair_cases() -> None:
    """No pair case of the family is skipped silently; the one named case is D-28-16."""
    apps = {case for case, family in PAIR_CASES.items() if family == "apps"}
    assert apps == COVERED
    named = {case for case in NAMED_EXCEPTIONS if PAIR_CASES.get(case) == "apps"}
    assert named == {("notes_create", "category")}
