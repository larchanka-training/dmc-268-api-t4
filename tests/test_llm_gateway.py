import json
import logging

import httpx
import pytest

from adapters.llm.config import LLMSettings, ProviderSettings
from adapters.llm.factory import build_gateway
from adapters.llm.gateway import FallbackLLMGateway
from adapters.llm.prompt import build_messages, estimate_tokens
from adapters.llm.rule_screen import JevRuleScreen
from adapters.llm.rules import Rule
from domain.errors import (
    AllProvidersFailedError,
    LLMInputTooLargeError,
    LLMOutputError,
    ProviderUnavailableError,
)
from domain.ports import ContextBlock, ContextBlockKind, FileContext, ReviewRequest
from tests.conftest import (
    FAKE_KEY_A,
    FAKE_KEY_B,
    VALID_FINDING,
    RecordingTransport,
    chat_response,
    log_events,
    make_provider,
    replies,
    review_json,
)

REQUEST = ReviewRequest(diff_text="diff --git a/x b/x", pr_title="t", pr_description="d")


async def test_primary_503_falls_back_to_ollama(caplog: pytest.LogCaptureFixture) -> None:
    primary = make_provider(RecordingTransport(replies(httpx.Response(503))))
    fallback_transport = RecordingTransport(replies(chat_response(review_json(VALID_FINDING))))
    fallback = make_provider(fallback_transport, name="ollama", keys=())

    with caplog.at_level(logging.INFO, logger="adapters.llm"):
        result = await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    assert result.provider == "ollama"
    assert result.model == "ollama-model"
    assert len(result.findings) == 1
    [fallback_record] = log_events(caplog, "llm.provider.fallback")
    assert fallback_record.next_provider == "ollama"
    [finished] = log_events(caplog, "llm.review.finished")
    assert (finished.provider, finished.model) == ("ollama", "ollama-model")


async def test_output_error_on_primary_also_falls_back() -> None:
    primary = make_provider(RecordingTransport(replies(chat_response("x"), chat_response("y"))))
    fallback = make_provider(
        RecordingTransport(replies(chat_response(review_json()))), name="ollama", keys=()
    )

    result = await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    assert result.provider == "ollama"


async def test_all_providers_failed_keeps_every_cause(caplog: pytest.LogCaptureFixture) -> None:
    primary = make_provider(RecordingTransport(replies(httpx.ReadTimeout("slow"))))
    fallback = make_provider(
        RecordingTransport(replies(httpx.Response(500))), name="ollama", keys=()
    )

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    causes = excinfo.value.causes
    assert len(causes) == 2
    assert all(isinstance(cause, ProviderUnavailableError) for cause in causes)
    assert [cause.provider for cause in causes if isinstance(cause, ProviderUnavailableError)] == [
        "eurouter",
        "ollama",
    ]
    assert "timeout" in str(causes[0])
    assert "HTTP 500" in str(causes[1])
    # A warning: the job is retried, so this is not yet a Sentry event.
    [record] = log_events(caplog, "llm.call.failed")
    assert record.levelno == logging.WARNING
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_primary_success_does_not_touch_fallback() -> None:
    primary = make_provider(RecordingTransport(replies(chat_response(review_json()))))
    fallback_transport = RecordingTransport(replies())
    fallback = make_provider(fallback_transport, name="ollama", keys=())

    result = await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    assert result.provider == "eurouter"
    assert fallback_transport.requests == []


async def test_single_provider_output_error_is_wrapped() -> None:
    primary = make_provider(RecordingTransport(replies(chat_response("x"), chat_response("y"))))

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await FallbackLLMGateway([primary]).review(REQUEST)

    assert isinstance(excinfo.value.causes[0], LLMOutputError)


def test_gateway_requires_providers() -> None:
    with pytest.raises(ValueError):
        FallbackLLMGateway([])


