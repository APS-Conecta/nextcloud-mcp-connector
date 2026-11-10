"""CalDAV client: discovery, a server expanded calendar-query, a create-only and an If-Match PUT.

Three decisions carry this module, and all three exist because CalDAV time handling is
where the competing servers collect their bug reports.

**The server expands recurrences, not this process.** The query asks for an expansion
window, and sabre answers with absolute single instances, using the collection's
``calendar-timezone`` property and defaulting to UTC. Nothing here interprets a recurrence
rule, walks exception dates or reasons about a daylight saving change; the one place that
knows all the rules of a series is the server that stores it (D-17).

**Every request body is built with lxml.** A calendar URI or a date that ends up inside an
attribute cannot close a tag this way, and there is no XML literal in this file at all
(threat T-01-45).

**A date is not a midnight.** ``DTSTART;VALUE=DATE`` has no time and no zone. It leaves
this module as a :class:`datetime.date`, flagged with ``all_day``. Turning it into midnight
in some zone is exactly how an all day event moves to the previous day for half the world.

The path is ``calendars/<uid>/<calendarUri>/``, without a ``users/`` segment: the CalDAV
root collection is named ``calendars`` while the principal lives under ``principals/users``.
Address books use a different shape, which is why plan 08 has its own module.

Two shapes deliberately stay as the server reports them. The end of an all day event is the
exclusive end date of RFC 5545, so a single day event on the 24th ends on the 25th. And all
expanded instances of one series share one object name, because they are one object on the
server; the instances differ in their times, not in their address.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from icalendar import Calendar as IcsCalendar
from lxml import etree

from ... import config, ids
from ...errors import (
    REASON_PERMISSION_DENIED,
    REASON_UNKNOWN_ID,
    ConflictError,
    ToolError,
)
from ..credentials import Credentials
from . import xml

#: Everything below the CalDAV root of one user.
DAV_CALENDARS_PREFIX = "/remote.php/dav/calendars/"

#: iCalendar UTC form. Not ISO 8601: ``time-range`` and the expansion window reject that.
CALDAV_UTC_FORMAT = "%Y%m%dT%H%M%SZ"

_URI_HINT = (
    "Use a calendar exactly as calendar_list_events reports it, for example 'personal'. "
    "A calendar name is one path segment, never a path."
)

_NO_CALENDAR = (
    "No calendar found for this account.",
    (
        "Open the Calendar app once in the Nextcloud web interface, or ask an administrator "
        "to run 'occ dav:create-calendar <user> personal'."
    ),
)

_COMPONENT_EVENT = "VEVENT"


@dataclass(frozen=True, slots=True)
class CalendarRef:
    """One event calendar of the user: its URI on the wire and its name for humans.

    ``read_only`` is ``oc:read-only`` of a calendar shared into the account without write
    permission. The writes refuse such a calendar before they send anything (gestion-aio#94),
    so the refusal never depends on how the server answers a write.
    """

    uri: str
    display_name: str
    read_only: bool = False


def to_caldav_utc(value: datetime) -> str:
    """Format an aware datetime as ``YYYYMMDDTHHMMSSZ``.

    ``ValueError`` on a naive datetime, on purpose and not as a :class:`ToolError`: a
    missing zone at this depth is a programming error, and the tool layer rejects user
    input without a zone long before it gets here (pitfall 4).
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("a timezone aware datetime is required for a CalDAV time value")
    return value.astimezone(UTC).strftime(CALDAV_UTC_FORMAT)


def calendars_home_url(creds: Credentials) -> str:
    """The CalDAV home collection of the authenticated user."""
    return f"{creds.base_url}{DAV_CALENDARS_PREFIX}{quote(creds.user, safe='')}/"


def calendar_url(creds: Credentials, calendar_uri: str) -> str:
    """The URL of one calendar collection, with the URI checked and quoted."""
    return f"{calendars_home_url(creds)}{quote(safe_segment(calendar_uri, 'calendar'), safe='')}/"


def object_url(creds: Credentials, calendar_uri: str, object_name: str) -> str:
    """The URL of one calendar object inside a calendar."""
    name = safe_segment(object_name, "calendar object")
    return f"{calendar_url(creds, calendar_uri)}{quote(name, safe='')}"


def safe_segment(value: str, what: str) -> str:
    """Return a single path segment or raise (threat T-01-51, path traversal).

    Runs before the URL is built, so a name with a separator never becomes a path.
    """
    raw = (value or "").strip()
    if not raw:
        raise ToolError(message=f"No {what} was given.", hint=_URI_HINT)
    if raw in (".", ".."):
        raise ToolError(message=f"{raw!r} is not a {what} name.", hint=_URI_HINT)
    if "/" in raw or "\\" in raw:
        raise ToolError(
            message=f"The {what} name {raw!r} contains a path separator.",
            hint=_URI_HINT,
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ToolError(
            message=f"The {what} name contains a control character.",
            hint=_URI_HINT,
        )
    return raw


def build_calendar_query(start: datetime, end: datetime) -> bytes:
    """Build the calendar-query body that makes sabre expand the series for us.

    Both window attributes are mandatory and ``end`` must be greater than ``start``,
    otherwise sabre answers 400 BadRequest. That check happens here, so the failure costs
    no round trip and reaches the caller as a sentence instead of a DAV error body.
    """
    if end <= start:
        raise ToolError(
            message="The end of the time range must be after its start.",
            hint="Give a window such as start 2026-09-01T00:00:00+02:00 and end one day later.",
        )
    window = {"start": to_caldav_utc(start), "end": to_caldav_utc(end)}

    root = etree.Element(f"{{{xml.CAL}}}calendar-query", nsmap={"d": xml.DAV, "c": xml.CAL})
    prop = etree.SubElement(root, f"{{{xml.DAV}}}prop")
    etree.SubElement(prop, f"{{{xml.DAV}}}getetag")
    calendar_data = etree.SubElement(prop, f"{{{xml.CAL}}}calendar-data")
    etree.SubElement(calendar_data, f"{{{xml.CAL}}}expand", attrib=window)

    filter_element = etree.SubElement(root, f"{{{xml.CAL}}}filter")
    vcalendar = etree.SubElement(filter_element, f"{{{xml.CAL}}}comp-filter", name="VCALENDAR")
    vevent = etree.SubElement(vcalendar, f"{{{xml.CAL}}}comp-filter", name=_COMPONENT_EVENT)
    etree.SubElement(vevent, f"{{{xml.CAL}}}time-range", attrib=window)

    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def build_discovery_body() -> bytes:
    """Build the PROPFIND body of the calendar discovery."""
    root = etree.Element(
        f"{{{xml.DAV}}}propfind",
        nsmap={"d": xml.DAV, "c": xml.CAL, "cs": xml.CS, "oc": xml.OC},
    )
    prop = etree.SubElement(root, f"{{{xml.DAV}}}prop")
    etree.SubElement(prop, f"{{{xml.DAV}}}displayname")
    etree.SubElement(prop, f"{{{xml.DAV}}}resourcetype")
    etree.SubElement(prop, f"{{{xml.CS}}}getctag")
    etree.SubElement(prop, f"{{{xml.CAL}}}supported-calendar-component-set")
    etree.SubElement(prop, f"{{{xml.OC}}}read-only")
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


async def discover_calendars(client: httpx.AsyncClient, creds: Credentials) -> list[CalendarRef]:
    """List the user's own event calendars, newest sabre semantics, one request.

    An account without a single event calendar is an error with a way out, not an empty
    list: ``occ user:add`` does not fire the event that creates the default calendar, so
    "no calendar" and "no appointments" look identical from the outside (pitfall 3).
    """
    home = calendars_home_url(creds)
    response = await client.request(
        "PROPFIND",
        home,
        headers={"Depth": "1", "Content-Type": "application/xml"},
        content=build_discovery_body(),
        auth=creds.auth(),
    )
    _check(response, "the calendar list")

    calendars = parse_calendar_home(response.content, home_path=urlsplit(home).path)
    if not calendars:
        message, hint = _NO_CALENDAR
        raise ToolError(message=message, hint=hint)
    return calendars


def parse_calendar_home(body: str | bytes, home_path: str = "") -> list[CalendarRef]:
    """Read a discovery Multi-Status and keep only the user's own event calendars.

    Dropped: the home collection itself, the scheduling inbox and outbox, subscriptions
    (a subscription is a foreign feed, and writing to one is not a thing this server does)
    and every collection whose component set has no ``VEVENT``.
    """
    root = xml.parse_root(body)
    if root.tag != f"{{{xml.DAV}}}multistatus":
        raise ToolError(
            message="Expected a DAV Multi-Status response for the calendar list.",
            hint="Check that the base URL points at Nextcloud itself and not at a login page.",
        )

    home = (home_path or "").rstrip("/")
    found: list[CalendarRef] = []
    for response in root.findall(f"{{{xml.DAV}}}response"):
        href_element = response.find(f"{{{xml.DAV}}}href")
        href = (href_element.text or "").strip() if href_element is not None else ""
        path = urlsplit(href).path.rstrip("/")
        if not path or (home and path == home):
            continue

        props = _ok_props(response)
        if props is None:
            continue
        if not _is_own_event_calendar(props):
            continue

        uri = unquote(path.rsplit("/", 1)[-1])
        if not uri:
            continue
        name_element = props.find(f"{{{xml.DAV}}}displayname")
        display_name = (name_element.text or "").strip() if name_element is not None else ""
        flag = props.find(f"{{{xml.OC}}}read-only")
        read_only = flag is not None and (flag.text or "").strip().lower() in ("1", "true")
        found.append(CalendarRef(uri=uri, display_name=display_name or uri, read_only=read_only))
    return found


def _ok_props(response: etree._Element) -> etree._Element | None:
    """Return the ``d:prop`` of the 2xx propstat, or ``None`` if there is none."""
    for propstat in response.findall(f"{{{xml.DAV}}}propstat"):
        status = propstat.find(f"{{{xml.DAV}}}status")
        text = (status.text or "") if status is not None else ""
        if text and " 2" not in text:
            continue
        prop = propstat.find(f"{{{xml.DAV}}}prop")
        if prop is not None and len(prop):
            return prop
    return None


def _is_own_event_calendar(props: etree._Element) -> bool:
    resourcetype = props.find(f"{{{xml.DAV}}}resourcetype")
    types = (
        {str(child.tag) for child in resourcetype if isinstance(child.tag, str)}
        if resourcetype is not None
        else set()
    )
    excluded = {
        f"{{{xml.CS}}}subscribed",
        f"{{{xml.CAL}}}schedule-inbox",
        f"{{{xml.CAL}}}schedule-outbox",
    }
    if types & excluded:
        return False

    component_set = props.find(f"{{{xml.CAL}}}supported-calendar-component-set")
    if component_set is None:
        return False
    names = {
        (child.get("name") or "").upper() for child in component_set if isinstance(child.tag, str)
    }
    return _COMPONENT_EVENT in names


async def query_events(
    client: httpx.AsyncClient,
    creds: Credentials,
    calendar_uri: str,
    start: datetime,
    end: datetime,
    calendar: str | None = None,
) -> list[dict[str, Any]]:
    """Read every event instance of one calendar inside ``[start, end)``.

    ``time-range`` is half open on the server side, and this function does not adjust the
    boundary in either direction: an event that begins exactly at ``end`` is outside the
    window, and pretending otherwise would make two callers with the same window see
    different results.
    """
    body = build_calendar_query(start, end)
    url = calendar_url(creds, calendar_uri)
    response = await client.request(
        "REPORT",
        url,
        headers={"Depth": "1", "Content-Type": "application/xml"},
        content=body,
        auth=creds.auth(),
    )
    _check(response, f"the calendar {calendar_uri}")

    events: list[dict[str, Any]] = []
    for href, props in xml.parse_multistatus(response.content):
        ics = props.get(f"{{{xml.CAL}}}calendar-data")
        if not ics:
            continue
        object_name = unquote(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1])
        if not object_name:
            continue
        events.extend(
            parse_ics(
                ics,
                calendar_uri=calendar_uri,
                object_name=object_name,
                calendar=calendar,
            )
        )
    return events


async def get_event(
    client: httpx.AsyncClient,
    creds: Credentials,
    calendar_uri: str,
    object_name: str,
    calendar: str | None = None,
) -> list[dict[str, Any]]:
    """GET one calendar object and parse it. The read back proof after a write."""
    text, _etag = await get_object(client, creds, calendar_uri, object_name)
    return parse_ics(
        text,
        calendar_uri=calendar_uri,
        object_name=object_name,
        calendar=calendar,
    )


async def get_object(
    client: httpx.AsyncClient, creds: Credentials, calendar_uri: str, object_name: str
) -> tuple[str, str]:
    """GET one calendar object as text with its ETag, the precondition of a replace."""
    response = await client.get(
        object_url(creds, calendar_uri, object_name),
        headers={"Accept": "text/calendar"},
        auth=creds.auth(),
    )
    _check(response, f"the event {object_name}")
    return response.text, response.headers.get("etag", "")


async def put_event(
    client: httpx.AsyncClient,
    creds: Credentials,
    calendar_uri: str,
    object_name: str,
    ics_bytes: bytes,
) -> dict[str, Any]:
    """Create one calendar object that must not exist yet.

    ``If-None-Match: *`` is the whole overwrite protection and it is evaluated by sabre
    inside the same request, so there is no probe before it and no race after it
    (threat T-01-48).
    """
    response = await client.put(
        object_url(creds, calendar_uri, object_name),
        content=ics_bytes,
        headers={"If-None-Match": "*", "Content-Type": "text/calendar; charset=utf-8"},
        auth=creds.auth(),
    )
    _check_write(response, calendar_uri, object_name)
    return {
        "calendar": calendar_uri,
        "object_name": object_name,
        "etag": response.headers.get("etag", ""),
        "created": True,
    }


async def replace_event(
    client: httpx.AsyncClient,
    creds: Credentials,
    calendar_uri: str,
    object_name: str,
    ics_bytes: bytes,
    etag: str,
) -> None:
    """Replace one calendar object only while its ETag still matches (gestion-aio#94).

    ``If-Match`` is evaluated by sabre inside the same request, like ``If-None-Match: *`` of
    :func:`put_event`: an object that changed since it was read is answered with 412 and left
    alone. An empty ETag would make the write unconditional, so it never leaves.
    """
    if not etag.strip():
        raise ToolError(
            message=f"Nextcloud reported no ETag for the event {object_name}; nothing changed.",
            hint="Retry once; this server only replaces an event it has just read.",
        )
    response = await client.put(
        object_url(creds, calendar_uri, object_name),
        content=ics_bytes,
        headers={"If-Match": etag, "Content-Type": "text/calendar; charset=utf-8"},
        auth=creds.auth(),
    )
    status = response.status_code
    if status in (200, 204):
        return
    if status == 412:
        raise ConflictError(
            message=f"The event {object_name} changed while it was updated; nothing was written.",
            hint="Call the update again; it reads the event anew.",
        )
    if status == 403:
        raise ToolError(
            message=f"No permission to change events in the calendar {calendar_uri}.",
            hint="Ask the owner of that calendar for write permission in Nextcloud.",
            reason=REASON_PERMISSION_DENIED,
        )
    _check(response, f"the event {object_name}")
    raise ToolError(
        message=f"Nextcloud answered the update with an unexpected status {status}.",
        hint="Check the Nextcloud log for that request; the event was probably not changed.",
    )


def parse_ics(
    text: str | bytes,
    *,
    calendar_uri: str,
    object_name: str,
    calendar: str | None = None,
) -> list[dict[str, Any]]:
    """Turn one calendar object into the stable event shape of this connector.

    Several events come back for an expanded series, because the server returns one
    instance per occurrence inside the requested window. They share the object name, and
    therefore the id, since they share the object.
    """
    try:
        parsed = IcsCalendar.from_ical(text)
    except ValueError as exc:
        raise ToolError(
            message=f"Nextcloud returned a calendar object that could not be read ({exc}).",
            hint="Open that event in the Nextcloud Calendar app; it may be damaged.",
        ) from None

    events: list[dict[str, Any]] = []
    for component in parsed.walk("VEVENT"):
        start_value = _value_of(component, "DTSTART")
        if start_value is None:
            # An event without a start has no place on a timeline. Skipping one damaged
            # entry beats failing the whole window.
            continue
        end_value = _end_of(component, start_value)
        all_day = not isinstance(start_value, datetime)

        events.append(
            {
                "id": ids.encode_event(calendar_uri, object_name),
                "uid": _text_of(component, "UID"),
                "summary": _text_of(component, "SUMMARY"),
                "start": start_value,
                "end": end_value,
                "all_day": all_day,
                "location": _text_of(component, "LOCATION"),
                "calendar": calendar or calendar_uri,
            }
        )
    return events


def _value_of(component: Any, name: str) -> datetime | date | None:
    """Read a date-time property as UTC, or a pure date for an all day event."""
    raw = component.get(name)
    value = getattr(raw, "dt", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # A floating time only appears when the server did not expand, which it does
            # for every query this client sends. UTC matches the expansion default.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, date):
        return value
    return None


def _end_of(component: Any, start_value: datetime | date) -> datetime | date:
    """DTEND if present, otherwise DURATION, otherwise the RFC 5545 default."""
    end_value = _value_of(component, "DTEND")
    if end_value is not None:
        return end_value

    duration = getattr(component.get("DURATION"), "dt", None)
    if duration is not None:
        try:
            return start_value + duration
        except TypeError:
            return start_value
    return start_value


def _text_of(component: Any, name: str) -> str:
    value = component.get(name)
    return str(value) if value is not None else ""


def _check_write(response: httpx.Response, calendar_uri: str, object_name: str) -> None:
    """Translate the answer to a create-only PUT. 412 is the expected refusal, not a bug."""
    status = response.status_code
    if status == 201:
        return
    if status in (200, 204):
        raise ToolError(
            message=(
                f"Nextcloud reports that the write replaced an existing event "
                f"{object_name} (status {status})."
            ),
            hint=(
                "This server sends If-None-Match: * and expects a refusal instead. Report "
                "this instance: it does not honour the precondition."
            ),
        )
    if status == 412:
        raise ConflictError(
            message=f"An event object named {object_name} already exists.",
            hint="This server never overwrites events. Create the event under a new name.",
        )
    if status == 403:
        raise ToolError(
            message=f"No permission to write to the calendar {calendar_uri}.",
            hint="Pick one of your own calendars, or ask its owner for write permission.",
            # Why the identifier sits at the status branches only: see the docstring of
            # ``_status_error`` in ``ocs.py`` (D-17).
            reason=REASON_PERMISSION_DENIED,
        )
    if status in (404, 409):
        raise ToolError(
            message=f"The calendar {calendar_uri} does not exist.",
            hint="Call calendar_list_events once to see the calendars of this account.",
            reason=REASON_UNKNOWN_ID,
        )
    if status == 415:
        raise ToolError(
            message="Nextcloud rejected the event as an unsupported media type.",
            hint="This is a bug in this connector; please report it with the event data.",
        )
    _check(response, f"the calendar {calendar_uri}")
    raise ToolError(
        message=f"Nextcloud answered the new event with an unexpected status {status}.",
        hint="Check the Nextcloud log for that request; the event was probably not created.",
    )


def _check(response: httpx.Response, what: str) -> None:
    """Translate a Nextcloud status into message plus hint. No retry, ever (pitfall 8)."""
    status = response.status_code
    if status in (200, 207):
        return
    if 300 <= status < 400:
        raise ToolError(
            message=f"Nextcloud answered the request for {what} with a redirect ({status}).",
            hint=config.REDIRECT_HINT,
        )
    if status == 401:
        raise ToolError(
            message="Nextcloud rejected the app password.",
            hint=(
                "Generate a new app password in Nextcloud under Settings, Security, "
                "Devices and sessions, then restart the MCP server."
            ),
        )
    if status == 403:
        raise ToolError(
            message=f"No permission to read {what}.",
            hint="Ask the owner of that calendar for read permission in Nextcloud.",
            reason=REASON_PERMISSION_DENIED,
        )
    if status == 404:
        raise ToolError(
            message=f"Nextcloud does not know {what}.",
            hint="Call calendar_list_events once to see the calendars of this account.",
            reason=REASON_UNKNOWN_ID,
        )
    if status == 429:
        raise ToolError(
            message="Nextcloud is rate limiting this server.",
            hint="Wait about a minute before the next call; do not repeat it immediately.",
        )
    if status >= 500:
        raise ToolError(
            message=f"Nextcloud reported a server error ({status}) for {what}.",
            hint="This is a problem on the Nextcloud side. Retry later or check its log.",
        )
    raise ToolError(
        message=f"Nextcloud answered with an unexpected status {status} for {what}.",
        hint="Retry once; if it persists, check the Nextcloud log for that request.",
    )
