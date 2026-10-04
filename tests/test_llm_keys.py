import pytest

from adapters.llm.keys import KeyPool, mask_key
from domain.errors import ProviderUnavailableError
from tests.conftest import FAKE_KEY_A, FAKE_KEY_B, FAKE_KEY_C, FakeClock


def test_round_robin_cycles_through_keys(clock: FakeClock) -> None:
    pool = KeyPool("eurouter", [FAKE_KEY_A, FAKE_KEY_B, FAKE_KEY_C], clock)

    issued = [pool.acquire() for _ in range(7)]

    assert issued == [FAKE_KEY_A, FAKE_KEY_B, FAKE_KEY_C] * 2 + [FAKE_KEY_A]


def test_cooled_down_key_is_skipped_until_cooldown_ends(clock: FakeClock) -> None:
    pool = KeyPool("eurouter", [FAKE_KEY_A, FAKE_KEY_B], clock)
    pool.cool_down(FAKE_KEY_A, 30)

    clock.now += 29.9
    assert {pool.acquire() for _ in range(4)} == {FAKE_KEY_B}

    clock.now += 0.1
    assert FAKE_KEY_A in {pool.acquire() for _ in range(2)}


def test_disabled_key_never_returns(clock: FakeClock) -> None:
    pool = KeyPool("eurouter", [FAKE_KEY_A, FAKE_KEY_B], clock)
    pool.disable(FAKE_KEY_A)

    clock.now += 10**9

    assert {pool.acquire() for _ in range(4)} == {FAKE_KEY_B}


def test_no_available_key_raises_provider_unavailable(clock: FakeClock) -> None:
    pool = KeyPool("eurouter", [FAKE_KEY_A, FAKE_KEY_B], clock)
    pool.cool_down(FAKE_KEY_A, 60)
    pool.disable(FAKE_KEY_B)

    with pytest.raises(ProviderUnavailableError, match="eurouter"):
        pool.acquire()


def test_duplicate_keys_are_pooled_once(clock: FakeClock) -> None:
    pool = KeyPool("eurouter", [FAKE_KEY_A, FAKE_KEY_A, FAKE_KEY_B], clock)

    assert len(pool) == 2


def test_empty_pool_is_rejected(clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        KeyPool("eurouter", [], clock)


@pytest.mark.parametrize(
    ("key", "masked"),
    [("sk-or-v1-0123456789abcd", "sk-…abcd"), ("shortkey", "…ey")],
)
def test_mask_key_hides_the_middle(key: str, masked: str) -> None:
    assert mask_key(key) == masked
