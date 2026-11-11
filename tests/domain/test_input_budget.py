import pytest

from domain.input_budget import FileCost, fit_to_budget, is_doc_path, is_test_path


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_users.py",
        "app/tests/helpers.py",
        "src/__tests__/Button.tsx",
        "pkg/users/user_test.go",
        "web/src/api.spec.ts",
        "web/src/Button.test.tsx",
        "test_main.py",
        "conftest.py",
        "spec/models/user_spec.rb",
    ],
)
def test_test_files_are_recognised(path: str) -> None:
    assert is_test_path(path)


@pytest.mark.parametrize(
    "path", ["app/users.py", "src/contest.ts", "app/attestation.py", "docs/testing.md", "latest.go"]
)
def test_code_and_docs_are_not_test_files(path: str) -> None:
    assert not is_test_path(path)


def test_markdown_and_rst_are_docs() -> None:
    assert is_doc_path("README.md")
    assert is_doc_path("docs/guide.RST")
    assert not is_doc_path("docs/conf.py")


def test_nothing_is_dropped_when_everything_fits() -> None:
    files = [FileCost("app/a.py", 40), FileCost("tests/test_a.py", 60)]

    fit = fit_to_budget(files, budget=100)

    assert fit.kept == ("app/a.py", "tests/test_a.py")
    assert fit.dropped == ()


def test_tests_go_first_then_docs_then_the_largest_file() -> None:
    files = [
        FileCost("app/small.py", 10),
        FileCost("app/large.py", 50),
        FileCost("README.md", 5),
        FileCost("tests/test_small.py", 5),
        FileCost("app/medium.py", 30),
    ]

    assert fit_to_budget(files, budget=95).dropped == ("tests/test_small.py",)
    assert fit_to_budget(files, budget=90).dropped == ("README.md", "tests/test_small.py")
    assert fit_to_budget(files, budget=89).dropped == (
        "app/large.py",
        "README.md",
        "tests/test_small.py",
    )
    assert fit_to_budget(files, budget=10).kept == ("app/small.py",)


def test_the_largest_test_goes_before_a_smaller_one() -> None:
    files = [FileCost("tests/test_a.py", 10), FileCost("tests/test_b.py", 70), FileCost("a.py", 20)]

    fit = fit_to_budget(files, budget=30)

    assert fit.dropped == ("tests/test_b.py",)


def test_a_small_test_is_dropped_before_a_large_source_file() -> None:
    files = [FileCost("app/huge.py", 900), FileCost("tests/test_tiny.py", 1)]

    fit = fit_to_budget(files, budget=900)

    assert fit.dropped == ("tests/test_tiny.py",)


def test_everything_is_dropped_when_no_file_fits() -> None:
    files = [FileCost("app/a.py", 40), FileCost("app/b.py", 60)]

    assert fit_to_budget(files, budget=30).kept == ()
    assert fit_to_budget(files, budget=-5).kept == ()
