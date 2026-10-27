from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

MAX_PAGE_SIZE = 100


def page_of(items: Sequence[T], page: int, size: int) -> list[T]:
    size = min(size, MAX_PAGE_SIZE)
    start = page * size
    return list(items[start : start + size + 1])
