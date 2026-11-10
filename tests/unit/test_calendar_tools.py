"""Unit tests for the calendar tools, all paths.

The time matrix itself lives in ``test_caldav_client.py``; what is pinned here is the
contract the model sees: a mandatory window with an explicit zone, a display zone that
only changes the representation, a fan out over all calendars that stays honest when one
of them fails, and an empty window that is an empty list and not an error.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from icalendar import Calendar as IcsCalendar

from mcp_connector.errors import REASON_PERMISSION_DENIED, ConflictError, ToolError
from mcp_connector.nextcloud import NcClients
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import calendar as calendar_tools

BASE = "http://nc.test"
USER = "alice"
SECRET = "app-password-test"

CALENDAR_HOME = f"{BASE}/remote.php/dav/calendars/alice/"
PERSONAL = f"{CALENDAR_HOME}personal/"
ALPHA = f"{CALENDAR_HOME}projekt%20alpha/"
CAPABILITIES_URL = f"{BASE}/ocs/v2.php/cloud/capabilities"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

WINDOW = ("2026-09-01T00:00:00+02:00", "2026-11-01T00:00:00+01:00")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def clients() -> NcClients:
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False),
        creds=Credentials(BASE, USER, SECRET),
    )


def dav_response(body: str) -> httpx.Response:
    return httpx.Response(207, text=body, headers={"Content-Type": "application/xml"})


EMPTY = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>'


def mock_discovery(mock: respx.MockRouter) -> respx.Route:
    return mock.route(method="PROPFIND", url=CALENDAR_HOME).mock(
        return_value=dav_response(fixture("caldav_calendars_207.xml"))
    )


def mock_calendars(
    mock: respx.MockRouter,
    *,
    personal: httpx.Response | None = None,
    alpha: httpx.Response | None = None,
) -> tuple[respx.Route, respx.Route]:
    mock_discovery(mock)
    personal_route = mock.route(method="REPORT", url=PERSONAL).mock(
        return_value=personal or dav_response(fixture("caldav_report_207.xml"))
    )
    alpha_route = mock.route(method="REPORT", url=ALPHA).mock(
        return_value=alpha or dav_response(EMPTY)
    )
    return personal_route, alpha_route


@pytest.mark.anyio
async def test_all_calendars_are_queried_and_the_events_are_merged(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        personal, alpha = mock_calendars(mock)
        result = await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1])

    assert personal.call_count == 1
    assert alpha.call_count == 1, "without a calendar parameter every calendar is asked"

    assert result["count"] == 4
    assert [event["summary"] for event in result["events"]] == [
        "Projektbesprechung",
        "Standup",
        "Betriebsausflug",
        "Standup",
    ]
    assert result["range"]["start"] == WINDOW[0]
    assert result["range"]["end"] == WINDOW[1]
    assert result["range"]["timezone"] == "UTC"
    assert "degraded" not in result


@pytest.mark.anyio
async def test_the_output_times_are_absolute_and_carry_an_offset(clients: NcClients) -> None:
    """Even for a series: every instance is an absolute time, never a rule."""
    with respx.mock(assert_all_called=True) as mock:
        mock_calendars(mock)
        result = await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1])

    standups = [event for event in result["events"] if event["summary"] == "Standup"]
    assert [event["start"] for event in standups] == [
        "2026-10-19T07:00:00+00:00",
        "2026-10-26T08:00:00+00:00",
    ]
    assert all(event["all_day"] is False for event in standups)


@pytest.mark.anyio
async def test_the_timezone_parameter_only_changes_the_representation(
    clients: NcClients,
) -> None:
    """Same instants, shown in Berlin: 09:00 local on both sides of the DST boundary."""
    with respx.mock(assert_all_called=True) as mock:
        mock_calendars(mock)
        result = await calendar_tools.list_events(
            clients, start=WINDOW[0], end=WINDOW[1], timezone="Europe/Berlin"
        )

    standups = [event for event in result["events"] if event["summary"] == "Standup"]
    assert [event["start"] for event in standups] == [
        "2026-10-19T09:00:00+02:00",
        "2026-10-26T09:00:00+01:00",
    ]
    assert result["range"]["timezone"] == "Europe/Berlin"


@pytest.mark.anyio
async def test_an_all_day_event_is_a_date_without_a_time(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_calendars(mock)
        result = await calendar_tools.list_events(
            clients, start=WINDOW[0], end=WINDOW[1], timezone="Europe/Berlin"
        )

    outing = next(event for event in result["events"] if event["summary"] == "Betriebsausflug")
    assert outing["all_day"] is True
    assert outing["start"] == "2026-10-24"
    assert outing["end"] == "2026-10-25"
    assert "T" not in outing["start"], "a date must never grow a time or an offset"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-09-01T00:00:00", "2026-09-30T00:00:00+02:00"),
        ("2026-09-01T00:00:00+02:00", "2026-09-30T00:00:00"),
        ("2026-09-01", "2026-09-30"),
    ],
)
async def test_a_time_without_a_zone_is_refused_before_any_request(
    clients: NcClients, start: str, end: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route()
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.list_events(clients, start=start, end=end)

    assert everything.call_count == 0
    assert "timezone" in f"{excinfo.value.message} {excinfo.value.hint}".lower()
    assert "+02:00" in excinfo.value.hint, "the hint shows the accepted shape"


@pytest.mark.anyio
async def test_an_unparsable_time_is_refused_with_the_expected_format(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route()
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.list_events(clients, start="next monday", end=WINDOW[1])

    assert everything.call_count == 0
    assert excinfo.value.hint


@pytest.mark.anyio
async def test_an_end_before_the_start_is_refused_before_any_request(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route()
        with pytest.raises(ToolError):
            await calendar_tools.list_events(clients, start=WINDOW[1], end=WINDOW[0])

    assert everything.call_count == 0


@pytest.mark.anyio
async def test_an_unknown_timezone_name_is_refused_before_any_request(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route()
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.list_events(
                clients, start=WINDOW[0], end=WINDOW[1], timezone="Europe/Hamburg"
            )

    assert everything.call_count == 0
    assert "Europe/Berlin" in excinfo.value.hint, "the hint shows a valid IANA name"


@pytest.mark.anyio
async def test_one_broken_calendar_is_named_and_the_others_still_answer(
    clients: NcClients,
) -> None:
    """Graceful degradation, D-15: a partial answer must say that it is partial."""
    with respx.mock(assert_all_called=True) as mock:
        mock_calendars(mock, alpha=httpx.Response(503, text="service unavailable"))
        result = await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1])

    assert result["count"] == 4, "the healthy calendar still delivers"
    assert len(result["degraded"]) == 1
    assert result["degraded"][0]["calendar"] == "Projekt Alpha"
    assert "503" in result["degraded"][0]["reason"]


@pytest.mark.anyio
async def test_a_window_without_events_is_an_empty_list_and_not_an_error(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_calendars(mock, personal=dav_response(EMPTY))
        result = await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1])

    assert result["events"] == []
    assert result["count"] == 0
    assert result["range"]["start"] == WINDOW[0]
    assert "degraded" not in result


@pytest.mark.anyio
async def test_a_named_calendar_is_the_only_one_that_is_asked(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        personal, alpha = mock_calendars(mock)
        result = await calendar_tools.list_events(
            clients, start=WINDOW[0], end=WINDOW[1], calendar="Persönlich"
        )

    assert personal.call_count == 1
    assert alpha.call_count == 0
    assert {event["calendar"] for event in result["events"]} == {"Persönlich"}


@pytest.mark.anyio
async def test_the_calendar_parameter_also_accepts_the_uri(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        personal, alpha = mock_calendars(mock)
        await calendar_tools.list_events(
            clients, start=WINDOW[0], end=WINDOW[1], calendar="personal"
        )

    assert personal.call_count == 1
    assert alpha.call_count == 0


@pytest.mark.anyio
async def test_an_unknown_calendar_name_lists_the_ones_that_exist(clients: NcClients) -> None:
    """The name is validated against the discovery, never pasted into a path (T-01-51)."""
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery(mock)
        report = mock.route(method="REPORT")
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.list_events(
                clients, start=WINDOW[0], end=WINDOW[1], calendar="../contacts"
            )

    assert report.call_count == 0
    assert "Persönlich" in excinfo.value.hint


@pytest.mark.anyio
async def test_an_account_without_a_calendar_says_so_instead_of_reporting_no_events(
    clients: NcClients,
) -> None:
    """Pitfall 3 through the tool: an empty account is not an empty schedule."""
    body = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        "<d:response><d:href>/remote.php/dav/calendars/alice/</d:href>"
        "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.route(method="PROPFIND", url=CALENDAR_HOME).mock(return_value=dav_response(body))
        report = mock.route(method="REPORT")
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1])

    assert report.call_count == 0
    assert "dav:create-calendar" in excinfo.value.hint


@pytest.mark.anyio
async def test_the_calendar_tools_never_ask_for_the_optional_app_capabilities(
    clients: NcClients,
) -> None:
    """CalDAV is core dav: requiring the Calendar app would be a wrong precondition."""
    with respx.mock(assert_all_called=False) as mock:
        capabilities = mock.get(CAPABILITIES_URL)
        mock_calendars(mock)
        await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1])

    assert capabilities.call_count == 0


@pytest.mark.anyio
async def test_the_limit_caps_the_answer_and_marks_it(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_calendars(mock)
        result = await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1], limit=2)

    assert result["count"] == 2
    assert result["truncated"] is True
    assert len(result["events"]) == 2


@pytest.mark.anyio
async def test_an_impossible_limit_is_refused(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route()
        with pytest.raises(ToolError):
            await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1], limit=0)

    assert everything.call_count == 0


@pytest.mark.anyio
async def test_every_calendar_failing_is_an_error_and_not_an_empty_schedule(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_calendars(
            mock,
            personal=httpx.Response(500, text="boom"),
            alpha=httpx.Response(500, text="boom"),
        )
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.list_events(clients, start=WINDOW[0], end=WINDOW[1])

    assert "500" in f"{excinfo.value.message} {excinfo.value.hint}"


def test_the_window_is_converted_to_caldav_utc_once(clients: NcClients) -> None:
    """The parser is strict and the conversion has exactly one implementation."""
    parsed = calendar_tools.parse_instant("2026-09-01T00:00:00+02:00", field="start")
    assert parsed == datetime(2026, 8, 31, 22, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# calendar_create_event
# --------------------------------------------------------------------------------------

READBACK = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Sabre//Sabre VObject 4.5.6//EN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:20260901T101500Z
DTSTART:20260915T121500Z
DTEND:20260915T131500Z
SUMMARY:Projektbesprechung
END:VEVENT
END:VCALENDAR
"""


