"""Repository review rules (`.review/rules.md`): parsing, sanitizing and limits.

docs/RULES_FORMAT_SPEC.md §2: a heading of any level names a section, every list item
under it is one rule, and everything outside list items is ignored. Recognised section
titles give their rules a category (§2.2); items under `# Ignore` are directives (§2.3),
validated here and never sent to a model. Applying them (the diff filter and the
category mute) is not wired yet. Every rule and directive keeps the lines it came from,
and every dropped item gets a diagnostic, for the rules editor in the console.
"""

import dataclasses
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from domain.models import Category
from domain.repository_rules import (
    IgnoreDirective,
    IgnoreKind,
    ParsedRules,
    PreviewRule,
    RulesDiagnostic,
    RulesDiagnosticCode,
    is_ignore_section,
    section_category,
)

# Limits on what a repository may feed into the prompt; everything over them is dropped.
RULES_MAX = 50
RULES_MAX_CHARS = 8000
IGNORE_MAX = 100
DEFAULT_SECTION = "General"

# Rules that look like attempts to steer the model are dropped whole. This is a second line
# of defence: the first is that rules only ever travel as marked, untrusted data. The
# patterns only know English and Russian; the JEV rule screen (rule_screen.py) covers the rest.
SUSPICIOUS_RULE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:ignore|disregard|forget)\b.{0,40}?\b(?:all|previous|prior|above|earlier)\b"
        r".{0,40}?\binstructions?\b",
        r"\bsystem\s+prompt\b",
        r"\byou\s+are\s+now\b",
        r"\bact\s+as\s+(?:an?\s+|the\s+)?(?:assistant|ai|model|system|admin\w*|developer)\b",
        r"\b(?:change|override|alter)\b.{0,40}?\b(?:output|response)\s+format\b",
        r"\b(?:change|override|alter)\b.{0,40}?\bjson\b",
        r"\b(?:do\s+not|don'?t|never)\s+report\s+(?:all|any|every|security)\b",
        r"\b(?:skip|suppress|hide)\b.{0,20}?\b(?:all|any|every|security)\b.{0,20}?\bfindings?\b",
        r"\b(?:reveal|print|show|output|repeat)\b.{0,40}?\bprompt\b",
        r"игнорир\w*.{0,40}?инструкци",
        r"системн\w*\s+промпт",
        r"не\s+сообщай(?:те)?\s+(?:об?\s+)?(?:находк|замечан|уязвимост)",
        # A marker look-alike in a rule can only be an attempt to break out of the block.
        r"<<<\s*(?:END_)?UNTRUSTED_\w+\s*>>>",
    )
)

_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(?P<title>.*?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])\s+(?P<text>.*)$")
_WHITESPACE = re.compile(r"\s+")
_DIRECTIVE = re.compile(r"^(?P<kind>path|category)\s*:\s*(?P<value>.*)$", re.IGNORECASE)
_CATEGORY_VALUES = frozenset(category.value for category in Category)


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    section: str
    text: str
    # The category of a recognised section (RULES_FORMAT_SPEC.md §2.2), else None.
    category: Category | None = None
    # The list item's lines in the file, 1-based and inclusive; 0 when not from a file.
    line_start: int = 0
    line_end: int = 0


@dataclass(frozen=True, slots=True)
class RuleSet:
    rules: tuple[Rule, ...] = ()
    dropped_suspicious: int = 0
    dropped_over_limit: int = 0
    # `# Ignore` directives (§2.3): parsed and validated, never sent to a model.
    ignore: tuple[IgnoreDirective, ...] = ()
    diagnostics: tuple[RulesDiagnostic, ...] = ()

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


def _blank_comments(text: str) -> str:
    """Drop HTML comments but keep their line breaks, so line numbers stay true."""
    return _HTML_COMMENT.sub(lambda match: "\n" * match.group().count("\n"), text)


@dataclass(frozen=True, slots=True)
class _Item:
    section: str
    # Line of the heading that opened the section; 0 for the default section.
    section_line: int
    text: str
    line_start: int
    line_end: int


def _split_items(markdown: str) -> list[_Item]:
    """One entry per list item, in document order, with its section and lines."""
    items: list[_Item] = []
    section = DEFAULT_SECTION
    section_line = 0
    current: list[str] | None = None
    start = end = 0

    def close() -> None:
        nonlocal current
        if current is not None:
            text = _collapse(" ".join(current))
            if text:
                items.append(_Item(section, section_line, text, start, end))
        current = None

    for number, line in enumerate(markdown.split("\n"), start=1):
        heading = _HEADING.match(line)
        item = _LIST_ITEM.match(line)
        if heading:
            close()
            section = _collapse(heading.group("title")) or DEFAULT_SECTION
            section_line = number
        elif item:
            close()
            current = [item.group("text")]
            start = end = number
        elif not line.strip():
            continue
        elif current is not None and line[:1] in (" ", "\t"):
            current.append(line)
            end = number
        else:
            close()
    close()
    return items


