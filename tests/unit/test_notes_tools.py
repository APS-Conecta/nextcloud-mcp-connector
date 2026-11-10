"""Unit tests for the three notes tools, all paths.

Happy path, empty result, unknown note, storage full, missing app, an unusable
``resourceUrl``, an empty search term and a server error are all covered here, plus the two
properties that make the tools honest: the search never asks the Notes REST API for a
second round trip per hit, and the title of a created note is the one the server reports
back, not the one the model asked for.
"""

import json
from pathlib import Path
from typing import Any

import guard_routes
import httpx
import pytest
import respx

from mcp_connector.errors import REASON_PERMISSION_DENIED, AppMissingError, ToolError
from mcp_connector.nextcloud import NcClients, capabilities
from mcp_connector.nextcloud.clients import notes as notes_client
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import notes as notes_tools

BASE = "http://nc.test"
USER = "alice"
SECRET = "app-password-test"

CAPABILITIES_URL = f"{BASE}/ocs/v2.php/cloud/capabilities"
SEARCH_URL = f"{BASE}/ocs/v2.php/search/providers/notes/search"
NOTES_BASE = f"{BASE}/index.php/apps/notes/api/v1/notes"
SETTINGS_URL = f"{BASE}/index.php/apps/notes/api/v1/settings"
SETTINGS = {"notesPath": "Notes", "fileSuffix": ".md"}

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(autouse=True)
def _no_kein_ki_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing tests assert the behaviour without any kein-ki tag; the guard states are
    tested in the *_exclusion test modules."""
    guard_routes.patch_untagged(monkeypatch)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def envelope(data: Any) -> dict:
    return {"ocs": {"meta": {"status": "ok", "statuscode": 200, "message": "OK"}, "data": data}}


def capabilities_payload(*, notes: dict | None = None) -> dict:
    section: dict[str, Any] = {"core": {}}
    if notes is not None:
        section["notes"] = notes
    return envelope({"capabilities": section})


NOTES_INSTALLED = {"api_version": ["0.2", "1.3"], "version": "6.0.1"}

NOTE_12 = {
    "id": 12,
    "etag": "abc123",
    "readonly": False,
    "title": "Protokoll 2026-08-14",
    "content": "# Protokoll\nAnwesend: Anja, Khaled\n",
    "category": "Meetings",
    "favorite": False,
    "modified": 1755180000,
}


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    capabilities.clear_cache()


@pytest.fixture
def clients() -> NcClients:
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False),
        creds=Credentials(BASE, USER, SECRET),
    )


def mock_capabilities(mock: respx.MockRouter, *, notes: dict | None = NOTES_INSTALLED) -> None:
    mock.get(CAPABILITIES_URL).mock(
        return_value=httpx.Response(200, json=capabilities_payload(notes=notes))
    )


def mock_settings(mock: respx.MockRouter) -> respx.Route:
    """notes_create reads notesPath and fileSuffix before it writes (27-04)."""
    return mock.get(SETTINGS_URL).mock(return_value=httpx.Response(200, json=SETTINGS))


@pytest.mark.anyio
async def test_search_returns_id_title_excerpt_and_url(clients: NcClients) -> None:
    """One request to the unified search provider, no second round trip per hit."""
    # assert_all_called stays off: the notes route below is a guard that must never fire.
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        search = mock.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=fixture("ocs_search_notes.json"))
        )
        notes_api = mock.route(url__startswith=NOTES_BASE)

        result = await notes_tools.search(clients, query="protokoll")

    assert search.call_count == 1
    assert notes_api.call_count == 0, "the search must not read every hit a second time"
    assert result["count"] == 2
    assert result["results"][0] == {
        "id": "note:12",
        "title": "Protokoll 2026-08-14",
        "excerpt": "Anwesend: Anja, Khaled. Thema: Übergabe der Straßenplanung",
        "url": f"{BASE}/index.php/apps/notes/note/12",
    }
    assert result["results"][1]["id"] == "note:7"

    request = search.calls[0].request
    assert request.url.params["term"] == "protokoll"
    assert request.headers["OCS-APIRequest"] == "true"
    assert request.headers["Accept"] == "application/json"


@pytest.mark.anyio
async def test_search_without_a_hit_returns_an_empty_list(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=envelope({"name": "Notes", "entries": [], "cursor": None})
            )
        )
        result = await notes_tools.search(clients, query="nichts")

    assert result["count"] == 0
    assert result["results"] == []


@pytest.mark.anyio
async def test_an_empty_search_term_never_reaches_nextcloud(clients: NcClients) -> None:
    """Unified search answers an empty term with 400 "No valid filters provided"."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        search = mock.get(SEARCH_URL)

        with pytest.raises(ToolError) as excinfo:
            await notes_tools.search(clients, query="   ")

    assert search.call_count == 0
    assert excinfo.value.hint


