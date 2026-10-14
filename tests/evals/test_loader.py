from pathlib import Path

import pytest

from evals.loader import DatasetError, load_case, load_dataset
from evals.models import Metrics

DIFF = """\
diff --git a/app/users.py b/app/users.py
index 3f2a1c4..9b7e0d2 100644
--- a/app/users.py
+++ b/app/users.py
@@ -1,3 +1,4 @@
 import sqlite3
+import os
 
 DB = "app.db"
"""

GOOD_CASE = """\
id: case-a
intent: "detection: demo"
pattern: "demo"
metrics: detection
fixture: change.diff
title: "Demo"
description: ""
expected:
  - path: app/users.py
    line: 2
    side: new
    category: maintainability
    severity: low
"""


def write_case(root: Path, text: str = GOOD_CASE, diff: str = DIFF, name: str = "case-a") -> Path:
    case_dir = root / name
    case_dir.mkdir(parents=True)
    (case_dir / "case.yaml").write_text(text, encoding="utf-8")
    (case_dir / "change.diff").write_text(diff, encoding="utf-8")
    return case_dir


def test_a_well_formed_case_loads(tmp_path: Path) -> None:
    case = load_case(write_case(tmp_path))

    assert case.id == "case-a"
    assert case.metrics is Metrics.DETECTION
    assert case.expected[0].line == 2
    assert "import os" in case.read_diff()


def test_an_expected_line_that_is_context_is_rejected(tmp_path: Path) -> None:
    case_dir = write_case(tmp_path, GOOD_CASE.replace("line: 2", "line: 1"))

    with pytest.raises(DatasetError, match="not an added or removed line"):
        load_case(case_dir)


def test_an_expected_line_outside_the_diff_is_rejected(tmp_path: Path) -> None:
    case_dir = write_case(tmp_path, GOOD_CASE.replace("line: 2", "line: 40"))

    with pytest.raises(DatasetError, match="not an added or removed line"):
        load_case(case_dir)


def test_an_expected_path_outside_the_diff_is_rejected(tmp_path: Path) -> None:
    case_dir = write_case(tmp_path, GOOD_CASE.replace("path: app/users.py", "path: app/nope.py"))

    with pytest.raises(DatasetError, match="not an added or removed line"):
        load_case(case_dir)


def test_a_negative_control_with_expected_defects_is_rejected(tmp_path: Path) -> None:
    case_dir = write_case(tmp_path, GOOD_CASE.replace("detection\n", "negative control\n", 1))

    with pytest.raises(DatasetError, match="only a detection case"):
        load_case(case_dir)


def test_a_detection_case_without_expected_defects_is_rejected(tmp_path: Path) -> None:
    head = GOOD_CASE.split("expected:")[0]
    case_dir = write_case(tmp_path, head + "expected: []\n")

    with pytest.raises(DatasetError, match="at least one expected"):
        load_case(case_dir)


def test_a_malformed_diff_is_rejected_without_quoting_it(tmp_path: Path) -> None:
    secret = "hunter2-should-never-be-echoed"
    broken = DIFF.replace("@@ -1,3 +1,4 @@", "@@ -1,3 +1,9 @@") + f"+{secret}\n"
    case_dir = write_case(tmp_path, diff=broken)

    with pytest.raises(DatasetError, match="not a valid unified diff") as raised:
        load_case(case_dir)

    assert secret not in str(raised.value)


def test_the_id_must_match_the_directory(tmp_path: Path) -> None:
    case_dir = write_case(tmp_path, GOOD_CASE.replace("id: case-a", "id: other"))

    with pytest.raises(DatasetError, match="must equal the directory name"):
        load_case(case_dir)


def test_unknown_and_missing_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="unknown keys"):
        load_case(write_case(tmp_path / "a", GOOD_CASE + "surprise: 1\n"))
    with pytest.raises(DatasetError, match="missing keys"):
        load_case(write_case(tmp_path / "b", GOOD_CASE.replace('title: "Demo"\n', "")))


def test_unknown_enum_values_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="unknown side/category/severity"):
        load_case(write_case(tmp_path / "a", GOOD_CASE.replace("severity: low", "severity: nit")))
    with pytest.raises(DatasetError, match="metrics must be one of"):
        load_case(write_case(tmp_path / "b", GOOD_CASE.replace("metrics: detection", "metrics: x")))


def test_must_not_may_not_cover_an_expected_defect(tmp_path: Path) -> None:
    text = GOOD_CASE + "must_not:\n  - path: app/users.py\n    first_line: 1\n    last_line: 3\n"

    with pytest.raises(DatasetError, match="must_not covers expected"):
        load_case(write_case(tmp_path, text))


def test_must_not_path_must_be_in_the_diff(tmp_path: Path) -> None:
    text = GOOD_CASE + "must_not:\n  - path: app/nope.py\n"

    with pytest.raises(DatasetError, match="not in the diff"):
        load_case(write_case(tmp_path, text))


def test_a_duplicate_expected_defect_is_rejected(tmp_path: Path) -> None:
    entry = GOOD_CASE.split("expected:\n")[1]
    case_dir = write_case(tmp_path, GOOD_CASE + entry)

    with pytest.raises(DatasetError, match="duplicate expected"):
        load_case(case_dir)


def test_an_empty_dataset_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="no case directories"):
        load_dataset(tmp_path)


def test_load_dataset_returns_cases_in_id_order(tmp_path: Path) -> None:
    for name in ("case-b", "case-a"):
        write_case(tmp_path, GOOD_CASE.replace("case-a", name), name=name)

    assert [case.id for case in load_dataset(tmp_path)] == ["case-a", "case-b"]