def _parse_directive(text: str) -> tuple[IgnoreKind, str] | RulesDiagnosticCode:
    """`path: <glob>` or `category: <name>`, or the reason the item is invalid."""
    match = _DIRECTIVE.match(text)
    if match is None:
        return RulesDiagnosticCode.IGNORE_INVALID
    kind = IgnoreKind(match.group("kind").casefold())
    value = match.group("value").strip().strip("`").strip()
    if kind is IgnoreKind.PATH:
        # No negation (§2.3), and a glob is one token.
        if not value or value.startswith("!") or _WHITESPACE.search(value):
            return RulesDiagnosticCode.IGNORE_INVALID
        return kind, value
    name = value.casefold()
    if name == Category.SECURITY:
        return RulesDiagnosticCode.IGNORE_SECURITY
    if name not in _CATEGORY_VALUES:
        return RulesDiagnosticCode.IGNORE_UNKNOWN_CATEGORY
    return kind, name


def rule_id(index: int) -> str:
    return f"R{index}"


def renumber_rules(rules: Sequence[Rule]) -> tuple[Rule, ...]:
    """Ids R1..Rn in order, so the prompt never shows gaps left by dropped rules."""
    return tuple(
        dataclasses.replace(rule, id=rule_id(index)) for index, rule in enumerate(rules, start=1)
    )


def parse_rules(markdown: str | None) -> RuleSet:
    """Turn rules.md into numbered rules and ignore directives, enforcing the heuristic
    filter and the limits, and say why each dropped item was dropped."""
    if not markdown or not markdown.strip():
        return RuleSet()
    text = _blank_comments(sanitize_rules_text(markdown))

    kept: list[Rule] = []
    ignore: list[IgnoreDirective] = []
    diagnostics: list[RulesDiagnostic] = []
    noted_sections: set[int] = set()
    suspicious = 0
    over_limit = 0
    total_chars = 0

    def note(code: RulesDiagnosticCode, start: int, end: int) -> None:
        diagnostics.append(RulesDiagnostic(code=code, line_start=start, line_end=end))

    for item in _split_items(text):
        if is_ignore_section(item.section):
            # Directives skip the pattern filter: they never reach a model (§6).
            if len(ignore) >= IGNORE_MAX:
                note(RulesDiagnosticCode.IGNORE_OVER_LIMIT, item.line_start, item.line_end)
                continue
            directive = _parse_directive(item.text)
            if isinstance(directive, RulesDiagnosticCode):
                note(directive, item.line_start, item.line_end)
                continue
            kind, value = directive
            ignore.append(IgnoreDirective(kind, value, item.line_start, item.line_end))
            continue

        category = section_category(item.section)
        if category is None and item.section_line and item.section_line not in noted_sections:
            noted_sections.add(item.section_line)
            note(RulesDiagnosticCode.SECTION_OTHER, item.section_line, item.section_line)
        if is_suspicious_rule(item.text) or is_suspicious_rule(item.section):
            suspicious += 1
            note(RulesDiagnosticCode.RULE_SUSPICIOUS, item.line_start, item.line_end)
            continue
        if len(kept) >= RULES_MAX or total_chars + len(item.text) > RULES_MAX_CHARS:
            over_limit += 1
            note(RulesDiagnosticCode.RULE_OVER_LIMIT, item.line_start, item.line_end)
            continue
        total_chars += len(item.text)
        kept.append(
            Rule(
                id=rule_id(len(kept) + 1),
                section=item.section,
                text=item.text,
                category=category,
                line_start=item.line_start,
                line_end=item.line_end,
            )
        )
    return RuleSet(
        rules=tuple(kept),
        dropped_suspicious=suspicious,
        dropped_over_limit=over_limit,
        ignore=tuple(ignore),
        diagnostics=tuple(diagnostics),
    )


class MarkdownRulesParser:
    """Implements domain.ports.RulesParser with the parser the review prompt uses."""

    def parse(self, markdown: str) -> ParsedRules:
        rule_set = parse_rules(markdown)
        return ParsedRules(
            rules=tuple(
                PreviewRule(
                    id=rule.id,
                    section=rule.section,
                    text=rule.text,
                    category=rule.category,
                    line_start=rule.line_start,
                    line_end=rule.line_end,
                )
                for rule in rule_set.rules
            ),
            dropped_suspicious=rule_set.dropped_suspicious,
            dropped_over_limit=rule_set.dropped_over_limit,
            ignore=rule_set.ignore,
            diagnostics=rule_set.diagnostics,
        )
