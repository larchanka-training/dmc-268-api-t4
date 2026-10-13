"""Repository review rules (`.review/rules.md`): parsing, sanitizing and limits.

Interim format until the tech lead's RULES_FORMAT_SPEC.md (#31): a `#`/`##` heading names a
section, every list item under it is one rule, and everything outside list items is ignored.
"""

import re
import unicodedata
from dataclasses import dataclass

# Limits on what a repository may feed into the prompt; everything over them is dropped.
RULES_MAX = 50
RULES_MAX_CHARS = 8000
DEFAULT_SECTION = "General"

# Rules that look like attempts to steer the model are dropped whole. This is a second line
# of defence: the first is that rules only ever travel as marked, untrusted data.
SUSPICIOUS_RULE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:ignore|disregard|forget)\b.{0,40}?\b(?:all|previous|prior|above|earlier)\b"
        r".{0,40}?\binstructions?\b",
        r"\bsystem\s+prompt\b",
        r"\byou\s+are\s+now\b",
        r"\bact\s+as\b",
        r"\b(?:change|override|alter)\b.{0,40}?\b(?:output|response)\s+format\b",
        r"\b(?:change|override|alter)\b.{0,40}?\bjson\b",
        r"\b(?:do\s+not|don'?t|never)\s+report\b",
        r"\b(?:skip|suppress|hide)\b.{0,40}?\bfindings?\b",
        r"\b(?:reveal|print|show|output|repeat)\b.{0,40}?\bprompt\b",
        r"игнорир\w*.{0,40}?инструкци",
        r"системн\w*\s+промпт",
        r"не\s+сообща",
    )
)

_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(?P<title>.*?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])\s+(?P<text>.*)$")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    section: str
    text: str


@dataclass(frozen=True, slots=True)
class RuleSet:
    rules: tuple[Rule, ...] = ()
    dropped_suspicious: int = 0
    dropped_over_limit: int = 0

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(rule.id for rule in self.rules)


def sanitize_rules_text(text: str) -> str:
    """Normalize line breaks; drop control characters (except \\n, \\t) and invisible ones.

    Zero-width and bidi-override characters (Unicode category Cf) can hide text from a
    human reviewer of rules.md while the model still reads it.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        char for char in text if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf")
    )


def is_suspicious_rule(text: str) -> bool:
    return any(pattern.search(text) for pattern in SUSPICIOUS_RULE_PATTERNS)


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _split_items(markdown: str) -> list[tuple[str, str]]:
    """(section, text) per list item, in document order."""
    items: list[tuple[str, str]] = []
    section = DEFAULT_SECTION
    current: list[str] | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            text = _collapse(" ".join(current))
            if text:
                items.append((section, text))
        current = None

    for line in markdown.split("\n"):
        heading = _HEADING.match(line)
        item = _LIST_ITEM.match(line)
        if heading:
            close()
            section = _collapse(heading.group("title")) or DEFAULT_SECTION
        elif item:
            close()
            current = [item.group("text")]
        elif not line.strip():
            continue
        elif current is not None and line[:1] in (" ", "\t"):
            current.append(line)
        else:
            close()
    close()
    return items


def parse_rules(markdown: str | None) -> RuleSet:
    """Turn rules.md into numbered rules, enforcing the heuristic filter and the limits."""
    if not markdown or not markdown.strip():
        return RuleSet()
    text = _HTML_COMMENT.sub("", sanitize_rules_text(markdown))

    kept: list[Rule] = []
    suspicious = 0
    over_limit = 0
    total_chars = 0
    for section, rule_text in _split_items(text):
        if is_suspicious_rule(rule_text) or is_suspicious_rule(section):
            suspicious += 1
            continue
        if len(kept) >= RULES_MAX or total_chars + len(rule_text) > RULES_MAX_CHARS:
            over_limit += 1
            continue
        total_chars += len(rule_text)
        kept.append(Rule(id=f"R{len(kept) + 1}", section=section, text=rule_text))
    return RuleSet(rules=tuple(kept), dropped_suspicious=suspicious, dropped_over_limit=over_limit)
