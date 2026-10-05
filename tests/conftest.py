import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from adapters.llm.keys import KeyPool
from adapters.llm.openai_compat import OpenAICompatibleProvider

FAKE_KEY_A = "sk-test-aaaaaaaaaaaaaaaa1111"
FAKE_KEY_B = "sk-test-bbbbbbbbbbbbbbbb2222"
FAKE_KEY_C = "sk-test-cccccccccccccccc3333"

VALID_FINDING: dict[str, Any] = {
    "path": "app/users.py",
    "line": 16,
    "side": "new",
    "severity": "critical",
    "category": "security",
    "message": "The sort parameter is interpolated into SQL; whitelist the column names.",
    "suggestion": {
        "before": ['    rows = conn.execute(f"SELECT ... ORDER BY {sort}").fetchall()'],
        "after": [
            '    rows = conn.execute(f"SELECT ... ORDER BY {SORT_COLUMNS[sort]}").fetchall()'
        ],
    },
}


def review_json(*findings: dict[str, Any], summary: str = "Adds a users endpoint.") -> str:
    return json.dumps({"summary": summary, "findings": list(findings)})


def chat_response(content: str | None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


Handler = Callable[[httpx.Request], httpx.Response]


class RecordingTransport(httpx.MockTransport):
    """MockTransport that keeps every request it served."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []

        def recording_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(recording_handler)


def replies(*responses: httpx.Response | Exception) -> Handler:
    """Serve the given responses in order; an exception instance is raised instead."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def make_provider(
    transport: httpx.AsyncBaseTransport,
    *,
    name: str = "eurouter",
    keys: tuple[str, ...] = (FAKE_KEY_A,),
    clock: FakeClock | None = None,
    json_mode: bool = True,
) -> OpenAICompatibleProvider:
    pool = KeyPool(name, keys, clock or FakeClock()) if keys else None
    return OpenAICompatibleProvider(
        name=name,
        base_url="https://llm.example.test/api/v1",
        model=f"{name}-model",
        key_pool=pool,
        temperature=0.0,
        json_mode=json_mode,
        timeout_seconds=5.0,
        max_tokens=1024,
        transport=transport,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
