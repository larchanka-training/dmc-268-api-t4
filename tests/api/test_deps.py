from fastapi import Response

from api.config import AppSettings
from api.deps import (
    OAUTH_COOKIE,
    SESSION_COOKIE,
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