def mock_write(
    mock: respx.MockRouter,
    *,
    put: httpx.Response | None = None,
    readback: str | None = None,
) -> tuple[respx.Route, respx.Route]:
    mock_discovery(mock)
    put_route = mock.route(method="PUT", url__startswith=PERSONAL).mock(
        return_value=put or httpx.Response(201, headers={"ETag": '"new"'})
    )
    get_route = mock.route(method="GET", url__startswith=PERSONAL).mock(
        return_value=httpx.Response(
            200,
            text=readback if readback is not None else READBACK.format(uid="server-uid"),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
    )
    return put_route, get_route


def written_ics(route: respx.Route) -> str:
    return route.calls[0].request.content.decode("utf-8")


@pytest.mark.anyio
async def test_create_event_puts_the_ics_and_returns_the_confirmed_times(
    clients: NcClients,
) -> None:
    """The read back is the answer, not the request: silently dropped fields show up here."""
    with respx.mock(assert_all_called=True) as mock:
        put, get = mock_write(mock)
        result = await calendar_tools.create_event(
            clients,
            summary="Projektbesprechung",
            start="2026-09-15T14:00:00+02:00",
            end="2026-09-15T15:00:00+02:00",
        )

    request = put.calls[0].request
    assert request.headers["If-None-Match"] == "*"
    assert request.headers["Content-Type"] == "text/calendar; charset=utf-8"
    assert get.call_count == 1, "the created object is read back once"

    body = written_ics(put)
    for marker in ("BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:", "UID:", "DTSTAMP:", "SUMMARY:"):
        assert marker in body, f"the ICS is missing {marker}"

    assert result["created"] is True
    assert result["confirmed"] is True
    assert result["start"] == "2026-09-15T12:15:00+00:00", "the server time wins over ours"
    assert result["id"].startswith("event:personal:")
    assert result["calendar"] == "Persönlich"


@pytest.mark.anyio
async def test_the_object_name_is_a_uuid_and_carries_the_same_uid(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        put, _ = mock_write(mock)
        await calendar_tools.create_event(
            clients,
            summary="Projektbesprechung",
            start="2026-09-15T14:00:00+02:00",
            end="2026-09-15T15:00:00+02:00",
        )

    name = str(put.calls[0].request.url).rsplit("/", 1)[-1]
    assert name.endswith(".ics")
    stem = name[: -len(".ics")]
    uuid.UUID(stem)  # raises when the name is not a UUID, e.g. derived from the summary
    assert f"UID:{stem}" in written_ics(put)


@pytest.mark.anyio
async def test_a_zoned_event_carries_an_iana_vtimezone(clients: NcClients) -> None:
    """A Windows TZID is the classic interop killer; icalendar plus zoneinfo avoids it."""
    with respx.mock(assert_all_called=True) as mock:
        put, _ = mock_write(mock)
        await calendar_tools.create_event(
            clients,
            summary="Projektbesprechung",
            start="2026-09-15T14:00:00+02:00",
            end="2026-09-15T15:00:00+02:00",
            timezone="Europe/Berlin",
        )

    body = written_ics(put)
    assert "BEGIN:VTIMEZONE" in body
    assert "TZID:Europe/Berlin" in body
    assert "DTSTART;TZID=Europe/Berlin:20260915T140000" in body
    assert "W. Europe Standard Time" not in body


@pytest.mark.anyio
async def test_without_a_zone_parameter_the_times_are_written_in_utc(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        put, _ = mock_write(mock)
        await calendar_tools.create_event(
            clients,
            summary="Projektbesprechung",
            start="2026-09-15T14:00:00+02:00",
            end="2026-09-15T15:00:00+02:00",
        )

    body = written_ics(put)
    assert "DTSTART:20260915T120000Z" in body
    assert "DTEND:20260915T130000Z" in body
    assert "BEGIN:VTIMEZONE" not in body, "UTC needs no VTIMEZONE"


@pytest.mark.anyio
async def test_an_all_day_event_is_written_as_a_date(clients: NcClients) -> None:
    readback = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//y//EN\nBEGIN:VEVENT\n"
        "UID:x\nDTSTAMP:20260901T101500Z\nDTSTART;VALUE=DATE:20261024\n"
        "DTEND;VALUE=DATE:20261025\nSUMMARY:Betriebsausflug\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    with respx.mock(assert_all_called=True) as mock:
        put, _ = mock_write(mock, readback=readback)
        result = await calendar_tools.create_event(
            clients,
            summary="Betriebsausflug",
            start="2026-10-24",
            end="2026-10-25",
            all_day=True,
        )

    body = written_ics(put)
    assert "DTSTART;VALUE=DATE:20261024" in body
    assert "T000000" not in body.split("DTSTAMP")[0] + body.split("DTSTAMP:")[-1].split("\n", 1)[1]
    assert result["all_day"] is True
    assert result["start"] == "2026-10-24"


@pytest.mark.anyio
async def test_a_single_day_all_day_event_gets_the_exclusive_next_day_as_end(
    clients: NcClients,
) -> None:
    """RFC 5545 counts the end out; without the correction the event would be empty."""
    with respx.mock(assert_all_called=True) as mock:
        put, _ = mock_write(
            mock,
            readback=(
                "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//x//y//EN\nBEGIN:VEVENT\nUID:x\n"
                "DTSTAMP:20260901T101500Z\nDTSTART;VALUE=DATE:20261024\n"
                "DTEND;VALUE=DATE:20261025\nSUMMARY:Feiertag\nEND:VEVENT\nEND:VCALENDAR\n"
            ),
        )
        await calendar_tools.create_event(
            clients, summary="Feiertag", start="2026-10-24", end="2026-10-24", all_day=True
        )

    assert "DTEND;VALUE=DATE:20261025" in written_ics(put)


@pytest.mark.anyio
async def test_a_summary_with_ics_syntax_cannot_break_the_object(clients: NcClients) -> None:
    """Threat T-01-46: the ICS is generated, never concatenated, so this stays one field."""
    nasty = "Termin; mit, Sonderzeichen\nEND:VEVENT\nBEGIN:VEVENT\nSUMMARY:untergeschoben"
    with respx.mock(assert_all_called=True) as mock:
        put, _ = mock_write(mock)
        await calendar_tools.create_event(
            clients,
            summary=nasty,
            start="2026-09-15T14:00:00+02:00",
            end="2026-09-15T15:00:00+02:00",
        )

    body = written_ics(put)
    starts = [line for line in body.split("\r\n") if line == "BEGIN:VEVENT"]
    assert len(starts) == 1, "the summary must not open a second component"
    assert "\\n" in body, "line breaks are escaped, not raw"
    assert "\\;" in body, "separators are escaped, not raw"

    parsed = IcsCalendar.from_ical(body)
    events = parsed.walk("VEVENT")
    assert len(events) == 1
    assert str(events[0].get("SUMMARY")) == nasty


@pytest.mark.anyio
async def test_an_existing_object_is_reported_as_a_conflict_and_not_read_back(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _, get = mock_write(mock, put=httpx.Response(412, text="precondition failed"))
        with pytest.raises(ConflictError):
            await calendar_tools.create_event(
                clients,
                summary="Projektbesprechung",
                start="2026-09-15T14:00:00+02:00",
                end="2026-09-15T15:00:00+02:00",
            )

    assert get.call_count == 0, "nothing was created, so there is nothing to confirm"


@pytest.mark.anyio
async def test_a_calendar_without_write_permission_gets_a_permission_hint(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_write(mock, put=httpx.Response(403, text="forbidden"))
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.create_event(
                clients,
                summary="Projektbesprechung",
                start="2026-09-15T14:00:00+02:00",
                end="2026-09-15T15:00:00+02:00",
            )

    assert "permission" in f"{excinfo.value.message} {excinfo.value.hint}".lower()


@pytest.mark.anyio
async def test_a_failed_read_back_still_reports_the_event_as_created(
    clients: NcClients,
) -> None:
    """The write succeeded. Claiming otherwise would send the model creating it twice."""
    with respx.mock(assert_all_called=True) as mock:
        mock_discovery(mock)
        mock.route(method="PUT", url__startswith=PERSONAL).mock(return_value=httpx.Response(201))
        mock.route(method="GET", url__startswith=PERSONAL).mock(
            return_value=httpx.Response(503, text="service unavailable")
        )
        result = await calendar_tools.create_event(
            clients,
            summary="Projektbesprechung",
            start="2026-09-15T14:00:00+02:00",
            end="2026-09-15T15:00:00+02:00",
        )

    assert result["created"] is True
    assert result["confirmed"] is False
    assert result["start"] == "2026-09-15T12:00:00+00:00", "then our own values, marked"


@pytest.mark.anyio
async def test_creating_without_a_zone_in_the_start_is_refused(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route()
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.create_event(
                clients,
                summary="Projektbesprechung",
                start="2026-09-15T14:00:00",
                end="2026-09-15T15:00:00+02:00",
            )

    assert everything.call_count == 0
    assert "+02:00" in excinfo.value.hint


@pytest.mark.anyio
async def test_creating_with_an_end_before_the_start_is_refused(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route()
        with pytest.raises(ToolError):
            await calendar_tools.create_event(
                clients,
                summary="Projektbesprechung",
                start="2026-09-15T15:00:00+02:00",
                end="2026-09-15T14:00:00+02:00",
            )

    assert everything.call_count == 0


@pytest.mark.anyio
async def test_creating_without_a_summary_is_refused(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        everything = mock.route()
        with pytest.raises(ToolError):
            await calendar_tools.create_event(
                clients,
                summary="   ",
                start="2026-09-15T14:00:00+02:00",
                end="2026-09-15T15:00:00+02:00",
            )

    assert everything.call_count == 0


@pytest.mark.anyio
async def test_creating_in_an_unknown_calendar_lists_the_ones_that_exist(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery(mock)
        put = mock.route(method="PUT")
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.create_event(
                clients,
                summary="Projektbesprechung",
                start="2026-09-15T14:00:00+02:00",
                end="2026-09-15T15:00:00+02:00",
                calendar="Privat",
            )

    assert put.call_count == 0
    assert "Persönlich" in excinfo.value.hint


def _calendar_entry(uri: str, name: str) -> str:
    return (
        f"<d:response><d:href>/remote.php/dav/calendars/alice/{uri}/</d:href>"
        f"<d:propstat><d:prop><d:displayname>{name}</d:displayname>"
        "<d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>"
        '<cal:supported-calendar-component-set><cal:comp name="VEVENT"/>'
        "</cal:supported-calendar-component-set></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def _discovery_body(*entries: tuple[str, str]) -> str:
    inner = "".join(_calendar_entry(uri, name) for uri, name in entries)
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        f'xmlns:cal="urn:ietf:params:xml:ns:caldav">{inner}</d:multistatus>'
    )


@pytest.mark.anyio
async def test_creating_without_a_calendar_targets_personal_even_when_it_is_not_first(
    clients: NcClients,
) -> None:
    """WR-04: the default target is the 'personal' uri, never the discovery order."""
    body = _discovery_body(("geteilt", "Team (geteilt)"), ("personal", "Persönlich"))
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="PROPFIND", url=CALENDAR_HOME).mock(return_value=dav_response(body))
        put = mock.route(method="PUT", url__startswith=PERSONAL).mock(
            return_value=httpx.Response(201)
        )
        mock.route(method="GET", url__startswith=PERSONAL).mock(
            return_value=httpx.Response(
                200,
                text=READBACK.format(uid="server-uid"),
                headers={"Content-Type": "text/calendar; charset=utf-8"},
            )
        )
        result = await calendar_tools.create_event(
            clients,
            summary="Projektbesprechung",
            start="2026-09-15T14:00:00+02:00",
            end="2026-09-15T15:00:00+02:00",
        )

    assert put.call_count == 1
    assert result["calendar"] == "Persönlich", "the answer names the calendar that was chosen"


@pytest.mark.anyio
async def test_creating_without_a_calendar_and_without_personal_asks_the_caller_to_choose(
    clients: NcClients,
) -> None:
    """WR-04: with several calendars and no 'personal', silently picking one could write
    into a calendar shared into the account."""
    body = _discovery_body(("arbeit", "Arbeit"), ("geteilt", "Team (geteilt)"))
    with respx.mock(assert_all_called=False) as mock:
        mock.route(method="PROPFIND", url=CALENDAR_HOME).mock(return_value=dav_response(body))
        put = mock.route(method="PUT")
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.create_event(
                clients,
                summary="Projektbesprechung",
                start="2026-09-15T14:00:00+02:00",
                end="2026-09-15T15:00:00+02:00",
            )

    assert put.call_count == 0, "nothing is written until the target is unambiguous"
    assert "Arbeit" in excinfo.value.hint
    assert "Team (geteilt)" in excinfo.value.hint


@pytest.mark.anyio
async def test_a_single_calendar_without_personal_is_the_deterministic_default(
    clients: NcClients,
) -> None:
    body = _discovery_body(("arbeit", "Arbeit"))
    target = f"{CALENDAR_HOME}arbeit/"
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="PROPFIND", url=CALENDAR_HOME).mock(return_value=dav_response(body))
        put = mock.route(method="PUT", url__startswith=target).mock(
            return_value=httpx.Response(201)
        )
        mock.route(method="GET", url__startswith=target).mock(
            return_value=httpx.Response(
                200,
                text=READBACK.format(uid="server-uid"),
                headers={"Content-Type": "text/calendar; charset=utf-8"},
            )
        )
        result = await calendar_tools.create_event(
            clients,
            summary="Projektbesprechung",
            start="2026-09-15T14:00:00+02:00",
            end="2026-09-15T15:00:00+02:00",
        )

    assert put.call_count == 1
    assert result["calendar"] == "Arbeit"


@pytest.mark.anyio
async def test_an_ambiguous_calendar_parameter_is_refused_with_the_candidates(
    clients: NcClients,
) -> None:
    """WR-04: 'team' matches one calendar by uri and another by display name."""
    body = _discovery_body(("team", "Projekte"), ("beta", "Team"))
    with respx.mock(assert_all_called=False) as mock:
        mock.route(method="PROPFIND", url=CALENDAR_HOME).mock(return_value=dav_response(body))
        put = mock.route(method="PUT")
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.create_event(
                clients,
                summary="Projektbesprechung",
                start="2026-09-15T14:00:00+02:00",
                end="2026-09-15T15:00:00+02:00",
                calendar="team",
            )

    assert put.call_count == 0
    assert "more than one" in excinfo.value.message
    assert "Projekte (team)" in excinfo.value.hint
    assert "Team (beta)" in excinfo.value.hint


@pytest.mark.anyio
async def test_creating_without_any_calendar_names_the_occ_command(
    clients: NcClients,
) -> None:
    body = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        "<d:response><d:href>/remote.php/dav/calendars/alice/</d:href>"
        "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.route(method="PROPFIND", url=CALENDAR_HOME).mock(return_value=dav_response(body))
        put = mock.route(method="PUT")
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.create_event(
                clients,
                summary="Projektbesprechung",
                start="2026-09-15T14:00:00+02:00",
                end="2026-09-15T15:00:00+02:00",
            )

    assert put.call_count == 0
    assert "dav:create-calendar" in excinfo.value.hint


# --- calendar_update_event (APS-Conecta/gestion-aio#94) -------------------------------------

SHARED = f"{CALENDAR_HOME}personal_shared_by_bob/"
EVENT_ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:ev-1\r\nDTSTAMP:20260801T080000Z\r\n"
    "DTSTART:20260915T120000Z\r\nDTEND:20260915T130000Z\r\n"
    "SUMMARY:Teamrunde\r\nLOCATION:Raum 2\r\nDESCRIPTION:Agenda folgt\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def event_response(ics: str = EVENT_ICS, etag: str = '"e-1"') -> httpx.Response:
    return httpx.Response(200, text=ics, headers={"ETag": etag, "Content-Type": "text/calendar"})


def home_with_shared(*, read_only: bool) -> httpx.Response:
    """The calendar home: personal, plus a calendar bob shares into it."""
    flag = "1" if read_only else "0"
    shared = (
        f"<d:response><d:href>{SHARED.removeprefix(BASE)}</d:href><d:propstat><d:prop>"
        "<d:displayname>Team</d:displayname><d:resourcetype><d:collection/><cal:calendar/>"
        '</d:resourcetype><cal:supported-calendar-component-set><cal:comp name="VEVENT"/>'
        f"</cal:supported-calendar-component-set><oc:read-only>{flag}</oc:read-only></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )
    body = fixture("caldav_calendars_207.xml").replace(
        "</d:multistatus>", f"{shared}</d:multistatus>"
    )
    return dav_response(body)


def mock_home(mock: respx.MockRouter, *, read_only: bool = True) -> None:
    mock.route(method="PROPFIND", url=CALENDAR_HOME).mock(
        return_value=home_with_shared(read_only=read_only)
    )


@pytest.mark.anyio
async def test_update_event_in_a_calendar_the_user_cannot_edit_is_refused(
    clients: NcClients,
) -> None:
    """A calendar shared read-only is refused before any request touches the event."""
    with respx.mock(assert_all_called=False) as mock:
        mock_home(mock)
        event = mock.route(url__startswith=SHARED)
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.update_event(
                clients, event_id="event:personal_shared_by_bob:ev-1.ics", summary="Neu"
            )

    assert event.call_count == 0
    assert excinfo.value.reason == REASON_PERMISSION_DENIED
    assert excinfo.value.message == "The calendar Team is read-only for this account."


@pytest.mark.anyio
async def test_update_event_reports_a_write_sabre_refuses(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_home(mock, read_only=False)
        mock.get(f"{SHARED}ev-1.ics").mock(return_value=event_response())
        mock.put(f"{SHARED}ev-1.ics").mock(return_value=httpx.Response(403))
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.update_event(
                clients, event_id="event:personal_shared_by_bob:ev-1.ics", summary="Neu"
            )

    assert excinfo.value.reason == REASON_PERMISSION_DENIED


@pytest.mark.anyio
async def test_update_event_changes_only_the_named_field_under_the_etag(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_home(mock, read_only=False)
        mock.get(f"{PERSONAL}ev-1.ics").mock(
            side_effect=[event_response(), event_response(EVENT_ICS.replace("Teamrunde", "Neu"))]
        )
        put = mock.put(f"{PERSONAL}ev-1.ics").mock(return_value=httpx.Response(204))
        result = await calendar_tools.update_event(
            clients, event_id="event:personal:ev-1.ics", summary=" Neu "
        )

    request = put.calls[0].request
    assert request.headers["If-Match"] == '"e-1"'
    written = IcsCalendar.from_ical(request.content).walk("VEVENT")[0]
    assert str(written["SUMMARY"]) == "Neu"
    assert str(written["LOCATION"]) == "Raum 2"
    assert str(written["DESCRIPTION"]) == "Agenda folgt"
    assert str(written["UID"]) == "ev-1"
    assert written.decoded("DTSTART") == datetime(2026, 9, 15, 12, tzinfo=UTC)
    assert written["SEQUENCE"] == 1
    assert result == {
        "id": "event:personal:ev-1.ics",
        "uid": "ev-1",
        "summary": "Neu",
        "start": "2026-09-15T12:00:00+00:00",
        "end": "2026-09-15T13:00:00+00:00",
        "all_day": False,
        "calendar": "Persönlich",
        "location": "Raum 2",
        "updated": True,
        "confirmed": True,
    }


@pytest.mark.anyio
async def test_update_event_moves_the_times_in_the_zone_the_event_is_stored_in(
    clients: NcClients,
) -> None:
    berlin = EVENT_ICS.replace(
        "DTSTART:20260915T120000Z\r\nDTEND:20260915T130000Z",
        "DTSTART;TZID=Europe/Berlin:20260915T140000\r\nDURATION:PT1H",
    )
    with respx.mock(assert_all_called=False) as mock:
        mock_home(mock, read_only=False)
        mock.get(f"{PERSONAL}ev-1.ics").mock(
            side_effect=[event_response(berlin), httpx.Response(500)]
        )
        put = mock.put(f"{PERSONAL}ev-1.ics").mock(return_value=httpx.Response(204))
        result = await calendar_tools.update_event(
            clients,
            event_id="event:personal:ev-1.ics",
            start="2026-09-16T08:00:00Z",
            end="2026-09-16T09:30:00Z",
        )

    written = IcsCalendar.from_ical(put.calls[0].request.content).walk("VEVENT")[0]
    assert "DURATION" not in written
    assert written["DTSTART"].params["TZID"] == "Europe/Berlin"
    assert written.decoded("DTSTART").isoformat() == "2026-09-16T10:00:00+02:00"
    assert written.decoded("DTEND").isoformat() == "2026-09-16T11:30:00+02:00"
    assert result["start"] == "2026-09-16T10:00:00+02:00"
    assert result["confirmed"] is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fields", "ics"),
    [
        ({"summary": "Neu"}, EVENT_ICS.replace("SUMMARY:", "RRULE:FREQ=WEEKLY\r\nSUMMARY:")),
        ({"start": "2026-09-16T08:00:00Z"}, EVENT_ICS),
        ({"summary": "  "}, EVENT_ICS),
    ],
)
async def test_update_event_refuses_a_series_a_lone_start_or_nothing_without_writing(
    clients: NcClients, fields: dict[str, str], ics: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_home(mock, read_only=False)
        mock.get(f"{PERSONAL}ev-1.ics").mock(return_value=event_response(ics))
        put = mock.put(f"{PERSONAL}ev-1.ics")
        with pytest.raises(ToolError):
            await calendar_tools.update_event(clients, event_id="event:personal:ev-1.ics", **fields)

    assert put.call_count == 0


@pytest.mark.anyio
async def test_update_event_that_changed_in_between_is_a_conflict(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock_home(mock, read_only=False)
        mock.get(f"{PERSONAL}ev-1.ics").mock(return_value=event_response())
        mock.put(f"{PERSONAL}ev-1.ics").mock(return_value=httpx.Response(412))
        with pytest.raises(ConflictError):
            await calendar_tools.update_event(
                clients, event_id="event:personal:ev-1.ics", location="Raum 3"
            )


@pytest.mark.anyio
async def test_create_event_in_a_calendar_shared_read_only_is_refused(clients: NcClients) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_home(mock)
        put = mock.route(method="PUT", url__startswith=SHARED)
        with pytest.raises(ToolError) as excinfo:
            await calendar_tools.create_event(
                clients,
                summary="Neu",
                start="2026-09-15T14:00:00+02:00",
                end="2026-09-15T15:00:00+02:00",
                calendar="personal_shared_by_bob",
            )

    assert put.call_count == 0
    assert excinfo.value.reason == REASON_PERMISSION_DENIED
