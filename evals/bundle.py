"""The bundle: every scored case merged into one large multi-file pull request.

Gold cases are 5-22 line single-file diffs, while live pull requests run to hundreds of lines
over a dozen files. The bundle approximates a live pull request, so a run shows how review
quality holds up when one request carries many unrelated changes. Each case's paths get the
case id as a prefix: two cases touching the same file stay apart, and every expected defect
keeps a key of its own. Schema-only cases score nothing and stay out.
"""

import dataclasses
from collections.abc import Sequence
from pathlib import Path

from evals.models import Case, Metrics

BUNDLE_ID = "bundle"
BUNDLE_TAG = "bundle"
_HEADER_PREFIXES = ("--- a/", "+++ b/", "rename from ", "rename to ", "copy from ", "copy to ")


def prefix_paths(diff_text: str, prefix: str) -> str:
    """Put `prefix/` in front of every path in the file headers; hunk bodies stay as they are."""
    out: list[str] = []
    in_header = False
    for line in diff_text.split("\n"):
        if line.startswith("diff --git a/"):
            in_header = True
            old, _, new = line.removeprefix("diff --git a/").partition(" b/")
            out.append(f"diff --git a/{prefix}/{old} b/{prefix}/{new}")
            continue
        if line.startswith("@@"):
            in_header = False
        if in_header:
            marker = next((m for m in _HEADER_PREFIXES if line.startswith(m)), None)
            if marker is not None:
                out.append(f"{marker}{prefix}/{line.removeprefix(marker)}")
                continue
        out.append(line)
    return "\n".join(out)


def build_bundle(cases: Sequence[Case], fixture: Path) -> tuple[Case, str]:
    """The bundle case and its diff text; the caller writes the text to `fixture`."""
    scored = [case for case in cases if case.metrics is not Metrics.SCHEMA_ONLY]
    diffs = [prefix_paths(case.read_diff(), case.id).rstrip("\n") for case in scored]
    expected = tuple(
        dataclasses.replace(defect, path=f"{case.id}/{defect.path}")
        for case in scored
        for defect in case.expected
    )
    must_not = tuple(
        dataclasses.replace(region, path=f"{case.id}/{region.path}")
        for case in scored
        for region in case.must_not
    )
    bundle = Case(
        id=BUNDLE_ID,
        intent="every scored case reviewed as one large pull request",
        pattern="bundle",
        metrics=Metrics.DETECTION,
        fixture=fixture,
        title=f"Bundle of {len(scored)} unrelated changes",
        description="",
        expected=expected,
        must_not=must_not,
        tags=frozenset({BUNDLE_TAG}),
    )
    return bundle, "\n".join(diffs) + "\n"
