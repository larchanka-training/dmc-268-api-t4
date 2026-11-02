"""domain/redaction.py: prefixed patterns only, the context around a secret kept,
and a report of what fired. Fixture values are fakes built to match the formats."""

import pytest

from domain.redaction import RULESET_VERSION, redact

GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5" * 4  # 40 characters after the prefix
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKC\n-----END RSA PRIVATE KEY-----"
TELEGRAM_TOKEN = "123456789:" + "AAH" + "x1Y2z3" * 5 + "_-"  # 35 characters after the colon


@pytest.mark.parametrize(
    ("text", "rule", "secret"),
    [
        (f"token={GITHUB_TOKEN}", "github_token", GITHUB_TOKEN),
        ("github_pat_" + "a" * 30, "github_token", "github_pat_" + "a" * 30),
        ("key sk-test-aaaaaaaaaaaaaaaa1111 rejected", "sk_key", "sk-test-aaaaaaaaaaaaaaaa1111"),
        ("sk-ant-api03-" + "x" * 30, "sk_key", "sk-ant-api03-" + "x" * 30),
        ("whsec_" + "b" * 20, "stripe_secret", "whsec_" + "b" * 20),
        ("sk_live_" + "c" * 20, "stripe_secret", "sk_live_" + "c" * 20),
        ("AKIAABCDEFGHIJKLMNOP", "aws_access_key_id", "AKIAABCDEFGHIJKLMNOP"),
        ("xoxb-1234567890-abcdef", "slack_token", "xoxb-1234567890-abcdef"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlLXNpZw",
            "jwt",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlLXNpZw",
        ),
        (f"key:\n{PEM}\n", "private_key", "MIIEow"),
        (
            f'HTTP Request: POST https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage "200 OK"',
            "telegram_bot_token",
            TELEGRAM_TOKEN,
        ),
        (f"ALERTS_TELEGRAM_BOT_TOKEN={TELEGRAM_TOKEN}", "telegram_bot_token", TELEGRAM_TOKEN),
    ],
)
def test_each_rule_replaces_its_secret(text: str, rule: str, secret: str) -> None:
    redacted, report = redact(text)

    assert secret not in redacted
    assert f"[REDACTED:{rule}]" in redacted
    assert dict(report.counts)[rule] >= 1


def test_the_context_around_a_secret_survives() -> None:
    redacted, _ = redact("Authorization: Bearer abc.DEF-123_xyz")
    assert redacted == "Authorization: Bearer [REDACTED:bearer_token]"

    redacted, _ = redact("postgresql+asyncpg://dmc:s3cret@postgres:5432/dmc268")
    assert redacted == "postgresql+asyncpg://[REDACTED:url_credentials]@postgres:5432/dmc268"


def test_a_private_key_cut_off_before_its_end_line_is_still_redacted() -> None:
    redacted, _ = redact("-----BEGIN PRIVATE KEY-----\nMIIEvQ")

    assert "MIIEvQ" not in redacted


def test_the_report_counts_matches_and_carries_the_ruleset_version() -> None:
    _, report = redact(f"{GITHUB_TOKEN} and again {GITHUB_TOKEN}")

    assert report.ruleset_version == RULESET_VERSION
    assert report.counts == (("github_token", 2),)
    assert report.total == 2


@pytest.mark.parametrize(
    "text",
    [
        # Precision over recall: shapes that look random but are not secrets.
        "a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5",  # a git sha
        "0192f1c4-7a2b-7c3d-8e4f-5a6b7c8d9e0f",  # a uuid
        "sk-…abcd",  # a key already masked by adapters/llm/keys.py
        "ghp_short",  # a prefix without a token behind it
        "plain text with a sk- in it",
        "started 2026-10-26T12:00:00Z, job 42:queued",  # a time and a short id:word pair
    ],
)
def test_lookalikes_are_left_alone(text: str) -> None:
    redacted, report = redact(text)

    assert redacted == text
    assert report.total == 0
