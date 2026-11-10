"""The notes family against the real ``kein-ki`` guard and NC_MCP_FILES_ROOT (EXCL-05, SBX-02).

A note id is the file id of the note's Markdown file (25-MESSBERICHT K4), so the guard's
file id set decides directly, and ``dav.paths_of_fileids`` resolves the path whenever a
tagged folder or the sandbox needs it. Three states, three answers:

*   ``untagged`` leaves every answer as it was, with no extra request beyond the listing.
*   ``active`` withholds silently: a tagged note is gone from the search without a counter,
    and ``notes_read`` answers exactly like an unknown id, without the foreign text of the
    Notes app (no file id oracle).
*   ``unverifiable`` speaks once: one ``degraded`` entry in the search, and the one
    ``withhold.unavailable_error()`` for every id and every write.

No ``patch_untagged`` here: every test mocks the guard's own requests.
"""

import json
from typing import Any

import guard_routes
import httpx
import pytest
import respx
from guard_routes import BASE, USER

from mcp_connector.errors import REASON_UNKNOWN_ID, ToolError
from mcp_connector.nextcloud import NcClients, capabilities
from mcp_connector.nextcloud.clients import dav
from mcp_connector.nextcloud.clients import xml as davxml
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import notes as notes_tools
from mcp_connector.tools import withhold

SECRET = "app-password-test"

CAPABILITIES_URL = f"{BASE}/ocs/v2.php/cloud/capabilities"
PROVIDER_URL = f"{BASE}/ocs/v2.php/search/providers/notes/search"
NOTES_BASE = f"{BASE}/index.php/apps/notes/api/v1/notes"
SETTINGS_URL = f"{BASE}/index.php/apps/notes/api/v1/settings"
DAV_SEARCH_URL = f"{BASE}/remote.php/dav/"
XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}

NOT_FOUND_HINT = "Search for it first; the id or the name is unknown to this instance."

#: A tagged folder of notes (the category folder of measurement K4) and its file id.
GEHEIM = ("Notes/Geheim", "932", True)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    capabilities.clear_cache()
    guard_routes.reset()
    monkeypatch.delenv("NC_MCP_FILES_ROOT", raising=False)


@pytest.fixture
def clients() -> NcClients:
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False),
        creds=Credentials(BASE, USER, SECRET),
    )


def triple(error: ToolError) -> tuple[str, str, str]:
    return (error.message, error.hint, error.reason)


def envelope(data: Any) -> dict:
    return {"ocs": {"meta": {"status": "ok", "statuscode": 200, "message": "OK"}, "data": data}}


def mock_capabilities(mock: respx.MockRouter) -> None:
    payload = envelope(
        {"capabilities": {"core": {}, "notes": {"api_version": ["1.3"], "version": "6.1.0"}}}
    )
    mock.get(CAPABILITIES_URL).mock(return_value=httpx.Response(200, json=payload))


def hit(note_id: str, title: str = "") -> dict[str, Any]:
    return {
        "title": title or f"Notiz {note_id}",
        "subline": "",
        "resourceUrl": f"{BASE}/index.php/apps/notes/note/{note_id}",
        "attributes": [],
    }


def mock_provider(mock: respx.MockRouter, *note_ids: str) -> respx.Route:
    payload = envelope({"name": "Notes", "entries": [hit(i) for i in note_ids], "cursor": None})
    return mock.get(PROVIDER_URL).mock(return_value=httpx.Response(200, json=payload))


def fileid_answer(*entries: tuple[str, str]) -> httpx.Response:
    """A 207 SEARCH answer of ``paths_of_fileids``; each entry is (href suffix, fileid)."""
    responses = "".join(
        f"<d:response><d:href>/remote.php/dav/files/{USER}/{suffix}</d:href><d:propstat>"
        f"<d:prop><oc:fileid>{fileid}</oc:fileid><d:resourcetype/></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        for suffix, fileid in entries
    )
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<d:multistatus xmlns:d="DAV:" xmlns:oc="{davxml.OC}">{responses}</d:multistatus>'
    ).encode()
    return httpx.Response(207, content=body, headers=XML_HEADERS)


def mock_fileids(mock: respx.MockRouter, *entries: tuple[str, str]) -> respx.Route:
    return mock.route(method="SEARCH", url=DAV_SEARCH_URL).mock(
        return_value=fileid_answer(*entries)
    )