async def test_factory_builds_keyed_primary_and_keyless_fallback() -> None:
    settings = LLMSettings(
        primary=ProviderSettings(
            name="eurouter",
            base_url="https://primary.test/api/v1",
            model="m1",
            api_keys=(FAKE_KEY_A, FAKE_KEY_B),
        ),
        fallback=ProviderSettings(name="ollama", base_url="http://fallback.test/v1", model="m2"),
        timeout_seconds=5,
        temperature=0.0,
        json_mode=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.test":
            return httpx.Response(503)
        return chat_response(review_json())

    transport = RecordingTransport(handler)

    result = await build_gateway(settings, transport=transport).review(REQUEST)

    assert (result.provider, result.model) == ("ollama", "m2")
    primary_request, fallback_request = transport.requests
    assert primary_request.headers["Authorization"] == f"Bearer {FAKE_KEY_A}"
    assert "Authorization" not in fallback_request.headers


async def test_input_over_budget_is_refused_before_any_provider_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = RecordingTransport(replies(chat_response(review_json(VALID_FINDING))))
    big = ReviewRequest(diff_text="x" * 30_000, pr_title="t", pr_description="d")

    with (
        caplog.at_level(logging.WARNING, logger="adapters.llm"),
        pytest.raises(LLMInputTooLargeError) as error,
    ):
        await FallbackLLMGateway([make_provider(transport)], max_input_tokens=1000).review(big)

    assert transport.requests == []
    assert error.value.limit == 1000
    assert error.value.estimated_tokens > 1000
    assert "x" * 50 not in caplog.text


async def test_input_within_budget_is_reviewed() -> None:
    transport = RecordingTransport(replies(chat_response(review_json(VALID_FINDING))))

    result = await FallbackLLMGateway([make_provider(transport)], max_input_tokens=32000).review(
        REQUEST
    )

    assert len(result.findings) == 1


async def test_optional_context_is_cut_before_a_valid_diff_is_rejected() -> None:
    diff = "D" * 9000
    request = ReviewRequest(
        diff_text=diff,
        pr_title="t",
        pr_description="d",
        context=(
            FileContext(
                path="app/users.py",
                blocks=(
                    ContextBlock(
                        kind=ContextBlockKind.ENCLOSING,
                        name="changed",
                        start_line=1,
                        end_line=1,
                        text="E" * 600,
                    ),
                    ContextBlock(
                        kind=ContextBlockKind.CALLED,
                        name="called",
                        start_line=2,
                        end_line=2,
                        text="C" * 5000,
                    ),
                ),
            ),
        ),
    )
    base = ReviewRequest(diff_text=diff, pr_title="t", pr_description="d")
    limit = 5000
    assert estimate_tokens(build_messages(base)) < limit
    transport = RecordingTransport(replies(chat_response(review_json())))

    result = await FallbackLLMGateway([make_provider(transport)], max_input_tokens=limit).review(
        request
    )

    assert len(result.findings) == 0
    [outbound] = transport.requests
    messages = json.loads(outbound.content)["messages"]
    user_message = messages[1]["content"]
    assert "D" * 100 in user_message
    assert "E" * 100 in user_message
    assert "C" * 100 not in user_message
    assert estimate_tokens(messages) <= limit


async def test_context_cannot_exceed_its_share_when_full_prompt_has_room() -> None:
    request = ReviewRequest(
        diff_text="small diff",
        pr_title="t",
        pr_description="d",
        context=(
            FileContext(
                path="app/users.py",
                blocks=(
                    ContextBlock(
                        kind=ContextBlockKind.ENCLOSING,
                        name="large",
                        start_line=1,
                        end_line=1,
                        text="E" * 42_000,
                    ),
                ),
            ),
        ),
    )
    limit = 32_000
    transport = RecordingTransport(replies(chat_response(review_json())))

    await FallbackLLMGateway([make_provider(transport)], max_input_tokens=limit).review(request)

    [outbound] = transport.requests
    messages = json.loads(outbound.content)["messages"]
    user_message = messages[1]["content"]
    assert "small diff" in user_message
    assert "E" * 100 not in user_message
    assert estimate_tokens(messages) <= limit


async def test_source_context_secret_is_redacted_in_provider_request() -> None:
    token = "ghp_" + "A" * 30
    rule_token = "ghp_" + "C" * 30
    request = ReviewRequest(
        diff_text="diff --git a/x b/x",
        pr_title="t",
        pr_description="d",
        rules_markdown=f"# Security\n- Do not expose {rule_token}.",
        context=(
            FileContext(
                path="app/users.py",
                blocks=(
                    ContextBlock(
                        kind=ContextBlockKind.ENCLOSING,
                        name="load_user",
                        start_line=40,
                        end_line=42,
                        text=f'def load_user():\n    token = "{token}"\n    return token',
                    ),
                ),
            ),
        ),
    )
    transport = RecordingTransport(replies(chat_response(review_json())))

    await FallbackLLMGateway([make_provider(transport)]).review(request)

    [outbound] = transport.requests
    messages = json.loads(outbound.content)["messages"]
    user_message = messages[1]["content"]
    assert token not in user_message
    assert rule_token not in user_message
    assert "[REDACTED:github_token]" in user_message
    assert "  42 |     return token" in user_message


async def test_rule_secret_is_redacted_in_typesafe_request() -> None:
    token = "ghp_" + "B" * 30
    transport = RecordingTransport(
        replies(
            httpx.Response(
                200,
                json={"model": "test", "answers": {"rule_1": {"noul": 0.0}}},
            )
        )
    )
    screen = JevRuleScreen(api_key="test-key", transport=transport)

    await screen.screen((Rule(id="R1", section="Security", text=f"Avoid {token}."),))

    [outbound] = transport.requests
    state = json.loads(outbound.content)["state"]
    assert token not in state
    assert "[REDACTED:github_token]" in state


TWO_FILE_DIFF = (
    "diff --git a/src/x.py b/src/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/x.py\n"
    "+++ b/src/x.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def one():\n"
    "+    return 1\n"
    " \n"
    "diff --git a/src/y.py b/src/y.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/src/y.py\n"
    "+++ b/src/y.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def two():\n"
    "+    return 2\n"
    " \n"
)


def _context(path: str, text: str) -> FileContext:
    return FileContext(
        path=path,
        blocks=(
            ContextBlock(
                kind=ContextBlockKind.ENCLOSING,
                name=f"sym_{path}",
                start_line=1,
                end_line=3,
                text=text,
            ),
        ),
    )


def test_per_file_parts_carry_only_their_own_file_context() -> None:
    # [C4]'s cap is per request: handing every per-file part the full context
    # would spend n_files × context. Each part gets its own file's blocks only.
    from adapters.llm.config import ReviewScope
    from adapters.llm.context_budget import split_request

    request = ReviewRequest(
        diff_text=TWO_FILE_DIFF,
        pr_title="t",
        pr_description="d",
        context=(
            _context("src/x.py", "def one():\n    return 1\n"),
            _context("src/y.py", "def two():\n    return 2\n"),
        ),
    )

    (whole,) = split_request(request, ReviewScope.PR)
    assert whole is request
    first, second = split_request(request, ReviewScope.FILE)
    assert first.diff_text.startswith("diff --git a/src/x.py")
    assert [c.path for c in first.context] == ["src/x.py"]
    assert [c.path for c in second.context] == ["src/y.py"]


def test_a_part_whose_path_cannot_be_parsed_gets_no_context() -> None:
    from adapters.llm.config import ReviewScope
    from adapters.llm.context_budget import split_request

    request = ReviewRequest(
        diff_text="not a diff at all",
        pr_title="t",
        pr_description="d",
        context=(_context("src/x.py", "x = 1\n"),),
    )

    (part,) = split_request(request, ReviewScope.FILE)

    assert part.diff_text == "not a diff at all"
    assert part.context == ()


class _CapturingProvider:
    """A ChatProvider that records the messages of every call."""

    name = "capture"
    model = "test-model"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages) -> str:  # type: ignore[no-untyped-def]
        self.calls.append([dict(m) for m in messages])
        return review_json()


