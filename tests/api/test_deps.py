import logging

from fastapi import Response

from api.config import AppSettings
from api.deps import (
    OAUTH_COOKIE,
    SESSION_COOKIE,
    CallbackQueryLogFilter,
    clear_session_cookie,
    set_oauth_cookie,
    set_session_cookie,
)
from tests.conftest import TEST_ORIGIN, make_settings


def cookie_header(response: Response, name: str) -> str:
    for raw in response.headers.getlist("set-cookie"):
        if raw.startswith(f"{name}="):
            return raw
    raise AssertionError(f"no {name} cookie was set")


# --- cookies ------------------------------------------------------------


def test_the_session_cookie_has_the_agreed_attributes() -> None:
    response = Response()

    set_session_cookie(response, "the-value", make_settings(session_ttl_seconds=3600))

    raw = cookie_header(response, SESSION_COOKIE)
    assert "__Host-session=the-value" in raw
    assert "HttpOnly" in raw
    assert "Secure" in raw
    assert "SameSite=strict" in raw.replace("SameSite=Strict", "SameSite=strict")
    assert "Path=/" in raw
    assert "Max-Age=3600" in raw
    # __Host- forbids a Domain attribute.
    assert "Domain=" not in raw


def test_the_attempt_cookie_is_lax_so_it_survives_the_redirect_back() -> None:
    response = Response()

    set_oauth_cookie(response, "attempt-1", make_settings(state_ttl_seconds=600))

    raw = cookie_header(response, OAUTH_COOKIE)
    assert "SameSite=lax" in raw.replace("SameSite=Lax", "SameSite=lax")
    assert "Max-Age=600" in raw
    assert "Secure" in raw
    assert "HttpOnly" in raw


def test_clearing_the_session_cookie_expires_it() -> None:
    response = Response()

    clear_session_cookie(response)

    assert "Max-Age=0" in cookie_header(response, SESSION_COOKIE)


# --- settings -----------------------------------------------------------


def test_settings_from_env_reads_the_documented_variables() -> None:
    settings = AppSettings.from_env(
        {
            "APP_ORIGIN": "https://console.example/",
            "SESSION_TTL_SECONDS": "60",
            "OAUTH_STATE_TTL_SECONDS": "30",
        }
    )

    assert settings.app_origin == "https://console.example"
    assert settings.session_ttl.total_seconds() == 60
    assert settings.oauth_state_ttl.total_seconds() == 30


def test_settings_default_the_two_ttls() -> None:
    settings = AppSettings.from_env({"APP_ORIGIN": TEST_ORIGIN})

    assert settings.session_ttl_seconds == 3600
    assert settings.oauth_state_ttl_seconds == 600


def test_a_missing_required_variable_names_it() -> None:
    try:
        AppSettings.from_env({})
    except Exception as error:  # noqa: BLE001 - the type is asserted below
        assert "APP_ORIGIN" in str(error)
    else:
        raise AssertionError("a missing APP_ORIGIN must stop the app")


# --- access log ---------------------------------------------------------


def make_access_record(full_path: str) -> logging.LogRecord:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", "GET", full_path, "1.1", 302),
        exc_info=None,
    )
    return record


def test_the_log_filter_drops_the_query_from_callback_lines() -> None:
    record = make_access_record("/auth/github/callback?code=secret-code&state=secret-state")

    assert CallbackQueryLogFilter().filter(record)

    assert "secret-code" not in record.getMessage()
    assert "secret-state" not in record.getMessage()
    assert "/auth/github/callback" in record.getMessage()


def test_the_log_filter_leaves_other_paths_alone() -> None:
    record = make_access_record("/runs?page=2")

    CallbackQueryLogFilter().filter(record)

    assert "page=2" in record.getMessage()
