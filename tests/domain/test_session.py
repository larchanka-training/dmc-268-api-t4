from datetime import timedelta

from domain.auth import Session, hash_session_id
from domain.tenancy import OrganizationRole
from tests.conftest import FixedClock, make_organization, make_user


def issue(clock: FixedClock, organizations: tuple[object, ...] = ()) -> Session:
    return Session.issue(
        id_hash="hash",
        user=make_user(),
        organizations=organizations,  # type: ignore[arg-type]
        now=clock.now(),
        ttl=timedelta(seconds=3600),
    )


def test_hash_session_id_never_returns_the_raw_value() -> None:
    raw = "a-cookie-value"
    hashed = hash_session_id(raw)
    assert raw not in hashed
    assert len(hashed) == 64
    assert hash_session_id(raw) == hashed


def test_is_expired_is_false_before_and_true_at_the_boundary() -> None:
    clock = FixedClock()
    session = issue(clock)

    assert not session.is_expired(clock.now())
    clock.advance(3599)
    assert not session.is_expired(clock.now())
    clock.advance(1)
    assert session.is_expired(clock.now())


def test_touch_slides_the_expiry_and_leaves_the_original_alone() -> None:
    clock = FixedClock()
    session = issue(clock)
    clock.advance(1800)

    refreshed = session.touch(clock.now(), timedelta(seconds=3600))

    assert refreshed.expires_at == clock.now() + timedelta(seconds=3600)
    assert refreshed.last_seen_at == clock.now()
    assert refreshed.created_at == session.created_at
    assert session.expires_at < refreshed.expires_at


def test_current_organization_is_none_without_organizations() -> None:
    session = issue(FixedClock())

    assert session.current_organization is None
    assert session.current_organization_id is None


def test_current_organization_is_the_first_by_login() -> None:
    zulu = make_organization("zulu", "1")
    alpha = make_organization("alpha", "2", OrganizationRole.MEMBER)
    session = issue(FixedClock(), (zulu, alpha))

    assert session.current_organization == alpha
    assert session.current_organization_id == "2"
