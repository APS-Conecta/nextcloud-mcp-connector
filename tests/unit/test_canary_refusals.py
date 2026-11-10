"""The canary accepts only the refusal it claims, in a neutral environment (review WR-05).

Before the review ``ABWEISUNG`` asserted ``is_error`` alone, so ``talk_send`` refused by a
switched off channel, ``files_upload`` failing on a 5xx or ``notes_create`` without the
Notes app all passed as a correct refusal, and the canary ran with whatever
``NC_MCP_TALK_SEND`` and ``NC_MCP_FILES_ROOT`` the shell carried.
"""

import os

import canary_world as cw
import pytest

from mcp_connector import config
from mcp_connector.nextcloud.clients import dav
from mcp_connector.tools import talk as talk_tools
from mcp_connector.tools import withhold

TOKEN = "filero0m"
UPLOAD = "/kanarie28xab/Gesperrt-ab/neu-ab.txt"
SWITCHED_OFF = "Sending messages to Talk is switched off on this server."
SERVER_ERROR = "Nextcloud answered with HTTP 503."


def unknown_token() -> str:
    return talk_tools._unknown_token(TOKEN).message


def parent_missing() -> str:
    return dav.parent_missing(UPLOAD).message


@pytest.mark.parametrize(
    ("tool", "args", "text"),
    [
        ("talk_send", {"token": TOKEN}, unknown_token()),
        ("files_upload", {"path": UPLOAD}, parent_missing()),
        ("notes_create", {"category": "kat"}, parent_missing()),
        ("notes_update", {"note_id": "note:933"}, "Nextcloud did not find the note 933."),
    ],
)
def test_the_does_not_exist_sentence_is_the_refusal(
    tool: str, args: dict[str, str], text: str
) -> None:
    assert cw.refusal_problem(tool, args, "normal", text) is None


@pytest.mark.parametrize(
    ("tool", "args", "text"),
    [
        ("talk_send", {"token": TOKEN}, SWITCHED_OFF),
        ("talk_send", {"token": TOKEN}, withhold.EXCLUSION_UNAVAILABLE),
        ("files_upload", {"path": UPLOAD}, SERVER_ERROR),
        ("notes_create", {"category": "kat"}, "The Notes app is not installed."),
        ("notes_update", {"note_id": "note:933"}, "The note 933 is read-only for this account."),
    ],
)
def test_any_other_error_is_not_the_refusal_in_the_normal_state(
    tool: str, args: dict[str, str], text: str
) -> None:
    assert cw.refusal_problem(tool, args, "normal", text) is not None


def test_an_outage_accepts_the_unanswered_check_and_nothing_else() -> None:
    args = {"path": UPLOAD}
    assert (
        cw.refusal_problem("files_upload", args, "ausfall-500", withhold.EXCLUSION_UNAVAILABLE)
        is None
    )
    assert cw.refusal_problem("files_upload", args, "ausfall-500", SERVER_ERROR) is not None


def test_a_tool_without_a_defined_refusal_is_a_finding() -> None:
    assert cw.refusal_problem("files_read", {}, "normal", parent_missing()) is not None


def test_the_live_environment_neutralises_both_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_TALK_SEND, "off")
    monkeypatch.setenv(config.ENV_FILES_ROOT, "/Sandbox")
    env = cw.LiveEnv(url="http://nc.test", user="alice", app_password="x")

    cw.mcp_env(monkeypatch, env)

    assert config.ENV_TALK_SEND not in os.environ
    assert config.ENV_FILES_ROOT not in os.environ
