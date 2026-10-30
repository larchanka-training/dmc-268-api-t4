"""forge_rate_limit_remaining_ratio (docs/observability.md § Metrics): GitHub's rate-limit
headers from every App and installation client, the lowest of the last 5 minutes."""

import ast
from pathlib import Path

import httpx
import pytest

from adapters.github.rate_limit import RATE_LIMIT_HOOKS
from adapters.observability import metrics
from adapters.observability.metrics import RateLimitGauge

GITHUB = Path(__file__).resolve().parents[2] / "adapters" / "github"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_gauge_is_the_lowest_ratio_of_the_window_per_provider() -> None:
    clock = Clock()
    gauge = RateLimitGauge(window=300, clock=clock)
    gauge.observe("github", 4000, 5000)
    gauge.observe("github", 500, 5000)
    gauge.observe("gitlab", 1800, 2000)

    assert gauge.lowest() == {"github": 0.1, "gitlab": 0.9}

    clock.now += 301
    gauge.observe("github", 4900, 5000)
    assert gauge.lowest() == {"github": 0.98}


def test_without_a_sample_in_the_window_the_gauge_is_absent() -> None:
    clock = Clock()
    gauge = RateLimitGauge(window=300, clock=clock)
    gauge.observe("github", 10, 5000)
    clock.now += 301

    assert list(gauge.collect()) == []


@pytest.fixture
def gauge(monkeypatch: pytest.MonkeyPatch) -> RateLimitGauge:
    fresh = RateLimitGauge()
    monkeypatch.setattr(metrics, "FORGE_RATE_LIMIT", fresh)
    return fresh


async def get_with(headers: dict[str, str]) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, json={}))
    async with httpx.AsyncClient(transport=transport, event_hooks=RATE_LIMIT_HOOKS) as client:
        await client.get("https://api.github.example/repos/acme/web")


async def test_a_github_response_feeds_the_gauge(gauge: RateLimitGauge) -> None:
    await get_with({"X-RateLimit-Remaining": "250", "X-RateLimit-Limit": "5000"})

    assert gauge.lowest() == {"github": 0.05}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-RateLimit-Remaining": "250"},
        {"X-RateLimit-Remaining": "x", "X-RateLimit-Limit": "5000"},
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "0"},
        # Search has a budget of its own: its ratio says nothing about reviews.
        {"X-RateLimit-Remaining": "1", "X-RateLimit-Limit": "30", "X-RateLimit-Resource": "search"},
    ],
)
async def test_other_responses_leave_the_gauge_alone(
    gauge: RateLimitGauge, headers: dict[str, str]
) -> None:
    await get_with(headers)

    assert gauge.lowest() == {}


def test_every_github_client_carries_the_rate_limit_hook() -> None:
    """A client added later without the hook would hide its calls from the gauge."""
    missing = []
    for path in sorted(GITHUB.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "AsyncClient"
            ):
                hooks = [k for k in node.keywords if k.arg == "event_hooks"]
                if not hooks or ast.unparse(hooks[0].value) != "RATE_LIMIT_HOOKS":
                    missing.append(f"{path.name}:{node.lineno}")

    assert missing == []
