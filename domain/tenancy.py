from dataclasses import dataclass
from enum import StrEnum


class OrganizationRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class AccountType(StrEnum):
    """Our vocabulary, not GitHub's: the adapter maps "Organization"/"User" onto it.

    It decides which install-settings URL a user is sent to, so it has to survive
    into the session.
    """

    ORGANIZATION = "organization"
    USER = "user"


@dataclass(frozen=True, slots=True)
class User:
    """A person signed in to the console."""

    id: str
    login: str
    name: str
    avatar_url: str
    # False until staff accounts exist.
    is_platform_admin: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("user id must not be empty")
        if not self.login.strip():
            raise ValueError("user login must not be empty")


@dataclass(frozen=True, slots=True)
class Organization:
    """An account the app is installed on, with the signed-in user's role in it."""

    id: str
    login: str
    name: str
    avatar_url: str
    role: OrganizationRole
    # Internal: needed to reach the installation's repositories and to build its
    # install-settings URL. Deliberately absent from the GET /me payload.
    installation_id: int
    account_type: AccountType

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("organization id must not be empty")
        if self.installation_id <= 0:
            raise ValueError(f"installation id must be positive, got {self.installation_id}")
        if not self.login.strip():
            raise ValueError("organization login must not be empty")
