import pytest

from domain.repositories import (
    PAGE_SIZE,
    Repository,
    connect_url,
    installation_settings_url,
    page,
    sort_by_full_name,
)
from domain.tenancy import AccountType
from tests.conftest import APP_SLUG, make_organization, make_repository


def names(repositories: tuple[Repository, ...]) -> list[str]:
    return [repository.full_name for repository in repositories]


# --- sorting ------------------------------------------------------------


def test_sorts_by_owner_then_name_ignoring_case() -> None:
    """The same case as the console's sortByFullName test, so both sides agree."""
    unsorted = (
        make_repository("acme", "web"),
        make_repository("Acme", "API"),
        make_repository("beta", "app"),
        make_repository("acme", "billing"),
    )

    assert names(sort_by_full_name(unsorted)) == [
        "Acme/API",
        "acme/billing",
        "acme/web",
        "beta/app",
    ]


def test_sorting_leaves_the_input_alone() -> None:
    unsorted = (make_repository("zulu", "a"), make_repository("alpha", "b"))

    sort_by_full_name(unsorted)

    assert names(unsorted) == ["zulu/a", "alpha/b"]


def test_full_name_is_owner_slash_name() -> None:
    assert make_repository("acme", "payments").full_name == "acme/payments"


def test_an_empty_owner_or_name_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        Repository(id="1", owner=" ", name="x", private=False, default_branch="main", html_url="")


# --- paging -------------------------------------------------------------


def many(count: int) -> tuple[Repository, ...]:
    return tuple(make_repository("acme", f"repo-{index:02d}") for index in range(count))


def test_the_first_page_holds_ten_and_points_at_the_next() -> None:
    result = page(many(25))

    assert len(result.items) == PAGE_SIZE
    assert result.items[0].name == "repo-00"
    assert result.next_cursor == "10"
    assert result.total_count == 25


def test_the_cursor_is_an_offset() -> None:
    result = page(many(25), "10")

    assert result.items[0].name == "repo-10"
    assert result.next_cursor == "20"
    assert result.total_count == 25


def test_the_last_page_has_no_next_cursor() -> None:
    result = page(many(25), "20")

    assert len(result.items) == 5
    assert result.next_cursor is None
    assert result.total_count == 25


def test_total_count_covers_every_page_not_just_this_one() -> None:
    assert page(many(25), "20").total_count == 25


def test_an_exact_multiple_of_the_page_size_ends_cleanly() -> None:
    assert page(many(20), "10").next_cursor is None


def test_an_offset_past_the_end_is_an_empty_page() -> None:
    result = page(many(5), "100")

    assert result.items == ()
    assert result.next_cursor is None
    assert result.total_count == 5


def test_an_empty_list_pages_to_nothing() -> None:
    result = page(())

    assert result.items == ()
    assert result.next_cursor is None
    assert result.total_count == 0


@pytest.mark.parametrize("cursor", ["-1", "abc", "1.5", "1e3", " 1", "0x10", "١٠"])
def test_a_malformed_cursor_raises(cursor: str) -> None:
    with pytest.raises(ValueError, match="cursor must be"):
        page(many(5), cursor)


@pytest.mark.parametrize("cursor", [None, ""])
def test_no_cursor_means_the_first_page(cursor: str | None) -> None:
    assert page(many(5), cursor).items[0].name == "repo-00"


# --- connect URL --------------------------------------------------------


def test_an_organisation_goes_to_its_installation_settings() -> None:
    organization = make_organization("acme", "100", installation_id=42)

    assert connect_url(organization, APP_SLUG) == (
        "https://github.com/organizations/acme/settings/installations/42"
    )


def test_a_personal_account_goes_to_the_users_own_settings() -> None:
    organization = make_organization(
        "octocat", "1", installation_id=7, account_type=AccountType.USER
    )

    assert connect_url(organization, APP_SLUG) == "https://github.com/settings/installations/7"


def test_no_organisation_goes_to_the_apps_install_page() -> None:
    assert connect_url(None, APP_SLUG) == (f"https://github.com/apps/{APP_SLUG}/installations/new")


# --- the settings page both actions share -------------------------------


def test_the_settings_url_is_the_same_page_connect_points_at() -> None:
    """GitHub has one page per installation, not one per repository, so connect
    and disconnect lead to the same place."""
    organization = make_organization("acme", "100", installation_id=42)

    assert installation_settings_url(organization) == connect_url(organization, APP_SLUG)


def test_the_settings_url_for_a_personal_account() -> None:
    organization = make_organization(
        "octocat", "1", installation_id=7, account_type=AccountType.USER
    )

    assert installation_settings_url(organization) == (
        "https://github.com/settings/installations/7"
    )
