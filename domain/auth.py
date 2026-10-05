import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from domain.tenancy import Organization, User

CODE_VERIFIER_MIN_LENGTH = 43
CODE_VERIFIER_MAX_LENGTH = 128

# A return_to may not send the browser back into the sign-in flow.
_BLOCKED_RETURN_TO_ROOTS = ("/login", "/auth")


class SignInResult(StrEnum):
    """The four outcomes the callback reports to the SPA."""

    SUCCESS = "success"
    ACCESS_DENIED = "access_denied"
    STATE_MISMATCH = "state_mismatch"
    SERVER_ERROR = "server_error"


def hash_session_id(raw_value: str) -> str:
    """Hash a cookie value for storage; the raw value is never persisted."""
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def sanitize_return_to(value: str | None) -> str | None:
    """Accept only same-origin paths, mirroring the frontend's sanitizeReturnTo.

    Returns None for anything unsafe, so the caller can fall back to a default.
    """
    if not value or not value.startswith("/"):
        return None
    if value.startswith(("//", "/\\")):
        return None
    if any(character < " " or character == "\x7f" for character in value):
        return None
    path = value.split("?", 1)[0].split("#", 1)[0]
    for root in _BLOCKED_RETURN_TO_ROOTS:
        if path == root or path.startswith(f"{root}/"):
            return None
    return value


@dataclass(frozen=True, slots=True)
class SignInAttempt:
    """One in-flight sign-in, bound to the browser by the attempt cookie."""

    attempt_id: str
    state: str
    code_verifier: str = field(repr=False)
    return_to: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        length = len(self.code_verifier)
        if not CODE_VERIFIER_MIN_LENGTH <= length <= CODE_VERIFIER_MAX_LENGTH:
            raise ValueError(
                "code_verifier length must be between "
                f"{CODE_VERIFIER_MIN_LENGTH} and {CODE_VERIFIER_MAX_LENGTH}, got {length}"
            )

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def matches(self, state: str) -> bool:
        """Constant-time-ish equality is unnecessary here: state is not a secret
        the attacker is guessing, it is a value they must already possess."""
        return bool(state) and state == self.state


@dataclass(frozen=True, slots=True)
class Session:
    """A signed-in browser session. Organisations are the snapshot taken at sign-in."""

    id_hash: str
    user: User
    organizations: tuple[Organization, ...]
    current_organization_id: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime

    @classmethod
    def issue(
        cls,
        *,
        id_hash: str,
        user: User,
        organizations: tuple[Organization, ...],
        now: datetime,
        ttl: timedelta,
    ) -> "Session":
        default = _first_by_login(organizations)
        return cls(
            id_hash=id_hash,
            user=user,
            organizations=organizations,
            current_organization_id=None if default is None else default.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + ttl,
        )

    @property
    def current_organization(self) -> Organization | None:
        """The first organisation by login, or None when the app is installed nowhere."""
        return _first_by_login(self.organizations)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def touch(self, now: datetime, ttl: timedelta) -> "Session":
        """Slide the idle timeout forward. Returns a new Session; this one is frozen."""
        return replace(self, last_seen_at=now, expires_at=now + ttl)


def _first_by_login(organizations: tuple[Organization, ...]) -> Organization | None:
    if not organizations:
        return None
    return min(organizations, key=lambda organization: organization.login)