def note(note_id: str, category: str = "") -> dict[str, Any]:
    return {
        "id": int(note_id),
        "title": f"Notiz {note_id}",
        "content": "geheimer Inhalt\n",
        "category": category,
        "favorite": False,
        "modified": 1789584914,
    }


def mock_note(mock: respx.MockRouter, note_id: str, category: str = "") -> respx.Route:
    return mock.get(f"{NOTES_BASE}/{note_id}").mock(
        return_value=httpx.Response(200, json=note(note_id, category))
    )


def mock_note_404(mock: respx.MockRouter, note_id: str, detail: str) -> respx.Route:
    return mock.get(f"{NOTES_BASE}/{note_id}").mock(
        return_value=httpx.Response(404, json={"status": 404, "message": detail})
    )


def report_route(mock: respx.MockRouter) -> respx.Route:
    """A REPORT counter for the states that must not send one (untagged mocks none)."""
    return mock.route(method="REPORT", url=guard_routes.HOME)


def expected_hit(note_id: str) -> dict[str, str]:
    return {
        "id": f"note:{note_id}",
        "title": f"Notiz {note_id}",
        "excerpt": "",
        "url": f"{BASE}/index.php/apps/notes/note/{note_id}",
    }


# --- notes_search --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_untagged_at_root_is_unchanged_and_resolves_no_path(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        report = report_route(mock)
        fileids = mock.route(method="SEARCH", url=DAV_SEARCH_URL)
        mock_provider(mock, "933", "934")

        result = await notes_tools.search(clients, query="notiz")

    assert result == {"count": 2, "results": [expected_hit("933"), expected_hit("934")]}
    assert fileids.call_count == 0
    assert report.call_count == 0


