"""Secret redaction: a pure function over text returning ``(redacted, report)``.

docs/BACKEND_ARCHITECTURE.md § Secret redaction: a published rule set, versioned
so a rule change is visible, tuned for precision over recall. Every rule is a
prefixed or structured pattern; there is no entropy heuristic, which on source
code flags base64 blobs, UUIDs and git SHAs indiscriminately.

It is a net for structured secrets, not for content: a line of diff or a piece of
a model reply passes unchanged (docs/observability.md § What is never logged).
The log processor uses it today; the same rules are meant for LLM egress.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

#: Bumped whenever a rule is added, removed or changed.
RULESET_VERSION = "1"


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """One pattern. A ``secret`` group, when present, is the only part replaced,
    so the context around it (``Bearer``, the URL) survives."""

    name: str
    pattern: re.Pattern[str]

    @property
    def placeholder(self) -> str:
        return f"[REDACTED:{self.name}]"


@dataclass(frozen=True, slots=True)
class RedactionReport:
    ruleset_version: str
    #: (rule name, matches) for each rule that fired, in rule order.
    counts: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        return sum(count for _, count in self.counts)


RULES: tuple[RedactionRule, ...] = (
    # A PEM private key, also when cut off before its END line.
    RedactionRule(
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
            r"(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----|[\s\S]*)"
        ),
    ),
    # user:password@ in any URL, e.g. a DSN.
    RedactionRule("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    RedactionRule(
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+(?P<secret>[A-Za-z0-9\-._~+/]+=*)"),
    ),
    RedactionRule(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
    ),
    # OpenAI-compatible keys, Anthropic (sk-ant-…) and project keys (sk-proj-…).
    RedactionRule("sk_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    RedactionRule(
        "stripe_secret",
        re.compile(r"\b(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|whsec_[A-Za-z0-9_]{10,})"),
    ),
    RedactionRule("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    RedactionRule("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    RedactionRule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
)


def redact(text: str, rules: tuple[RedactionRule, ...] = RULES) -> tuple[str, RedactionReport]:
    """Replace every match with ``[REDACTED:<rule>]``; report what fired."""
    counts: list[tuple[str, int]] = []
    for rule in rules:
        text, matches = rule.pattern.subn(_replacement(rule), text)
        if matches:
            counts.append((rule.name, matches))
    return text, RedactionReport(RULESET_VERSION, tuple(counts))


def _replacement(rule: RedactionRule) -> Callable[[re.Match[str]], str] | str:
    if "secret" not in rule.pattern.groupindex:
        return rule.placeholder

    def keep_context(match: re.Match[str]) -> str:
        start, end = match.span("secret")
        whole = match.group(0)
        offset = match.start()
        return whole[: start - offset] + rule.placeholder + whole[end - offset :]

    return keep_context
