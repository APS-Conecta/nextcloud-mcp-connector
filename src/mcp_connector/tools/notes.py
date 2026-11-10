"""Notes tools: search, read, create and update (D-05, gestion-aio#94).

The search does not use the Notes REST API, because it has no search route at all: the
routes are index, get, create, update, undo, autotitle, destroy, category, attachment and
settings. The server side search lives in the unified search provider ``notes``, which
matches title **and** content, sorts by modification date and answers in one request. Title
and excerpt come straight out of that answer, so a search over twenty hits stays one round
trip instead of twenty one.

    GET /ocs/v2.php/search/providers/notes/search?term=...&limit=...

Documented fallback, deliberately not built: if the Notes app is installed but the provider
is missing, ``GET /notes?exclude=content`` plus a client side title match would still find
something. That path matches titles only, so it is a degraded answer and would have to be
marked as one. The list helper it would need was written and then removed again, because an
untested branch nobody reaches is a liability, not a spare part. It is twenty lines and can
come back the day an instance without the provider actually turns up.

Two details keep the ids honest. The unified search entry has no ``id`` field, so the note
id is parsed out of ``resourceUrl``, and an entry whose URL does not end in a numeric note
segment is skipped rather than guessed at: a wrong id resolves to a different note, which
is worse than one missing hit (threat T-01-40). And ``resourceUrl`` is only ever parsed,
never fetched; the returned link is rebuilt from the configured base URL, so a manipulated
entry cannot point this server or its user at a foreign host (threat T-01-39).

The ``kein-ki`` guard and the sandbox (EXCL-05, SBX-02). A note id is the file id of the
note's file (25-MESSBERICHT K4), so the tagged file id set decides directly. The path comes
from ``dav.paths_of_fileids`` only when it is needed, that is when a folder is tagged (a
tagged category folder covers every note below it) or when ``NC_MCP_FILES_ROOT`` is not
``/``; the same lookup is the sandbox check, because its scope is the sandbox. The search
drops a tagged note silently and counts a note outside the sandbox in ``skipped`` like any
other unusable hit (D-27-04). ``notes_read`` answers every miss with one sentence,
:func:`_note_not_found`, and never passes the Notes app's own detail text on: that text
tells a note from any other file id, which would make the tool an oracle over file ids.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import ids
from ..errors import REASON_PERMISSION_DENIED, REASON_UNKNOWN_ID, ToolError
from ..nextcloud import NcClients, capabilities
from ..nextcloud.clients import dav, ocs
from ..nextcloud.clients import notes as notes_client
from ..nextcloud.exclusion import TagScope
from . import withhold

APP = "notes"

#: The unified search provider id of the Notes app.
SEARCH_PROVIDER_PATH = "/search/providers/notes/search"

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_ID_HINT = "Use an id from notes_search, for example note:12."

#: The shared file id lookup of one ``prepare_context`` bundle, as a note excerpt awaits it
#: (plan 27-10): a file id onto its entry, onto ``None``, or left out.
NoteBatch = Callable[[], Awaitable[Mapping[str, Mapping[str, Any] | None]]]

#: What a failed path lookup may raise; the same tuple as ``chatgpt.LOOKUP_FAILURES``,
#: spelled out here because this module must not import ``chatgpt`` (it imports this one).
_LOOKUP_FAILURES: tuple[type[Exception], ...] = (ToolError, httpx.HTTPError, ValueError)


async def search(clients: NcClients, query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Search notes by title and content and return compact hits."""
    await _ready(clients)

    term = (query or "").strip()
    if not term:
        raise ToolError(
            message="The search term is empty.",
            hint="Give at least one word; Nextcloud rejects a search without a term.",
        )
    if limit < 1 or limit > MAX_LIMIT:
        raise ToolError(
            message=f"limit must be between 1 and {MAX_LIMIT} (got {limit}).",
            hint=f"Leave it out for the default of {DEFAULT_LIMIT} hits.",
        )

    scope, response = await asyncio.gather(
        clients.exclusion.scope(clients),
        ocs.ocs_get(
            clients.client,
            clients.creds,
            SEARCH_PROVIDER_PATH,
            params={"term": term, "limit": limit},
        ),
        return_exceptions=True,
    )
    if isinstance(scope, BaseException):
        raise scope
    if isinstance(response, BaseException):
        raise response
    data = ocs.parse_ocs(response, what="the note search")
    entries = data.get("entries") if isinstance(data, dict) else None
    entries = entries if isinstance(entries, list) else []

    hits: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        note_id = _note_id_from_resource_url(entry.get("resourceUrl"))
        if note_id is None:
            skipped += 1
            continue
        hits.append((note_id, entry))

    if scope.state == "unverifiable":
        return _withheld_search(skipped)

    paths: dict[str, str] | None = None
    if hits and withhold.needs_paths(scope):
        try:
            paths = await dav.paths_of_fileids(
                clients.client, clients.creds, [note_id for note_id, _ in hits]
            )
        except (ToolError, httpx.HTTPError, ValueError):
            # Fail-closed: a failed lookup is "could not check", never "allowed".
            return _withheld_search(skipped)

    results: list[dict[str, str]] = []
    for note_id, entry in hits:
        path: str | None = None
        if paths is not None:
            path = paths.get(note_id)
            if path is None:
                # Outside NC_MCP_FILES_ROOT (or gone): a sandbox drop, counted (D-27-04).
                skipped += 1
                continue
        if scope.excludes(path=path, fileid=note_id):
            # Tagged, or below a tagged category folder: withheld without a trace.
            continue
        results.append(
            {
                "id": ids.encode_note(note_id),
                "title": str(entry.get("title") or ""),
                "excerpt": str(entry.get("subline") or ""),
                "url": notes_client.web_url(clients.creds, note_id),
            }
        )

    result: dict[str, Any] = {"count": len(results), "results": results}
    if skipped:
        # Named, not swallowed: the model should be able to say "some hits were unusable"
        # instead of silently reporting fewer notes than the user can see in the web UI.
        result["skipped"] = skipped
    return result