async def test_a_fit_passed_by_the_caller_is_reused_not_recomputed() -> None:
    # The worker computes the fit once and passes it through; the gateway must
    # send exactly fit.request, so the logged context_tokens/context_cut
    # describe the request that reached the provider.
    from adapters.llm.context_budget import fit_context_to_prompt

    provider = _CapturingProvider()
    gateway = FallbackLLMGateway([provider], max_input_tokens=2_000)
    request = ReviewRequest(
        diff_text=TWO_FILE_DIFF,
        pr_title="t",
        pr_description="d",
        context=(_context("src/x.py", "x = 1\n" * 20), _context("src/y.py", "y = 2\n" * 800)),
    )
    fit = fit_context_to_prompt(request, (), max_input_tokens=2_000)
    assert fit.request.context != request.context  # the fit did trim something

    await gateway.review(request, fit=fit)

    [messages] = provider.calls
    sent = json.dumps(messages)
    assert fit.context_tokens > 0
    assert "sym_src/x.py" in sent or "sym_src/y.py" in sent
    # Whatever survived the fit is exactly what was sent: the same FileContext
    # paths, and no context the fit had cut away.
    for context in fit.request.context:
        assert context.path in sent
    assert estimate_tokens(messages) <= 2_000


async def test_the_gateway_fits_itself_when_no_fit_is_given() -> None:
    provider = _CapturingProvider()
    gateway = FallbackLLMGateway([provider], max_input_tokens=2_000)
    request = ReviewRequest(
        diff_text=TWO_FILE_DIFF,
        pr_title="t",
        pr_description="d",
        context=(_context("src/x.py", "x = 1\n" * 800),),
    )

    await gateway.review(request)

    [messages] = provider.calls
    assert estimate_tokens(messages) <= 2_000
