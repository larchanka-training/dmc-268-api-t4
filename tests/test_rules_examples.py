import re
from pathlib import Path

import pytest

from adapters.llm.rules import RULES_MAX, RULES_MAX_CHARS, parse_rules

ROOT = Path(__file__).parent.parent
TEMPLATES_DIR = ROOT / "examples" / "rules"
GUIDE = ROOT / "docs" / "RULES_GUIDE.md"
TEMPLATE_NAMES = ("python-fastapi.md", "typescript-react.md", "go.md", "security-baseline.md")
IGNORE_SECTION = "Ignore"


def guide_example(name: str) -> str:
    match = re.search(
        rf"<!-- example:{name} -->\n```markdown\n(?P<body>.*?)\n```",
        GUIDE.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None, f"RULES_GUIDE.md has no example block named {name}"
    return match.group("body")


def test_templates_directory_holds_exactly_the_documented_templates() -> None:
    assert sorted(path.name for path in TEMPLATES_DIR.glob("*.md")) == sorted(TEMPLATE_NAMES)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_parses_without_dropped_rules(name: str) -> None:
    rule_set = parse_rules((TEMPLATES_DIR / name).read_text(encoding="utf-8"))

    assert rule_set.dropped_suspicious == 0
    assert rule_set.dropped_over_limit == 0
    assert len(rule_set.rules) <= RULES_MAX
    assert sum(len(rule.text) for rule in rule_set.rules) <= RULES_MAX_CHARS


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_has_15_to_35_rules_and_an_ignore_section(name: str) -> None:
    rule_set = parse_rules((TEMPLATES_DIR / name).read_text(encoding="utf-8"))
    rules = [rule for rule in rule_set.rules if rule.section != IGNORE_SECTION]
    directives = [rule for rule in rule_set.rules if rule.section == IGNORE_SECTION]

    assert 15 <= len(rules) <= 35
    assert directives
    assert all(rule.text.startswith("path: ") for rule in directives)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_guide_links_every_template(name: str) -> None:
    assert f"(../examples/rules/{name})" in GUIDE.read_text(encoding="utf-8")


def test_guide_bad_example_is_dropped_as_suspicious() -> None:
    rule_set = parse_rules(guide_example("suspicious-rules"))

    assert rule_set.rules == ()
    assert rule_set.dropped_suspicious == 3


def test_guide_good_example_is_kept() -> None:
    rule_set = parse_rules(guide_example("accepted-rules"))

    assert rule_set.dropped_suspicious == 0
    assert len(rule_set.rules) == 3