@pytest.mark.anyio
async def test_a_hit_without_a_usable_resource_url_is_skipped(clients: NcClients) -> None:
    """Skipping beats guessing: a wrong id would resolve to a different note."""
    payload = envelope(
        {
            "name": "Notes",
            "entries": [
                {"title": "kaputt", "subline": "", "resourceUrl": "/apps/notes/"},
                {"title": "ok", "subline": "", "resourceUrl": "/index.php/apps/notes/note/9"},
                {"title": "kein feld", "subline": ""},
            ],
            "cursor": None,
        }
    )
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
        result = await notes_tools.search(clients, query="protokoll")

    assert [hit["id"] for hit in result["results"]] == ["note:9"]
    assert result["skipped"] == 2


@pytest.mark.anyio
async def test_a_foreign_resource_url_never_leaves_the_configured_instance(
    clients: NcClients,
) -> None:
    """The url is rebuilt from the configured base URL (threat T-01-39, SSRF)."""
    payload = envelope(
        {
            "name": "Notes",
            "entries": [
                {
                    "title": "geklaut",
                    "subline": "",
                    "resourceUrl": "http://evil.test/index.php/apps/notes/note/5",
                }
            ],
            "cursor": None,
        }
    )
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
        result = await notes_tools.search(clients, query="protokoll")

    assert result["results"][0]["url"] == f"{BASE}/index.php/apps/notes/note/5"


