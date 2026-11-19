"""What `.review/rules.md` holds at a review job's pull request head, for the console.

The run screen shows whether the pull request's own branch carries a rules file and a
read-only preview of the parsed rules. The file is read at the job's head SHA, the
revision docs/RULES_FORMAT_SPEC.md §1 says reviews read rules from, through the
`RepositoryContents` port, and parsed through the `RulesParser` port, the same parser
the review prompt uses.

Limits and statuses follow docs/RULES_FORMAT_SPEC.md §4, §5 and §7. Rule text is
returned to the console but never logged.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import quote

from domain.auth import Session
from domain.errors import ForgePermissionError
from domain.models import Category

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from domain.ports import JobHistory, RepositoryContents, ReviewJob, RulesParser

RULES_PATH = ".review/rules.md"
# docs/RULES_FORMAT_SPEC.md §4: a larger file is ignored whole.
RULES_MAX_BYTES = 64 * 1024

# A github.com URL shape, like the ones in domain/repositories.py.
_REPOSITORY_URL = "https://github.com/{owner}/{name}"


class RulesStatus(StrEnum):
    """`rules_status` of docs/RULES_FORMAT_SPEC.md §7, as the console sees it."""

    ABSENT = "absent"
    APPLIED = "applied"
    REJECTED_TOO_LARGE = "rejected:too_large"
    REJECTED_ENCODING = "rejected:encoding"
    # The installation cannot read contents (HTTP 403, §5): rules are ignored.
    IGNORED_FORBIDDEN = "ignored:forbidden"


# docs/RULES_FORMAT_SPEC.md §2.2: recognised section titles, matched case-insensitively
# after trimming, and the category each one's rules are for. `Style` is the only way to
# ask for maintainability (style) comments.
SECTION_CATEGORIES: dict[str, Category] = {
    "security": Category.SECURITY,
    "correctness": Category.CORRECTNESS,
    "performance": Category.PERFORMANCE,
    "concurrency": Category.CONCURRENCY,
    "style": Category.MAINTAINABILITY,
}
IGNORE_SECTION = "ignore"


def section_category(title: str) -> Category | None:
    """The category a recognised section is for; None for "other guidance" and Ignore."""
    return SECTION_CATEGORIES.get(title.strip().casefold())


def is_ignore_section(title: str) -> bool:
    return title.strip().casefold() == IGNORE_SECTION


class IgnoreKind(StrEnum):
    """The two `# Ignore` directives of docs/RULES_FORMAT_SPEC.md §2.3."""

    PATH = "path"
    CATEGORY = "category"


class RulesDiagnosticCode(StrEnum):
    """Why the parser dropped a list item, or a note on how it reads one.

    Codes only: the console words them. A diagnostic never carries text from the file.
    """

    # A section whose title is not recognised (§2.2): its rules go to the model as
    # "other guidance", with no category implied. Not a problem, only a note.
    SECTION_OTHER = "section_other"
    # §6 layer 2: the rule, or the title of its section, looks like an attempt to steer
    # the model.
    RULE_SUSPICIOUS = "rule_suspicious"
    # §4: past 50 rules or 8000 characters of rule text in total.
    RULE_OVER_LIMIT = "rule_over_limit"
    # §2.3: an Ignore item that is neither `path: <glob>` nor `category: <name>`.
    IGNORE_INVALID = "ignore_invalid"
    IGNORE_UNKNOWN_CATEGORY = "ignore_unknown_category"
    # §3: security findings cannot be muted.
    IGNORE_SECURITY = "ignore_security"
    # §4: past 100 directives.
    IGNORE_OVER_LIMIT = "ignore_over_limit"

    @property
    def dropped(self) -> bool:
        """Whether the item it marks is left out of the review."""
        return self is not RulesDiagnosticCode.SECTION_OTHER


@dataclass(frozen=True, slots=True)
class RulesDiagnostic:
    """A note on lines `line_start..line_end` (1-based, inclusive) of the file.

    Lines are counted the way an editor counts them: `\\n`, `\\r\\n` and a lone `\\r`
    each end a line.
    """

    code: RulesDiagnosticCode
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class PreviewRule:
    id: str
    section: str
    text: str
    # The category of a recognised section (§2.2); None for "other guidance".
    category: Category | None
    # Where the list item sits in the file, 1-based and inclusive, so the console can
    # point at the rule a finding cites.
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class IgnoreDirective:
    kind: IgnoreKind
    # A gitignore-style glob for PATH, a Category value (never security) for CATEGORY.
    value: str
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class ParsedRules:
    rules: tuple[PreviewRule, ...] = ()
    dropped_suspicious: int = 0
    dropped_over_limit: int = 0
    ignore: tuple[IgnoreDirective, ...] = ()
    # In file order.
    diagnostics: tuple[RulesDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class RulesPreview:
    ref: str
    status: RulesStatus
    # The file on the forge, or the page that creates it on the pull request's branch;
    # None when the file is absent and the branch name is not known.
    file_url: str | None
    parsed: ParsedRules = ParsedRules()


def decode_rules_file(raw: bytes) -> str | RulesStatus:
    """The file as text, or the status that rejects it (§4: size, strict UTF-8)."""
    if len(raw) > RULES_MAX_BYTES:
        return RulesStatus.REJECTED_TOO_LARGE
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return RulesStatus.REJECTED_ENCODING


def rules_file_url(repo_full_name: str, ref: str) -> str:
    """The rules file at `ref` on github.com."""
    return f"{_repository_url(repo_full_name)}/blob/{quote(ref, safe='')}/{RULES_PATH}"


def new_rules_file_url(repo_full_name: str, branch: str) -> str:
    """GitHub's page that creates the rules file on `branch`.

    Slashes in the branch name stay literal, as in the links GitHub itself builds
    (`/new/feature/x`); GitHub matches the longest existing branch.
    """
    return (
        f"{_repository_url(repo_full_name)}/new/{quote(branch, safe='/')}"
        f"?filename={quote(RULES_PATH, safe='')}"
    )


def _repository_url(repo_full_name: str) -> str:
    owner, _, name = repo_full_name.partition("/")
    return _REPOSITORY_URL.format(owner=quote(owner, safe=""), name=quote(name, safe=""))


class JobRulesService:
    """Reads and parses the rules file at a review job's pull request head."""

    def __init__(
        self,
        jobs: "JobHistory",
        contents: "RepositoryContents",
        parser: "RulesParser",
    ) -> None:
        self._jobs = jobs
        self._contents = contents
        self._parser = parser

    async def preview(self, session: Session, job_id: str) -> RulesPreview | None:
        """The rules at the job's head SHA, or None when the session cannot see the job
        (missing, or on an installation of no organisation the session has).

        Raises the forge errors of the port other than a permission refusal, which is a
        status of its own.
        """
        job = await self._jobs.get(job_id)
        visible = {organization.installation_id for organization in session.organizations}
        if job is None or job.payload.installation_id not in visible:
            return None
        return await self._read(job)

    async def _read(self, job: "ReviewJob") -> RulesPreview:
        payload = job.payload
        head_sha = payload.head_sha
        try:
            raw = await self._contents.read_file(
                payload.installation_id,
                payload.repo_full_name,
                RULES_PATH,
                head_sha,
                max_bytes=RULES_MAX_BYTES,
            )
        except ForgePermissionError:
            return RulesPreview(
                ref=head_sha,
                status=RulesStatus.IGNORED_FORBIDDEN,
                file_url=rules_file_url(payload.repo_full_name, head_sha),
            )
        if raw is None:
            return RulesPreview(
                ref=head_sha,
                status=RulesStatus.ABSENT,
                # A file cannot be created at a SHA, only on a branch.
                file_url=(
                    None
                    if not payload.head_ref
                    else new_rules_file_url(payload.repo_full_name, payload.head_ref)
                ),
            )

        file_url = rules_file_url(payload.repo_full_name, head_sha)
        decoded = decode_rules_file(raw)
        if isinstance(decoded, RulesStatus):
            return RulesPreview(ref=head_sha, status=decoded, file_url=file_url)
        return RulesPreview(
            ref=head_sha,
            status=RulesStatus.APPLIED,
            file_url=file_url,
            parsed=self._parser.parse(decoded),
        )
