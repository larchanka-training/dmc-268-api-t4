"""What `.review/rules.md` holds at a review job's base revision, for the console.

The run screen shows whether the pull request's base branch carries a rules file
and a read-only preview of the parsed rules. The file is read at the job's
`base_sha` — the target branch the pull request merges into, so the PR author
cannot steer their own review's rules (team decision 2026-10-27, superseding
RULES_FORMAT_SPEC.md §1 [R1]; the same revision the review applies) — through
the `RepositoryContents` port, and parsed through the `RulesParser` port, the
same parser the review prompt uses.

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
        base_sha = payload.base_sha
        try:
            raw = await self._contents.read_file(
                payload.installation_id,
                payload.repo_full_name,
                RULES_PATH,
                base_sha,
                max_bytes=RULES_MAX_BYTES,
            )
        except ForgePermissionError:
            return RulesPreview(
                ref=base_sha,
                status=RulesStatus.IGNORED_FORBIDDEN,
                file_url=rules_file_url(payload.repo_full_name, base_sha),
            )
        if raw is None:
            return RulesPreview(
                ref=base_sha,
                status=RulesStatus.ABSENT,
                # A file cannot be created at a SHA, only on a branch.
                file_url=(
                    None
                    if not payload.base_ref
                    else new_rules_file_url(payload.repo_full_name, payload.base_ref)
                ),
            )

        file_url = rules_file_url(payload.repo_full_name, base_sha)
        decoded = decode_rules_file(raw)
        if isinstance(decoded, RulesStatus):
            return RulesPreview(ref=base_sha, status=decoded, file_url=file_url)
        return RulesPreview(
            ref=base_sha,
            status=RulesStatus.APPLIED,
            file_url=file_url,
            parsed=self._parser.parse(decoded),
        )