@pytest.mark.anyio
async def test_read_returns_the_stable_fields_of_one_note(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        route = mock.get(f"{NOTES_BASE}/12").mock(return_value=httpx.Response(200, json=NOTE_12))
        result = await notes_tools.read(clients, note_id="note:12")

    assert result == {
        "id": "note:12",
        "title": "Protokoll 2026-08-14",
        "content": "# Protokoll\nAnwesend: Anja, Khaled\n",
        "category": "Meetings",
        "modified": 1755180000,
        "favorite": False,
        "etag": "abc123",
        "url": f"{BASE}/index.php/apps/notes/note/12",
    }
    assert route.calls[0].request.headers["Accept"] == "application/json"


@pytest.mark.anyio
async def test_read_of_an_unknown_note_reports_not_found_without_the_notes_detail_text(
    clients: NcClients,
) -> None:
    """Changed in 27-04: the detail text of the Notes app no longer reaches the answer."""
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(f"{NOTES_BASE}/999").mock(
            return_value=httpx.Response(404, json={"status": 404, "message": "Note not found"})
        )
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.read(clients, note_id="note:999")

    assert excinfo.value.message == "Nextcloud did not find the note 999."
    assert "Nextcloud says" not in excinfo.value.message
    assert excinfo.value.hint


@pytest.mark.anyio
async def test_read_rejects_an_id_of_another_kind(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        notes_api = mock.route(url__startswith=NOTES_BASE)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.read(clients, note_id="file:4711")

    assert notes_api.call_count == 0
    assert "note" in excinfo.value.hint.lower()


@pytest.mark.anyio
@pytest.mark.parametrize("note_id", ["12/../../ocs", "", "  ", "-1", "1.5", "note:12"])
async def test_a_note_id_that_is_not_numeric_never_reaches_nextcloud(
    clients: NcClients, note_id: str
) -> None:
    """IN-05: the id goes into the URL path, so the client refuses anything but digits
    itself instead of trusting that every future caller repeats the tool layer's check
    (T-01-63, same guard as the Deck client)."""
    with respx.mock(assert_all_called=False) as mock:
        notes_api = mock.route(url__startswith=NOTES_BASE)
        with pytest.raises(ToolError) as excinfo:
            await notes_client.get_note(clients.client, clients.creds, note_id)

    assert notes_api.call_count == 0
    assert "numeric" in excinfo.value.message


@pytest.mark.parametrize("note_id", ["12/../evil", "", "javascript:alert(1)"])
def test_web_url_refuses_a_non_numeric_id(note_id: str) -> None:
    """IN-05: the link in every answer is built from the id as well, so the same guard
    holds where no request is sent at all."""
    with pytest.raises(ToolError):
        notes_client.web_url(Credentials(BASE, USER, SECRET), note_id)


def test_web_url_builds_the_link_for_a_numeric_id() -> None:
    url = notes_client.web_url(Credentials(BASE, USER, SECRET), "12")
    assert url == f"{BASE}/index.php/apps/notes/note/12"


@pytest.mark.anyio
async def test_create_returns_the_title_the_server_stored(clients: NcClients) -> None:
    """Notes sanitises and numbers titles; the server answer is the truth, not our input."""
    created = {**NOTE_12, "id": 31, "title": "Protokoll 2026-08-14 (2)", "category": ""}
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock_settings(mock)
        route = mock.post(NOTES_BASE).mock(return_value=httpx.Response(200, json=created))
        result = await notes_tools.create(
            clients, title="Protokoll 2026-08-14", content="# Protokoll\n"
        )

    assert result["id"] == "note:31"
    assert result["title"] == "Protokoll 2026-08-14 (2)"
    assert result["renamed"] is True

    body = json.loads(route.calls[0].request.content)
    assert body["title"] == "Protokoll 2026-08-14"
    assert body["content"] == "# Protokoll\n"
    assert route.calls[0].request.headers["Content-Type"].startswith("application/json")


@pytest.mark.anyio
async def test_create_without_a_rename_says_nothing_about_a_rename(clients: NcClients) -> None:
    created = {**NOTE_12, "id": 32, "title": "Einkaufsliste", "category": "Privat"}
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock_settings(mock)
        mock.post(NOTES_BASE).mock(return_value=httpx.Response(200, json=created))
        result = await notes_tools.create(
            clients, title="Einkaufsliste", content="Milch\n", category="Privat"
        )

    assert result["title"] == "Einkaufsliste"
    assert "renamed" not in result
    assert result["category"] == "Privat"


@pytest.mark.anyio
async def test_create_reports_a_full_nextcloud_with_its_own_message(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock_settings(mock)
        mock.post(NOTES_BASE).mock(
            return_value=httpx.Response(
                507, json={"status": 507, "message": "Insufficient storage"}
            )
        )
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Neu", content="Text\n")

    assert "space" in excinfo.value.message.lower()
    assert "quota" in excinfo.value.hint.lower()


@pytest.mark.anyio
async def test_create_refuses_an_empty_title(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        notes_api = mock.route(url__startswith=NOTES_BASE)
        with pytest.raises(ToolError):
            await notes_tools.create(clients, title="  ", content="Text\n")

    assert notes_api.call_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["search", "read", "create"])
async def test_a_missing_notes_app_stops_every_tool_before_the_first_request(
    clients: NcClients, tool: str
) -> None:
    """SRV-04 and D-15: one sentence with an alternative, and zero notes requests."""
    calls = {
        "search": lambda: notes_tools.search(clients, query="protokoll"),
        "read": lambda: notes_tools.read(clients, note_id="note:12"),
        "create": lambda: notes_tools.create(clients, title="Neu", content="Text\n"),
    }
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock, notes=None)
        notes_api = mock.route(url__startswith=NOTES_BASE)
        search = mock.get(SEARCH_URL)

        with pytest.raises(AppMissingError) as excinfo:
            await calls[tool]()

    assert notes_api.call_count == 0, "no request may go to an app that is not installed"
    assert search.call_count == 0
    assert excinfo.value.message == "The Notes app is not installed on this Nextcloud."
    assert "Notes app" in excinfo.value.hint


@pytest.mark.anyio
async def test_an_unknown_notes_api_version_is_reported_clearly(clients: NcClients) -> None:
    """Assumption A5: if Notes ever drops the v1 API, the tool says so instead of guessing."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock, notes={"api_version": ["2.0"], "version": "9.0.0"})
        notes_api = mock.route(url__startswith=NOTES_BASE)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.read(clients, note_id="note:12")

    assert notes_api.call_count == 0
    assert "2.0" in f"{excinfo.value.message} {excinfo.value.hint}"


@pytest.mark.anyio
async def test_a_server_error_during_the_search_is_a_degraded_answer(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(SEARCH_URL).mock(return_value=httpx.Response(502, text="bad gateway"))
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.search(clients, query="protokoll")

    assert "502" in excinfo.value.message
    assert excinfo.value.hint


# --- notes_update (APS-Conecta/gestion-aio#94) ----------------------------------------------


@pytest.mark.anyio
async def test_update_refuses_a_note_the_user_cannot_edit_and_writes_nothing(
    clients: NcClients,
) -> None:
    """A note shared without edit rights is refused before any write leaves this server."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        mock.get(f"{NOTES_BASE}/12").mock(
            return_value=httpx.Response(200, json={**NOTE_12, "readonly": True})
        )
        write = mock.put(f"{NOTES_BASE}/12")
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.update(clients, note_id="note:12", content="neu", etag="abc123")

    assert write.call_count == 0
    assert excinfo.value.reason == REASON_PERMISSION_DENIED
    assert excinfo.value.message == "The note 12 is read-only for this account."


@pytest.mark.anyio
async def test_update_sends_only_the_content_under_the_quoted_etag(clients: NcClients) -> None:
    """Title and category stay out of the body, so the file is never renamed or moved."""
    stored = {**NOTE_12, "content": "neu", "etag": "def456", "modified": 1755190000}
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(f"{NOTES_BASE}/12").mock(return_value=httpx.Response(200, json=NOTE_12))
        write = mock.put(f"{NOTES_BASE}/12").mock(return_value=httpx.Response(200, json=stored))
        result = await notes_tools.update(clients, note_id="note:12", content="neu", etag="abc123")

    request = write.calls[0].request
    assert request.headers["If-Match"] == '"abc123"'
    assert json.loads(request.content) == {"content": "neu"}
    assert result == {
        "id": "note:12",
        "title": "Protokoll 2026-08-14",
        "modified": 1755190000,
        "etag": "def456",
        "url": f"{BASE}/index.php/apps/notes/note/12",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, REASON_PERMISSION_DENIED), (412, "unspecified")],
)
async def test_update_reports_a_write_notes_itself_refuses(
    clients: NcClients, status: int, reason: str
) -> None:
    """403: no edit right Notes knows of; 412: the note changed since it was read."""
    with respx.mock(assert_all_called=True) as mock:
        mock_capabilities(mock)
        mock.get(f"{NOTES_BASE}/12").mock(return_value=httpx.Response(200, json=NOTE_12))
        mock.put(f"{NOTES_BASE}/12").mock(
            return_value=httpx.Response(status, json={"status": status, "message": "x"})
        )
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.update(clients, note_id="note:12", content="neu", etag="old")

    assert excinfo.value.reason == reason


@pytest.mark.anyio
async def test_update_without_an_etag_never_writes(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        mock.get(f"{NOTES_BASE}/12").mock(return_value=httpx.Response(200, json=NOTE_12))
        write = mock.put(f"{NOTES_BASE}/12")
        with pytest.raises(ToolError):
            await notes_tools.update(clients, note_id="note:12", content="neu", etag=" ")

    assert write.call_count == 0
