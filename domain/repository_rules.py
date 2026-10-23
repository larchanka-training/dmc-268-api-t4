"""What `.review/rules.md` in a connected repository holds, for the console.

The console's repository screen shows whether a branch carries a rules file and a
read-only preview of the rules a review would apply. The file is fetched through the
`RepositoryContents` port and parsed through the `RulesParser` port, the same parser
the review prompt uses, so the preview is what the model would see.

Limits and statuses follow docs/RULES_FORMAT_SPEC.md §4, §5 and §7. Rule text is
returned to the console but never logged.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import quote

from domain.auth import Session
from domain.errors import ForgePermissionError

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from domain.ports import RepositoryContents, RulesParser
    from domain.repositories import RepositoriesService, Repository

RULES_PATH = ".review/rules.md"
# docs/RULES_FORMAT_SPEC.md §4: a larger file is ignored whole.
RULES_MAX_BYTES = 64 * 1024
REF_MAX_LENGTH = 255


class RulesStatus(StrEnum):
    """`rules_status` of docs/RULES_FORMAT_SPEC.md §7, as the console sees it."""

    ABSENT = "absent"
    APPLIED = "applied"
    REJECTED_TOO_LARGE = "rejected:too_large"
    REJECTED_ENCODING = "rejected:encoding"
    # The installation cannot read contents (HTTP 403, §5): rules are ignored.
    IGNORED_FORBIDDEN = "ignored:forbidden"


@dataclass(frozen=True, slots=True)
class PreviewRule:
    id: str
    section: str
    text: str


@dataclass(frozen=True, slots=True)
class ParsedRules:
    rules: tuple[PreviewRule, ...] = ()
    dropped_suspicious: int = 0
    dropped_over_limit: int = 0


@dataclass(frozen=True, slots=True)
class RulesPreview:
    ref: str
    status: RulesStatus
    file_url: str
    parsed: ParsedRules = ParsedRules()


def validate_ref(ref: str) -> str:
    """A branch, tag or SHA as typed by the user. It travels as a query parameter,
    so only shape is checked: no control characters, no spaces, bounded length.

    Raises ValueError; the API turns that into 400 {"error": "bad_ref"}.
    """
    if not ref or len(ref) > REF_MAX_LENGTH:
        raise ValueError("ref must be 1 to 255 characters")
    if any(char.isspace() or not char.isprintable() for char in ref):
        raise ValueError("ref must not contain whitespace or control characters")
    return ref


def decode_rules_file(raw: bytes) -> str | RulesStatus:
    """The file as text, or the status that rejects it (§4: size, strict UTF-8)."""
    if len(raw) > RULES_MAX_BYTES:
        return RulesStatus.REJECTED_TOO_LARGE
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return RulesStatus.REJECTED_ENCODING


def rules_file_url(repository: "Repository", ref: str, *, exists: bool) -> str:
    """The file on github.com, or the page that creates it when it is absent.

    A github.com URL shape, like the ones in domain/repositories.py.
    """
    base = repository.html_url.rstrip("/") or (
        f"https://github.com/{quote(repository.owner)}/{quote(repository.name)}"
    )
    encoded_ref = quote(ref, safe="")
    if exists:
        return f"{base}/blob/{encoded_ref}/{RULES_PATH}"
    return f"{base}/new/{encoded_ref}?filename={quote(RULES_PATH, safe='')}"


class RepositoryRulesService:
    """Reads and parses one repository's rules file for the console."""

    def __init__(
        self,
        repositories: "RepositoriesService",
        contents: "RepositoryContents",
        parser: "RulesParser",
    ) -> None:
        self._repositories = repositories
        self._contents = contents
        self._parser = parser

    async def preview(
        self, session: Session, repository_id: str, ref: str | None = None
    ) -> RulesPreview | None:
        """The rules on `ref` (the default branch when None), or None when the
        repository is not connected to the session's organisation.

        Raises ValueError for a malformed ref, and the forge errors of the port
        other than a permission refusal, which is a status of its own.
        """
        organization = session.current_organization
        if organization is None:
            return None
        if ref is not None:
            validate_ref(ref)
        repository = await self._repositories.find(session, repository_id)
        if repository is None:
            return None
        resolved_ref = ref if ref is not None else repository.default_branch

        try:
            raw = await self._contents.read_file(
                organization.installation_id,
                repository.full_name,
                RULES_PATH,
                resolved_ref,
                max_bytes=RULES_MAX_BYTES,
            )
        except ForgePermissionError:
            return RulesPreview(
                ref=resolved_ref,
                status=RulesStatus.IGNORED_FORBIDDEN,
                file_url=rules_file_url(repository, resolved_ref, exists=True),
            )
        if raw is None:
            return RulesPreview(
                ref=resolved_ref,
                status=RulesStatus.ABSENT,
                file_url=rules_file_url(repository, resolved_ref, exists=False),
            )

        file_url = rules_file_url(repository, resolved_ref, exists=True)
        decoded = decode_rules_file(raw)
        if isinstance(decoded, RulesStatus):
            return RulesPreview(ref=resolved_ref, status=decoded, file_url=file_url)
        return RulesPreview(
            ref=resolved_ref,
            status=RulesStatus.APPLIED,
            file_url=file_url,
            parsed=self._parser.parse(decoded),
        )
