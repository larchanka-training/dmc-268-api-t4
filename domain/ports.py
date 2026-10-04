from dataclasses import dataclass
from typing import Protocol

from domain.models import ReviewResult


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    diff_text: str
    pr_title: str
    pr_description: str


class LLMGateway(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResult: ...
