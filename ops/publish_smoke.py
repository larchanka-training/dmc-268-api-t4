"""Manual smoke test of the review publisher against a real pull request.

Posts one sample finding on the first added line of a scratch pull request, plus the
check run with the summary. Never run in CI or tests: it writes to GitHub.

    set -a; . ./.env; set +a
    python -m ops.publish_smoke --installation-id 123 --repo you/test-repo --pr 1

Use a test repository you own. Prerequisites outside this repository:

- the GitHub App has `Pull requests: Read and write` and `Checks: Read and write`, and
  the installation on the test repository has accepted them;
- `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_APP_SLUG` and
  `GITHUB_WEBHOOK_SECRET` are set.

The per-file patches are read here with two plain calls rather than through
`GitProvider`, which returns one unified diff; in the real pipeline the worker's
FETCH_DIFF step provides the diff and the findings come from the LLM.
"""

import argparse
import asyncio
import sys

import httpx

from adapters.clock import SystemClock
from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import GitHubAppSettings
from adapters.github.publisher import GitHubReviewPublisher
from domain.models import Category, DiffSide, Finding, FindingPosition, Severity
from domain.ports import PullRequestTarget
from domain.publishing import CommentableLines, PublishPolicy, build_plan


async def main(installation_id: int, repo: str, number: int) -> int:
    owner, name = repo.split("/", 1)
    settings = GitHubAppSettings.from_env()
    auth = GitHubAppAuth(settings, SystemClock())
    token = await auth.installation_token(installation_id)
    async with httpx.AsyncClient(
        base_url=settings.api_base_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=settings.timeout_seconds,
    ) as client:
        pull = (await client.get(f"/repos/{owner}/{name}/pulls/{number}")).raise_for_status().json()
        files = (
            (
                await client.get(
                    f"/repos/{owner}/{name}/pulls/{number}/files", params={"per_page": 100}
                )
            )
            .raise_for_status()
            .json()
        )

    patches = {item["filename"]: item["patch"] for item in files if item.get("patch")}
    lines = CommentableLines.from_patches(patches)
    added = sorted(
        (path, line)
        for path, positions in lines.lines.items()
        for side, line in positions
        if side is DiffSide.NEW
    )
    if not added:
        print("the pull request has no commentable new-side line", file=sys.stderr)
        return 1
    path, line = added[0]
    finding = Finding(
        position=FindingPosition(path=path, line=line, side=DiffSide.NEW),
        severity=Severity.MEDIUM,
        category=Category.MAINTAINABILITY,
        message="Publisher smoke test: this comment was posted by ops/publish_smoke.py.",
        suggestion=None,
    )
    plan = build_plan(
        [finding],
        model_summary="Smoke test of the review publisher; no model was called.",
        lines=lines,
        policy=PublishPolicy(min_severity=Severity.LOW),
    )
    target = PullRequestTarget(
        installation_id=installation_id,
        owner=owner,
        repo=name,
        number=number,
        head_sha=pull["head"]["sha"],
    )
    outcome = await GitHubReviewPublisher(settings, auth).publish(target, plan)
    print(f"check run: {outcome.check_run_url}")
    print(f"review:    {outcome.review_url}")
    for comment in outcome.comments:
        print(f"comment:   {comment.url} (id {comment.external_comment_id})")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--installation-id", type=int, required=True)
    parser.add_argument("--repo", required=True, help="owner/name of a scratch repository")
    parser.add_argument("--pr", type=int, required=True)
    arguments = parser.parse_args()
    sys.exit(asyncio.run(main(arguments.installation_id, arguments.repo, arguments.pr)))