async def read(
    clients: NcClients,
    note_id: str,
    *,
    batch: NoteBatch | None = None,
) -> dict[str, Any]:
    """Read one note including its full content.

    The note id is the file id of the note (K4), so the guard and the sandbox (SBX-02) ask
    about exactly that id. A tagged note, a note below a tagged category, a note outside
    ``NC_MCP_FILES_ROOT``, a file that is no note and an unknown id all answer with the one
    sentence of :func:`_note_not_found`; the note fetched in parallel never leaves this
    function in any of those branches. When the check cannot be answered, every id gets
    the same ``withhold.unavailable_error()``. The answers are read in the order of
    ``files._visible_stat``: an error of the guard, ``unverifiable``, an error of the note
    fetched in parallel (a 404 or 998 first becomes :func:`_note_not_found`), then the tag,
    so a failing Notes app answers a tagged id and an unknown one alike (D-28-17).

    ``batch`` is Python only and never on the wire: ``prepare_context`` hands it in so the
    path check of a note excerpt rides in the one file id SEARCH of its bundle (plan
    27-10). It is only awaited where the path is needed, see :func:`_check_note_path`.
    """
    note = await _visible_note(clients, note_id, batch)
    stored_id = str(note.get("id", _plain_note_id(note_id)))
    return {
        "id": ids.encode_note(stored_id),
        "title": str(note.get("title") or ""),
        "content": str(note.get("content") or ""),
        "category": str(note.get("category") or ""),
        "modified": note.get("modified"),
        "favorite": bool(note.get("favorite")),
        "etag": str(note.get("etag") or ""),
        "url": notes_client.web_url(clients.creds, stored_id),
    }


async def update(clients: NcClients, note_id: str, content: str, etag: str) -> dict[str, Any]:
    """Replace the text of one note, only while its ETag still matches (gestion-aio#94).

    The note goes through the guard and the sandbox of :func:`read` first, so a note
    ``notes_read`` does not hand out cannot be written either, and it fails with the same
    sentence. A read-only note (shared without edit rights) is refused before any write;
    Notes itself refuses one with 403 as well. ``If-Match`` makes the write compare and
    swap: a note that changed since it was read is answered with 412 and left alone. Only
    the content is sent, so title and category, and with them the file name and folder,
    stay as they are.
    """
    note = await _visible_note(clients, note_id)
    raw = _plain_note_id(note_id)
    if note.get("readonly"):
        raise ToolError(
            message=f"The note {raw} is read-only for this account.",
            hint="Ask the owner of the note for edit rights in Nextcloud.",
            reason=REASON_PERMISSION_DENIED,
        )
    stored = await notes_client.update_note(
        clients.client, clients.creds, raw, content=content or "", etag=etag
    )
    return {
        "id": ids.encode_note(raw),
        "title": str(stored.get("title") or ""),
        "modified": stored.get("modified"),
        "etag": str(stored.get("etag") or ""),
        "url": notes_client.web_url(clients.creds, raw),
    }


