"""The smoke's --context: the worker's context assembly from a local checkout."""

from adapters.llm.prompt import (
    CONTEXT_START,
    RULES_START,
    UNTRUSTED_START,
    build_messages,
)
from adapters.llm.rules import parse_rules
from adapters.llm.smoke import load_context
from domain.ports import ReviewRequest

DIFF = (
    "diff --git a/app/x.py b/app/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app/x.py\n"
    "+++ b/app/x.py\n"
    "@@ -5,2 +5,3 @@ def run(a):\n"
    "     return helper(a)\n"
    "+    return helper(a + 1)\n"
    " \n"
)

SOURCE = (
    "def helper(x):\n    '''Docs.'''\n    return x + 1\n\n\ndef run(a):\n    return helper(a)\n"
)

RULES_MD = "# Security\n- Bind every SQL parameter.\n"


def test_context_from_a_checkout_dir(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text(SOURCE, encoding="utf-8")

    contexts = load_context(DIFF, tmp_path, budget_tokens=10_000)

    assert len(contexts) == 1
    assert contexts[0].path == "app/x.py"
    assert contexts[0].blocks  # tier 1 at least


def test_a_missing_checkout_file_gets_no_context(tmp_path) -> None:
    assert load_context(DIFF, tmp_path, budget_tokens=10_000) == ()


def test_without_a_checkout_there_is_no_context() -> None:
    assert load_context(DIFF, None, budget_tokens=10_000) == ()


def test_the_full_prompt_carries_all_three_blocks(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text(SOURCE, encoding="utf-8")

    request = ReviewRequest(
        diff_text=DIFF,
        pr_title="Smoke",
        pr_description="",
        rules_markdown=RULES_MD,
        context=load_context(DIFF, tmp_path, budget_tokens=10_000),
    )
    user = build_messages(request, parse_rules(RULES_MD).rules)[1]["content"]

    assert RULES_START in user
    assert CONTEXT_START in user
    assert UNTRUSTED_START in user
    assert user.index(RULES_START) < user.index(CONTEXT_START) < user.index(UNTRUSTED_START)
