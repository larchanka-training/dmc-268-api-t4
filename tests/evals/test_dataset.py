"""The committed gold dataset must satisfy the corpus contract of docs/TEST_PLAN.md § 4."""

import re
from pathlib import Path

import pytest

from domain.models import Category
from evals.loader import load_dataset
from evals.models import Case, Metrics

DATASET = Path(__file__).resolve().parents[2] / "test-prs-dataset"
# A real-looking key: the plan allows only fixture keys of the form sk-test-….
REAL_LOOKING_KEY = re.compile(r"sk-(?!test-)[A-Za-z0-9]{16,}")


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_dataset(DATASET)


def test_the_corpus_has_20_to_30_cases(cases: list[Case]) -> None:
    assert 20 <= len(cases) <= 30


def test_the_corpus_covers_security_correctness_and_a_third_category(cases: list[Case]) -> None:
    categories = {defect.category for case in cases for defect in case.expected}

    assert Category.SECURITY in categories
    assert Category.CORRECTNESS in categories
    assert len(categories) >= 3


def test_every_category_has_a_case(cases: list[Case]) -> None:
    categories = {defect.category for case in cases for defect in case.expected}

    assert categories == set(Category)


def test_there_is_a_clean_negative_control(cases: list[Case]) -> None:
    assert any(case.metrics is Metrics.NEGATIVE_CONTROL for case in cases)


def test_there_is_a_prompt_injection_case_that_still_expects_the_defect(
    cases: list[Case],
) -> None:
    injected = [case for case in cases if "prompt-injection" in case.tags]

    assert injected
    assert all(case.metrics is Metrics.DETECTION and case.expected for case in injected)


def test_there_is_a_schema_only_case(cases: list[Case]) -> None:
    assert any(case.metrics is Metrics.SCHEMA_ONLY for case in cases)


def test_no_fixture_holds_a_real_looking_key(cases: list[Case]) -> None:
    for case in cases:
        assert not REAL_LOOKING_KEY.search(case.read_diff()), case.id
        assert not REAL_LOOKING_KEY.search(case.title + case.description), case.id


def test_the_sql_injection_fixture_is_shared_with_the_unit_tests(cases: list[Case]) -> None:
    case = next(case for case in cases if case.id == "sec-sql-sort-injection")

    assert case.fixture == (DATASET.parent / "tests" / "fixtures" / "sql_injection.diff").resolve()
    assert case.expected[0].line == 16
