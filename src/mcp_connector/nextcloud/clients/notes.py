"""Notes REST v1 client: read, create, a compare and swap update and the settings.

The API lives at ``/index.php/apps/notes/api/v1`` and is an ordinary app route, not an OCS
route: the answer is the bare object and a failure is ``{"status": 4xx, "message": "..."}``,
which is why every response here goes through :func:`ocs.parse_app_json` and never through
the OCS envelope parser (pitfall 9).

``OCS-APIRequest: true`` goes out anyway, although the Notes documentation only asks for
``Accept: application/json``. It costs one header and it turns the browser login page that
an unauthenticated call would otherwise receive into a plain 401, which is the difference
between an actionable message and a parser guessing at HTML.

There is deliberately no delete function, and the one update carries ``If-Match``: the
server promise is that it never removes anything and never overwrites a note it did not
read first (TOOL-09, threat T-01-41, gestion-aio#94).
"""

from typing import Any

import httpx

from ...errors import ConflictError, ToolError
from ..credentials import Credentials
from . import ocs

#: Base path of the Notes REST API. The ``index.php`` is not optional on every instance.
NOTES_API_PREFIX = "/index.php/apps/notes/api/v1"

#: Web route of a single note, used for the ``url`` field of every answer.
NOTES_WEB_PREFIX = "/index.php/apps/notes/note"

#: The API generation this client speaks. Notes has published 1.0 up to 1.4 within it.
SUPPORTED_API_GENERATION = "1"

_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}


def api_url(creds: Credentials, path: str = "") -> str:
    """Build a Notes API URL; ``path`` is empty or starts with a slash."""
    if path and not path.startswith("/"):
        raise ValueError(f"a Notes path must start with a slash (got {path!r})")
    return f"{creds.base_url}{NOTES_API_PREFIX}{path}"


def web_url(creds: Credentials, note_id: str) -> str:
    """The link a human can open. Always built from the configured base URL."""
    return f"{creds.base_url}{NOTES_WEB_PREFIX}/{_path_id(note_id)}"


def check_api_version(versions: tuple[str, ...]) -> None:
    """Fail early when the instance no longer speaks the v1 API (assumption A5).

    An empty tuple is accepted: some Notes releases report no ``api_version`` at all, and
    refusing to work over a missing capability field would be a false negative.
    """
    if not versions:
        return
    if any(version.split(".", 1)[0] == SUPPORTED_API_GENERATION for version in versions):
        return
    listed = ", ".join(versions)
    raise ToolError(
        message=f"This Nextcloud offers the Notes API versions {listed}, not version 1.",
        hint=(
            "This server speaks the Notes API v1. Update the connector, or ask an "
            "administrator for a Notes version that still offers v1."
        ),
    )


async def get_note(client: httpx.AsyncClient, creds: Credentials, note_id: str) -> dict[str, Any]:
    """Read one note including its content."""
    note_id = _path_id(note_id)
    response = await client.get(
        api_url(creds, f"/notes/{note_id}"),
        headers=dict(_HEADERS),
        auth=creds.auth(),
    )
    return _as_note(ocs.parse_app_json(response, what=f"the note {note_id}"))


async def get_settings(client: httpx.AsyncClient, creds: Credentials) -> dict[str, Any]:
    """Read the Notes settings of this user; ``notesPath`` and ``fileSuffix`` matter here.

    Measured against Notes 6.1.0 on Nextcloud 35 (raw/27-04-notes-settings.txt):
    ``{"notesPath": "Notes", "fileSuffix": ".md", ...}``, the path relative to the home.
    """
    response = await client.get(
        api_url(creds, "/settings"),
        headers=dict(_HEADERS),
        auth=creds.auth(),
    )
    payload = ocs.parse_app_json(response, what="the Notes settings")
    if not isinstance(payload, dict):
        raise ToolError(
            message="Nextcloud answered with something that is not the Notes settings.",
            hint="Check that the Notes app is enabled and up to date on that instance.",
        )
    return payload


async def create_note(
    client: httpx.AsyncClient,
    creds: Credentials,
    *,
    title: str,
    content: str,
    category: str | None = None,
) -> dict[str, Any]:
    """Create a note and return the object the server stored, titles included."""
    body: dict[str, Any] = {"title": title, "content": content}
    if category:
        body["category"] = category

    response = await client.post(
        api_url(creds, "/notes"),
        json=body,
        headers={**_HEADERS, "Content-Type": "application/json"},
        auth=creds.auth(),
    )
    return _as_note(ocs.parse_app_json(response, what="the new note"))


async def update_note(
    client: httpx.AsyncClient,
    creds: Credentials,
    note_id: str,
    *,
    content: str,
    etag: str,
) -> dict[str, Any]:
    """Replace the content of one note only while its ETag still matches.

    Notes compares ``If-Match`` against the quoted ETag (``Helper::getNoteWithETagCheck``),
    so the bare ``etag`` attribute of a note is quoted here. An empty ETag would skip the
    check altogether, so it is refused before the request.
    """
    note_id = _path_id(note_id)
    tag = (etag or "").strip().strip('"')
    if not tag:
        raise ToolError(
            message="An update needs the etag of the note.",
            hint="Read the note with notes_read and send the etag it answers.",
        )
    response = await client.put(
        api_url(creds, f"/notes/{note_id}"),
        json={"content": content},
        headers={**_HEADERS, "Content-Type": "application/json", "If-Match": f'"{tag}"'},
        auth=creds.auth(),
    )
    if response.status_code == 412:
        raise ConflictError(
            message=f"The note {note_id} changed since it was read; nothing was written.",
            hint="Read it again with notes_read and build the new text on that version.",
        )
    return _as_note(ocs.parse_app_json(response, what=f"the note {note_id}"))


def _path_id(value: str | int, what: str = "note id") -> str:
    """Ids are numeric in Notes; anything else is a bug or an attempt (T-01-63, IN-05).

    The tool layer checks this as well, but this is the one place in the client package
    where an identifier goes into a URL path, and a future second caller would not
    inherit the tool layer's check. Same guard as the Deck client keeps for its ids.
    """
    text = str(value).strip()
    if not text.isdigit():
        raise ToolError(
            message=f"{value!r} is not a numeric {what}.",
            hint="Use an id from notes_search; Notes addresses notes by number.",
        )
    return text


def _as_note(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ToolError(
            message="Nextcloud answered with something that is not a note.",
            hint="Check that the Notes app is enabled and up to date on that instance.",
        )
    return payload
