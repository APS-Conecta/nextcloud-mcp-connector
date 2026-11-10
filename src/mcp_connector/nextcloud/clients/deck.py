"""Deck REST v1.0 client: boards, stacks including their cards, one create and one update.

The API lives at ``/index.php/apps/deck/api/v1.0`` and it is the strictest of the JSON
APIs this project speaks. Two headers are mandatory on **every** request, a plain GET
included: ``OCS-APIRequest: true`` and ``Content-Type: application/json`` (D-18). Without
the first one Nextcloud answers a browser login page with status 200, which is why the
headers are one constant here instead of an argument anyone could forget.

Deck does **not** answer in an OCS envelope. A failure is the bare object
``{"status": 403, "message": "Permission denied"}``, so every response goes through
:func:`ocs.parse_app_json`; letting an ``ocs.meta`` parser loose on that produces a
``KeyError`` where an actionable sentence belongs (pitfall 9).

Version 1.0 and not 1.1 on purpose. 1.1 exists since Deck 1.3.0 and only adds attachment
types, which this project does not touch, so 1.0 is the wider compatibility for the same
functionality.

Three local guards keep doomed requests from ever leaving, because a refused request is
cheaper than a 400 and its message is better:

* ids that go into the URL path must be digits (threat T-01-63)
* a title is trimmed, must not be empty and must not exceed 255 characters (threat T-01-67)
* a due date must be an ISO-8601 timestamp

There is deliberately no delete and no board, stack, label or assignment write in this
module. The one update writes back the fields it read, so only the fields a caller names
change (threat T-01-62, gestion-aio#94); nothing here can remove anything.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from ...errors import ToolError
from ..credentials import Credentials
from . import ocs

#: The API generation this client speaks. 1.0 instead of 1.1 on purpose: 1.1 only adds
#: attachment types we do not use, and 1.0 is available on more instances.
SUPPORTED_API_VERSION = "1.0"

#: Base path of the Deck REST API. ``index.php`` is not optional on every instance.
DECK_API_PREFIX = f"/index.php/apps/deck/api/v{SUPPORTED_API_VERSION}"

#: Web route of a single card (``deck.page.redirectToCard``), used for the ``url`` field.
DECK_WEB_PREFIX = "/index.php/apps/deck/card"

#: Deck rejects a longer title with a 400. The limit is checked before the request.
MAX_TITLE_LENGTH = 255

#: Where a new card lands: high enough to be appended at the end of the stack.
DEFAULT_CARD_ORDER = 999

#: The only card type Deck currently knows.
CARD_TYPE = "plain"

#: The two mandatory headers of D-18, plus the ``Accept`` that keeps a proxy from
#: negotiating HTML. Copied per request, never mutated in place.
DECK_HEADERS: Mapping[str, str] = {
    "OCS-APIRequest": "true",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_DATE_HINT = "Use an ISO-8601 timestamp with an offset, for example 2026-09-01T10:00:00+00:00."


def api_url(creds: Credentials, path: str = "") -> str:
    """Build a Deck API URL; ``path`` is empty or starts with a slash."""
    if path and not path.startswith("/"):
        raise ValueError(f"a Deck path must start with a slash (got {path!r})")
    return f"{creds.base_url}{DECK_API_PREFIX}{path}"


def web_url(creds: Credentials, card_id: str | int) -> str:
    """The link a human can open. Always built from the configured base URL."""
    return f"{creds.base_url}{DECK_WEB_PREFIX}/{card_id}"


async def get_boards(
    client: httpx.AsyncClient, creds: Credentials, *, details: bool = False
) -> list[dict[str, Any]]:
    """List the boards the user may see, optionally with labels, stacks and users."""
    response = await client.get(
        api_url(creds, "/boards"),
        params={"details": "true"} if details else None,
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_list(ocs.parse_app_json(response, what="the deck boards"), what="boards")


async def get_board(
    client: httpx.AsyncClient, creds: Credentials, board_id: str | int
) -> dict[str, Any]:
    """Read one board including its labels and access list."""
    board = _path_id(board_id, "board id")
    response = await client.get(
        api_url(creds, f"/boards/{board}"),
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the board {board}"), what="a board")


async def get_stacks(
    client: httpx.AsyncClient, creds: Credentials, board_id: str | int
) -> list[dict[str, Any]]:
    """List the stacks of a board; the answer already contains their cards.

    This is the single request behind ``deck_browse(level="cards")``. Asking every stack
    for its cards separately would be an N+1 over a payload Deck already sent.
    """
    board = _path_id(board_id, "board id")
    response = await client.get(
        api_url(creds, f"/boards/{board}/stacks"),
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    payload = ocs.parse_app_json(response, what=f"the stacks of board {board}")
    return _as_list(payload, what="stacks")


async def get_card(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    card_id: str | int,
) -> dict[str, Any]:
    """Read one card by its canonical board, stack and card triple."""
    board = _path_id(board_id, "board id")
    stack = _path_id(stack_id, "stack id")
    card = _path_id(card_id, "card id")
    response = await client.get(
        api_url(creds, f"/boards/{board}/stacks/{stack}/cards/{card}"),
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the card {card}"), what="a card")


async def create_card(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    *,
    title: str,
    description: str | None = None,
    duedate: str | None = None,
    order: int = DEFAULT_CARD_ORDER,
) -> dict[str, Any]:
    """Create one card in an existing stack and return the object Deck stored."""
    board = _path_id(board_id, "board id")
    stack = _path_id(stack_id, "stack id")
    body: dict[str, Any] = {
        "title": check_title(title),
        "type": CARD_TYPE,
        "order": order,
    }
    if description:
        body["description"] = description
    if duedate:
        body["duedate"] = check_duedate(duedate)

    response = await client.post(
        api_url(creds, f"/boards/{board}/stacks/{stack}/cards"),
        json=body,
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what="the new card"), what="a card")


async def update_card(
    client: httpx.AsyncClient,
    creds: Credentials,
    board_id: str | int,
    stack_id: str | int,
    card_id: str | int,
    current: Mapping[str, Any],
    *,
    title: str | None = None,
    description: str | None = None,
    duedate: str | None = None,
) -> dict[str, Any]:
    """Write one card back with the named fields changed and every other one as it was read.

    Deck's ``PUT`` replaces the card: a field left out falls back to its default, an empty
    description and no due date. So the body carries every field of ``current``, the card
    read right before, and only the three named ones may differ. ``done`` and ``color``
    stay out of the body, which Deck reads as unchanged. The route knows no precondition,
    so this PUT is pinned as unconditional in the contract gate.
    """
    board = _path_id(board_id, "board id")
    stack = _path_id(stack_id, "stack id")
    card = _path_id(card_id, "card id")
    owner = current.get("owner")
    body: dict[str, Any] = {
        "title": check_title(title if title is not None else str(current.get("title") or "")),
        "type": str(current.get("type") or CARD_TYPE),
        "owner": str(owner.get("uid") or "") if isinstance(owner, Mapping) else str(owner or ""),
        "description": (
            description if description is not None else str(current.get("description") or "")
        ),
        "order": int(current.get("order") or 0),
        "duedate": check_duedate(duedate) if duedate else current.get("duedate"),
        "startdate": current.get("startdate"),
        "archived": bool(current.get("archived")),
    }
    response = await client.put(
        api_url(creds, f"/boards/{board}/stacks/{stack}/cards/{card}"),
        json=body,
        headers=dict(DECK_HEADERS),
        auth=creds.auth(),
    )
    return _as_dict(ocs.parse_app_json(response, what=f"the card {card}"), what="a card")


def check_title(title: str) -> str:
    """Return the trimmed title, or refuse before a request Deck would answer with 400."""
    value = (title or "").strip()
    if not value:
        raise ToolError(
            message="A card needs a title.",
            hint="Give a short title, for example 'Übergabe vorbereiten'.",
        )
    if len(value) > MAX_TITLE_LENGTH:
        raise ToolError(
            message=(
                f"A Deck card title may hold {MAX_TITLE_LENGTH} characters, "
                f"this one has {len(value)}."
            ),
            hint="Shorten the title and put the rest into the description.",
        )
    return value


def check_duedate(duedate: str) -> str:
    """Return the ISO-8601 due date, or refuse before the request."""
    value = (duedate or "").strip()
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ToolError(
            message=f"{duedate!r} is not an ISO-8601 date.",
            hint=_DATE_HINT,
        ) from None
    return value


def _path_id(value: str | int, what: str) -> str:
    """Ids are numeric in Deck; anything else is a bug or an attempt (threat T-01-63)."""
    text = str(value).strip()
    if not text.isdigit():
        raise ToolError(
            message=f"{value!r} is not a numeric {what}.",
            hint="Use an id from deck_browse; Deck addresses boards, stacks and cards by number.",
        )
    return text


def _as_list(payload: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ToolError(
            message=f"Nextcloud answered with something that is not a list of {what}.",
            hint="Check that the Deck app is enabled and up to date on that instance.",
        )
    return [item for item in payload if isinstance(item, dict)]


def _as_dict(payload: Any, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ToolError(
            message=f"Nextcloud answered with something that is not {what}.",
            hint="Check that the Deck app is enabled and up to date on that instance.",
        )
    return payload