async def _visible_note(
    clients: NcClients, note_id: str, batch: NoteBatch | None = None
) -> dict[str, Any]:
    """The note behind ``note_id`` as Notes stored it, or the refusal :func:`read` owes."""
    await _ready(clients)
    raw = _plain_note_id(note_id)

    scope, fetched = await asyncio.gather(
        clients.exclusion.scope(clients),
        notes_client.get_note(clients.client, clients.creds, raw),
        return_exceptions=True,
    )
    if isinstance(scope, BaseException):
        raise scope
    if scope.state == "unverifiable":
        raise withhold.unavailable_error()
    if isinstance(fetched, ToolError) and fetched.reason == REASON_UNKNOWN_ID:
        # A 404 or 998 of the Notes app, without its detail text (no file id oracle).
        raise _note_not_found(raw) from None
    if isinstance(fetched, BaseException):
        raise fetched
    if scope.excludes(fileid=raw):
        raise _note_not_found(raw)
    if withhold.needs_paths(scope):
        await _check_note_path(clients, scope, raw, batch)
    return fetched


async def create(
    clients: NcClients, title: str, content: str, category: str | None = None
) -> dict[str, Any]:
    """Create a note and report what the server actually stored.

    Notes sanitises titles and numbers a collision, so the answer can carry a different
    title than the one that was asked for. That title is the truth and goes back
    unchanged; ``renamed`` marks the case so the model can mention it instead of telling
    the user about a note that does not exist under that name.

    Before anything is written, the target is checked (Resolution Open Question 1 of phase
    27). The folder is ``/`` plus ``notesPath`` from the Notes settings plus the category,
    the candidate file is that folder plus the title plus ``fileSuffix``. A folder outside
    ``NC_MCP_FILES_ROOT`` is refused with its own honest sentence, which says nothing about
    a tag. A tagged folder, a tagged ancestor or a tagged candidate file is refused with
    ``dav.parent_missing`` of the candidate, the sentence of an upload into a missing
    folder (D-27-01 analogy: excluded means absent, for writing as well). That refusal comes
    before the write, so a collision with a tagged note can never show up as ``renamed``.
    When the check cannot be answered, or the settings are unusable, every write is refused
    with ``withhold.unavailable_error()`` and no request is sent.

    Known limit (phase 29): Notes sanitises the title into a file name, so the real name
    can differ from the candidate path, and every refusal still differs from a success.
    """
    await _ready(clients)

    wanted = (title or "").strip()
    if not wanted:
        raise ToolError(
            message="A note needs a title.",
            hint="Give a short title, for example 'Protokoll 2026-08-14'.",
        )

    scope, settings = await asyncio.gather(
        clients.exclusion.scope(clients),
        notes_client.get_settings(clients.client, clients.creds),
        return_exceptions=True,
    )
    if isinstance(scope, BaseException):
        raise scope
    if scope.state == "unverifiable":
        raise withhold.unavailable_error()
    if isinstance(settings, Exception):
        raise withhold.unavailable_error() from None
    if isinstance(settings, BaseException):
        raise settings
    notes_path = settings.get("notesPath")
    if not isinstance(notes_path, str) or not notes_path.strip().strip("/"):
        raise withhold.unavailable_error()

    folder = "/" + notes_path.strip().strip("/")
    wanted_category = (category or "").strip().strip("/")
    if wanted_category:
        folder = f"{folder}/{wanted_category}"
    raw_suffix = settings.get("fileSuffix")
    suffix = raw_suffix if isinstance(raw_suffix, str) and raw_suffix.startswith(".") else ".md"
    candidate = f"{folder}/{wanted}{suffix}"

    if not dav.in_files_root(folder):
        raise ToolError(
            message=f"Notes are stored in {folder}, outside the folder this server may access.",
            hint=(
                "Ask an administrator to include the Notes folder in NC_MCP_FILES_ROOT, "
                "or write the text with files_upload instead."
            ),
        )
    if scope.excludes(path=candidate):
        raise dav.parent_missing(candidate)

    note = await notes_client.create_note(
        clients.client,
        clients.creds,
        title=wanted,
        content=content or "",
        category=(category or "").strip() or None,
    )

    stored_id = str(note.get("id", ""))
    if not stored_id:
        raise ToolError(
            message="Nextcloud created the note but reported no id.",
            hint="Look for the note in the Notes app; it was probably created.",
        )
    stored_title = str(note.get("title") or "")
    result: dict[str, Any] = {
        "id": ids.encode_note(stored_id),
        "title": stored_title,
        "category": str(note.get("category") or ""),
        "modified": note.get("modified"),
        "url": notes_client.web_url(clients.creds, stored_id),
    }
    if stored_title != wanted:
        result["renamed"] = True
    return result