@pytest.mark.anyio
async def test_search_drops_a_tagged_note_file_silently_without_a_path_lookup(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        _, report = guard_routes.active(mock, ("Notes/Offen/a.md", "933", False))
        fileids = mock.route(method="SEARCH", url=DAV_SEARCH_URL)
        mock_provider(mock, "933", "934")

        result = await notes_tools.search(clients, query="notiz")

    assert result == {"count": 1, "results": [expected_hit("934")]}
    assert "skipped" not in result
    assert fileids.call_count == 0
    assert report.call_count == 1


@pytest.mark.anyio
async def test_search_drops_a_note_below_a_tagged_category_folder(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.active(mock, GEHEIM)
        fileids = mock_fileids(mock, ("Notes/Geheim/a.md", "933"), ("Notes/Offen/b.md", "934"))
        mock_provider(mock, "933", "934")

        result = await notes_tools.search(clients, query="notiz")

    assert result == {"count": 1, "results": [expected_hit("934")]}
    assert fileids.call_count == 1
    body = fileids.calls[0].request.content
    assert b"933" in body
    assert b"934" in body


@pytest.mark.anyio
async def test_search_counts_a_note_outside_the_sandbox_as_skipped(
    clients: NcClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-27-04: a sandbox drop is counted like before; it says nothing about a tag."""
    monkeypatch.setenv("NC_MCP_FILES_ROOT", "/Shared/KI")
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        # 951 lives in /Notes/x.md, outside the scope, so the SEARCH does not return it.
        fileids = mock_fileids(mock, ("Shared/KI/Notizen/y.md", "950"))
        mock_provider(mock, "950", "951")

        result = await notes_tools.search(clients, query="notiz")

    assert result == {"count": 1, "results": [expected_hit("950")], "skipped": 1}
    assert fileids.call_count == 1


@pytest.mark.anyio
async def test_search_unverifiable_withholds_everything_and_says_so_once(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.unverifiable(mock)
        fileids = mock.route(method="SEARCH", url=DAV_SEARCH_URL)
        mock_provider(mock, "933", "934")

        result = await notes_tools.search(clients, query="notiz")

    assert result == {
        "count": 0,
        "results": [],
        "degraded": [withhold.degraded_entry("source")],
    }
    assert result["degraded"] == [{"source": "exclusion", "reason": withhold.EXCLUSION_UNAVAILABLE}]
    assert fileids.call_count == 0


@pytest.mark.anyio
async def test_search_withholds_every_hit_when_the_path_lookup_fails(clients: NcClients) -> None:
    """Fail-closed: a failed lookup is 'could not check', never 'not found' or 'allowed'."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.active(mock, GEHEIM)
        mock.route(method="SEARCH", url=DAV_SEARCH_URL).mock(return_value=httpx.Response(500))
        mock_provider(mock, "933", "934")

        result = await notes_tools.search(clients, query="notiz")

    assert result["results"] == []
    assert result["count"] == 0
    assert result["degraded"] == [withhold.degraded_entry("source")]


# --- notes_read ----------------------------------------------------------------------------


def _scenario_tagged_note(mock: respx.MockRouter) -> None:
    guard_routes.active(mock, ("Notes/Offen/geheim.md", "933", False))
    mock_note(mock, "933", "Offen")


def _scenario_below_tagged_category(mock: respx.MockRouter) -> None:
    guard_routes.active(mock, GEHEIM)
    mock_fileids(mock, ("Notes/Geheim/a.md", "933"))
    mock_note(mock, "933", "Geheim")


def _scenario_outside_the_sandbox(mock: respx.MockRouter) -> None:
    guard_routes.untagged(mock)
    mock_fileids(mock)  # the note lives in /Notes, the scope is /Shared/KI
    mock_note(mock, "933")


def _scenario_tagged_file_that_is_no_note(mock: respx.MockRouter) -> None:
    guard_routes.active(mock, ("Dokumente/plan.pdf", "933", False))
    mock_note_404(mock, "933", "File is not a note: Dokumente/plan.pdf")


def _scenario_unknown_id(mock: respx.MockRouter) -> None:
    guard_routes.untagged(mock)
    mock_note_404(mock, "933", "Note not found")


READ_SCENARIOS = {
    "a_tagged_note": (_scenario_tagged_note, "/"),
    "b_below_tagged_category": (_scenario_below_tagged_category, "/"),
    "c_outside_the_sandbox": (_scenario_outside_the_sandbox, "/Shared/KI"),
    "d_tagged_file_no_note": (_scenario_tagged_file_that_is_no_note, "/"),
    "e_unknown_id": (_scenario_unknown_id, "/"),
}


@pytest.mark.anyio
async def test_read_answers_all_five_not_found_cases_with_one_identical_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pair test of notes_read: nothing tells a tagged note from an unknown id."""
    answers: dict[str, tuple[str, str, str]] = {}
    for name, (scenario, root) in READ_SCENARIOS.items():
        capabilities.clear_cache()
        guard_routes.reset()
        monkeypatch.setenv("NC_MCP_FILES_ROOT", root)
        fresh = NcClients(
            client=httpx.AsyncClient(follow_redirects=False),
            creds=Credentials(BASE, USER, SECRET),
        )
        with respx.mock(assert_all_called=False) as mock:
            mock_capabilities(mock)
            scenario(mock)
            with pytest.raises(ToolError) as excinfo:
                await notes_tools.read(fresh, note_id="note:933")
        answers[name] = triple(excinfo.value)

    expected = ("Nextcloud did not find the note 933.", NOT_FOUND_HINT, REASON_UNKNOWN_ID)
    assert answers == dict.fromkeys(READ_SCENARIOS, expected)
    assert all("Nextcloud says" not in message for message, _, _ in answers.values())


def test_note_not_found_is_the_sentence_of_an_unknown_note() -> None:
    assert triple(notes_tools._note_not_found("12")) == (
        "Nextcloud did not find the note 12.",
        NOT_FOUND_HINT,
        REASON_UNKNOWN_ID,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("note_id", ["933", "999"])
async def test_read_unverifiable_answers_every_id_the_same(
    clients: NcClients, note_id: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.unverifiable(mock)
        mock_note(mock, "933")
        mock_note_404(mock, "999", "Note not found")
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.read(clients, note_id=f"note:{note_id}")

    assert triple(excinfo.value) == triple(withhold.unavailable_error())


def _fresh_clients() -> NcClients:
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False),
        creds=Credentials(BASE, USER, SECRET),
    )


async def _read_with_note_status(note_id: str, status: int) -> tuple[str, str, str]:
    """notes_read with 933 tagged and the Notes GET of ``note_id`` answering ``status``."""
    guard_routes.reset()
    capabilities.clear_cache()
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.active(mock, ("Notes/Offen/geheim.md", "933", False))
        if status == 404:
            mock_note_404(mock, note_id, "Note not found")
        else:
            mock.get(f"{NOTES_BASE}/{note_id}").mock(return_value=httpx.Response(status))
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.read(_fresh_clients(), note_id=f"note:{note_id}")
    return triple(excinfo.value)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [500, 503])
async def test_read_a_failing_notes_get_answers_a_tagged_and_an_unknown_id_alike(
    status: int,
) -> None:
    """D-28-17: the error of the Notes GET answers before the tag, like files._visible_stat."""
    tagged = await _read_with_note_status("933", status)
    invented = await _read_with_note_status("999", status)

    assert tagged == tuple(part.replace("999", "933") for part in invented)
    assert tagged != triple(notes_tools._note_not_found("933"))


@pytest.mark.anyio
async def test_read_a_notes_404_of_a_tagged_id_stays_note_not_found() -> None:
    tagged = await _read_with_note_status("933", 404)
    invented = await _read_with_note_status("999", 404)

    assert tagged == triple(notes_tools._note_not_found("933"))
    assert invented == triple(notes_tools._note_not_found("999"))


@pytest.mark.anyio
async def test_read_unverifiable_answers_before_a_failing_notes_get(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.unverifiable(mock)
        mock.get(f"{NOTES_BASE}/933").mock(return_value=httpx.Response(500))
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.read(clients, note_id="note:933")

    assert triple(excinfo.value) == triple(withhold.unavailable_error())


@pytest.mark.anyio
async def test_read_untagged_at_root_is_unchanged(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        fileids = mock.route(method="SEARCH", url=DAV_SEARCH_URL)
        route = mock_note(mock, "933", "Offen")

        result = await notes_tools.read(clients, note_id="note:933")

    assert result == {
        "id": "note:933",
        "title": "Notiz 933",
        "content": "geheimer Inhalt\n",
        "category": "Offen",
        "modified": 1789584914,
        "favorite": False,
        "etag": "",
        "url": f"{BASE}/index.php/apps/notes/note/933",
    }
    assert route.call_count == 1
    assert fileids.call_count == 0


@pytest.mark.anyio
async def test_read_of_a_note_inside_the_sandbox_passes(
    clients: NcClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NC_MCP_FILES_ROOT", "/Shared/KI")
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        fileids = mock_fileids(mock, ("Shared/KI/Notizen/n.md", "950"))
        mock_note(mock, "950")

        result = await notes_tools.read(clients, note_id="950")

    assert result["id"] == "note:950"
    assert fileids.call_count == 1


@pytest.mark.anyio
async def test_read_withholds_when_the_path_lookup_fails(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.active(mock, GEHEIM)
        mock.route(method="SEARCH", url=DAV_SEARCH_URL).mock(return_value=httpx.Response(500))
        mock_note(mock, "933")
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.read(clients, note_id="note:933")

    assert triple(excinfo.value) == triple(withhold.unavailable_error())


@pytest.mark.anyio
async def test_read_refuses_an_id_of_another_kind_before_any_request(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        listing = guard_routes.untagged(mock)
        notes_api = mock.route(url__startswith=NOTES_BASE)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.read(clients, note_id="card:1")

    assert "not a note id" in excinfo.value.message
    assert listing.call_count == 0
    assert notes_api.call_count == 0


# --- notes_create --------------------------------------------------------------------------

#: Marks a settings key the answer leaves out entirely.
_MISSING = object()


def settings_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "notesPath": "Notes",
        "fileSuffix": ".md",
        "noteMode": "rich",
        "showHidden": False,
        "loadRecentOnStartUp": True,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not _MISSING}


def mock_settings(mock: respx.MockRouter, **overrides: Any) -> respx.Route:
    return mock.get(SETTINGS_URL).mock(
        return_value=httpx.Response(200, json=settings_payload(**overrides))
    )


def mock_post(mock: respx.MockRouter, title: str = "Plan", category: str = "") -> respx.Route:
    created = {**note("960", category), "title": title}
    return mock.post(NOTES_BASE).mock(return_value=httpx.Response(200, json=created))


@pytest.mark.anyio
async def test_create_untagged_at_root_writes_with_one_settings_request(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        report = report_route(mock)
        settings = mock_settings(mock)
        post = mock_post(mock, title="Plan (2)", category="Offen")

        result = await notes_tools.create(clients, title="Plan", content="x\n", category="Offen")

    assert result["id"] == "note:960"
    assert result["renamed"] is True
    assert settings.call_count == 1
    assert post.call_count == 1
    assert report.call_count == 0
    assert json.loads(post.calls[0].request.content)["category"] == "Offen"


@pytest.mark.anyio
@pytest.mark.parametrize("category", ["Geheim", "Geheim/Unter", "/Geheim/"])
async def test_create_refuses_a_tagged_category_like_a_missing_parent(
    clients: NcClients, category: str
) -> None:
    """D-27-01 analogy: an excluded place does not exist, for writing as for reading."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.active(mock, GEHEIM)
        mock_settings(mock)
        post = mock_post(mock)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Plan", content="x\n", category=category)

    folder = "/Notes/" + category.strip("/")
    assert triple(excinfo.value) == triple(dav.parent_missing(f"{folder}/Plan.md"))
    assert post.call_count == 0


@pytest.mark.anyio
async def test_create_refuses_a_collision_with_a_tagged_note_before_writing(
    clients: NcClients,
) -> None:
    """No renamed oracle: the write never happens, so no '(2)' can betray the tagged file."""
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.active(mock, ("Notes/Offen/Plan.md", "970", False))
        mock_settings(mock)
        post = mock_post(mock)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Plan", content="x\n", category="Offen")

    assert triple(excinfo.value) == triple(dav.parent_missing("/Notes/Offen/Plan.md"))
    assert post.call_count == 0


@pytest.mark.anyio
async def test_create_beside_a_tagged_note_goes_through(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.active(mock, ("Notes/Offen/Plan.md", "970", False))
        mock_settings(mock)
        post = mock_post(mock, title="Anderes", category="Offen")

        result = await notes_tools.create(clients, title="Anderes", content="x\n", category="Offen")

    assert result["title"] == "Anderes"
    assert "renamed" not in result
    assert post.call_count == 1


@pytest.mark.anyio
async def test_create_uses_the_configured_file_suffix(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.active(mock, ("Notes/Offen/Plan.txt", "970", False))
        mock_settings(mock, fileSuffix=".txt")
        post = mock_post(mock)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Plan", content="x\n", category="Offen")

    assert triple(excinfo.value) == triple(dav.parent_missing("/Notes/Offen/Plan.txt"))
    assert post.call_count == 0


@pytest.mark.anyio
async def test_create_outside_the_sandbox_names_the_sandbox_not_a_tag(
    clients: NcClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NC_MCP_FILES_ROOT", "/Shared/KI")
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        mock_settings(mock)
        post = mock_post(mock)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Plan", content="x\n")

    assert excinfo.value.message == (
        "Notes are stored in /Notes, outside the folder this server may access."
    )
    assert "NC_MCP_FILES_ROOT" in excinfo.value.hint
    assert "kein-ki" not in f"{excinfo.value.message} {excinfo.value.hint}"
    assert post.call_count == 0


@pytest.mark.anyio
async def test_create_inside_the_sandbox_goes_through(
    clients: NcClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NC_MCP_FILES_ROOT", "/Notes")
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        mock_settings(mock)
        post = mock_post(mock)

        await notes_tools.create(clients, title="Plan", content="x\n")

    assert post.call_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("category", [None, "Offen", "Geheim"])
async def test_create_unverifiable_refuses_every_category_the_same(
    clients: NcClients, category: str | None
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.unverifiable(mock)
        mock_settings(mock)
        post = mock_post(mock)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Plan", content="x\n", category=category)

    assert triple(excinfo.value) == triple(withhold.unavailable_error())
    assert post.call_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("notes_path", [_MISSING, None, 7, "", "  ", "/"])
async def test_create_without_a_usable_notes_path_fails_closed(
    clients: NcClients, notes_path: object
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        mock_settings(mock, notesPath=notes_path)
        post = mock_post(mock)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Plan", content="x\n")

    assert triple(excinfo.value) == triple(withhold.unavailable_error())
    assert post.call_count == 0


@pytest.mark.anyio
async def test_create_fails_closed_when_the_settings_request_fails(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        mock.get(SETTINGS_URL).mock(return_value=httpx.Response(500))
        post = mock_post(mock)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Plan", content="x\n")

    assert triple(excinfo.value) == triple(withhold.unavailable_error())
    assert post.call_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("category", ["..", "../Privat", "Offen/../../x"])
async def test_create_refuses_a_dot_segment_category_before_writing(
    clients: NcClients, category: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_capabilities(mock)
        guard_routes.untagged(mock)
        mock_settings(mock)
        post = mock_post(mock)
        with pytest.raises(ToolError) as excinfo:
            await notes_tools.create(clients, title="Plan", content="x\n", category=category)

    assert "outside the folder this server may access" in excinfo.value.message
    assert post.call_count == 0
