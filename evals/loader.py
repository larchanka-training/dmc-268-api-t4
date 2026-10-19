"""Load and validate the gold dataset. Errors name the case and the reason, never diff text."""

from pathlib import Path
from typing import Any

import yaml

from domain.errors import DiffFormatError
from domain.models import Category, DiffSide, Severity
from evals.diffinfo import changed_lines
from evals.models import Case, CleanRegion, ExpectedDefect, Metrics

CASE_FILE = "case.yaml"
_CASE_KEYS = {
    "id",
    "intent",
    "pattern",
    "metrics",
    "fixture",
    "title",
    "description",
    "expected",
    "must_not",
    "tags",
}
_REQUIRED_KEYS = {"id", "intent", "pattern", "metrics", "fixture", "title", "expected"}
_EXPECTED_KEYS = {"path", "line", "side", "category", "severity"}
_MUST_NOT_KEYS = {"path", "first_line", "last_line"}


class DatasetError(ValueError):
    """A case file is malformed or contradicts its diff."""


def load_dataset(root: Path) -> list[Case]:
    case_dirs = sorted(path for path in root.iterdir() if path.is_dir())
    if not case_dirs:
        raise DatasetError(f"no case directories under {root}")
    cases = [load_case(path) for path in case_dirs]
    ids = [case.id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise DatasetError(f"duplicate case ids: {', '.join(duplicates)}")
    return cases


def load_case(case_dir: Path) -> Case:
    where = case_dir.name
    case_file = case_dir / CASE_FILE
    try:
        raw = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetError(f"{where}: cannot read {CASE_FILE}: {exc.strerror}") from None
    except yaml.YAMLError:
        raise DatasetError(f"{where}: {CASE_FILE} is not valid YAML") from None
    if not isinstance(raw, dict):
        raise DatasetError(f"{where}: {CASE_FILE} must hold a mapping")

    unknown = set(raw) - _CASE_KEYS
    if unknown:
        raise DatasetError(f"{where}: unknown keys {sorted(unknown)}")
    missing = _REQUIRED_KEYS - set(raw)
    if missing:
        raise DatasetError(f"{where}: missing keys {sorted(missing)}")
    if raw["id"] != where:
        raise DatasetError(f"{where}: id {raw['id']!r} must equal the directory name")

    for key in ("intent", "pattern", "title"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise DatasetError(f"{where}: {key} must be a non-empty string")
    description = raw.get("description") or ""
    if not isinstance(description, str):
        raise DatasetError(f"{where}: description must be a string")

    try:
        metrics = Metrics(raw["metrics"])
    except ValueError:
        raise DatasetError(
            f"{where}: metrics must be one of {[m.value for m in Metrics]}"
        ) from None

    fixture = (case_dir / str(raw["fixture"])).resolve()
    expected = tuple(_expected(where, item) for item in _list(where, raw, "expected"))
    must_not = tuple(_clean_region(where, item) for item in _list(where, raw, "must_not"))
    tags = frozenset(str(tag) for tag in _list(where, raw, "tags"))

    case = Case(
        id=where,
        intent=raw["intent"].strip(),
        pattern=raw["pattern"].strip(),
        metrics=metrics,
        fixture=fixture,
        title=raw["title"],
        description=description,
        expected=expected,
        must_not=must_not,
        tags=tags,
    )
    _check_against_diff(case)
    return case


def _list(where: str, raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key) or []
    if not isinstance(value, list):
        raise DatasetError(f"{where}: {key} must be a list")
    return value


def _expected(where: str, item: Any) -> ExpectedDefect:
    if not isinstance(item, dict) or set(item) != _EXPECTED_KEYS:
        raise DatasetError(f"{where}: each expected entry needs exactly {sorted(_EXPECTED_KEYS)}")
    line = item["line"]
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise DatasetError(f"{where}: expected line must be an integer >= 1")
    try:
        return ExpectedDefect(
            path=str(item["path"]),
            line=line,
            side=DiffSide(item["side"]),
            category=Category(item["category"]),
            severity=Severity(item["severity"]),
        )
    except ValueError:
        raise DatasetError(
            f"{where}: expected entry has an unknown side/category/severity"
        ) from None


def _clean_region(where: str, item: Any) -> CleanRegion:
    if not isinstance(item, dict) or "path" not in item or set(item) - _MUST_NOT_KEYS:
        raise DatasetError(f"{where}: each must_not entry takes {sorted(_MUST_NOT_KEYS)}")
    first, last = item.get("first_line"), item.get("last_line")
    if (first is None) != (last is None):
        raise DatasetError(f"{where}: must_not needs both first_line and last_line, or neither")
    if first is not None and (
        not isinstance(first, int) or not isinstance(last, int) or first > last
    ):
        raise DatasetError(f"{where}: must_not range must be integers with first_line <= last_line")
    return CleanRegion(path=str(item["path"]), first_line=first, last_line=last)


def _check_against_diff(case: Case) -> None:
    where = case.id
    if case.metrics is Metrics.DETECTION and not case.expected:
        raise DatasetError(f"{where}: a detection case needs at least one expected defect")
    if case.metrics is not Metrics.DETECTION and case.expected:
        raise DatasetError(f"{where}: only a detection case may list expected defects")

    try:
        diff_text = case.read_diff()
    except OSError as exc:
        raise DatasetError(f"{where}: cannot read fixture: {exc.strerror}") from None
    try:
        changed = changed_lines(diff_text)
    except DiffFormatError as exc:
        raise DatasetError(f"{where}: fixture is not a valid unified diff ({exc})") from None
    if not changed.paths:
        raise DatasetError(f"{where}: fixture holds no file diff")

    seen: set[tuple[str, int, DiffSide, Category]] = set()
    for defect in case.expected:
        if defect.key in seen:
            raise DatasetError(f"{where}: duplicate expected defect at {defect.path}:{defect.line}")
        seen.add(defect.key)
        if not changed.contains(defect.path, defect.line, defect.side):
            raise DatasetError(
                f"{where}: expected {defect.path}:{defect.line} ({defect.side.value}) is not an "
                "added or removed line of the diff"
            )
    for region in case.must_not:
        if region.path not in changed.paths:
            raise DatasetError(f"{where}: must_not path {region.path} is not in the diff")
        for defect in case.expected:
            if region.covers(defect.path, defect.line):
                raise DatasetError(
                    f"{where}: must_not covers expected defect {defect.path}:{defect.line}"
                )