def _note_not_found(note_id: str) -> ToolError:
    """The one answer of every note ``notes_read`` does not hand out.

    The sentence of a Notes 404 without the ``Nextcloud says: ...`` suffix: the detail text
    of the Notes app differs between "no such file" and "a file, but not a note", so it
    would turn the tool into an oracle over file ids.
    """
    return ToolError(
        message=f"Nextcloud did not find the note {note_id}.",
        hint="Search for it first; the id or the name is unknown to this instance.",
        reason=REASON_UNKNOWN_ID,
    )


def _withheld_search(skipped: int) -> dict[str, Any]:
    """The search answer while the check cannot be answered: nothing, said once."""
    result: dict[str, Any] = {
        "count": 0,
        "results": [],
        "degraded": [withhold.degraded_entry("source")],
    }
    if skipped:
        result["skipped"] = skipped
    return result


async def _check_note_path(
    clients: NcClients,
    scope: TagScope,
    note_id: str,
    batch: NoteBatch | None = None,
) -> None:
    """Resolve the note's path and refuse it outside the sandbox or below a tagged folder.

    With ``batch``, the source of the path changes and the decision does not (plan 27-10,
    the wall clock gap left after 27-09). The entry comes from the one file id SEARCH of
    the same tool call, asked in the sandbox scope, so ``None`` there means what a missing
    id means below: the first response for the id lies outside ``NC_MCP_FILES_ROOT`` or
    there is none. An id the batch left out (crowded out of a full block) and a batch that
    failed take the single lookup below, which words every outcome as it always did. The
    path never leaves this function, and nothing of the batch outlives the call (E3).
    """
    if batch is not None:
        try:
            entries = await batch()
        except _LOOKUP_FAILURES:
            entries = None
        if entries is not None and note_id in entries:
            entry = entries[note_id]
            batched = None if entry is None else str(entry["path"])
            if batched is None or scope.excludes(path=batched, fileid=note_id):
                raise _note_not_found(note_id)
            return
    try:
        paths = await dav.paths_of_fileids(clients.client, clients.creds, [note_id])
    except _LOOKUP_FAILURES:
        raise withhold.unavailable_error() from None
    path = paths.get(note_id)
    if path is None or scope.excludes(path=path, fileid=note_id):
        raise _note_not_found(note_id)


async def _ready(clients: NcClients) -> None:
    """Refuse before the first Notes request when the app or its API is not there."""
    caps = await capabilities.require_app(clients, APP)
    notes_client.check_api_version(caps.notes_api_versions)


def _plain_note_id(raw: str) -> str:
    """Accept ``note:12`` and a bare ``12``; refuse an id of any other kind."""
    value = (raw or "").strip()
    if value.isdigit():
        return value

    kind, parts = ids.parse(value)
    if kind != "note":
        raise ToolError(
            message=f"{raw!r} is not a note id (it is a {kind} id).",
            hint=_ID_HINT,
        )
    note_id = parts[0]
    if not note_id.isdigit():
        raise ToolError(message=f"{raw!r} has no numeric note id.", hint=_ID_HINT)
    return note_id


def _note_id_from_resource_url(resource_url: Any) -> str | None:
    """Return the numeric note id of ``.../apps/notes/note/12``, or ``None``."""
    if not isinstance(resource_url, str) or not resource_url.strip():
        return None
    path = urlsplit(resource_url.strip()).path.rstrip("/")
    if not path:
        return None
    candidate = path.rsplit("/", 1)[-1]
    return candidate if candidate.isdigit() else None
