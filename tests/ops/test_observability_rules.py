"""ops/observability/alerts: what promtool cannot check. promtool (ci.yml, job
alert-rules) tests each rule's behaviour; these tests hold the files to the spec's
alert table and to the labels and runbooks every rule needs (docs/observability.md §
Alerts)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
ALERTS = REPO / "ops" / "observability" / "alerts"
SPEC = REPO / "docs" / "observability.md"
RUNBOOKS = REPO / "docs" / "runbooks.md"
RUNBOOK_URL = "https://github.com/larchanka-training/dmc-268-api-t4/blob/main/docs/runbooks.md#"
DOMAINS = {"edge", "api", "worker", "db", "host", "monitoring", "business"}
#: Rows of § Alerts that a later phase writes: the SLO burn alerts (phase 6, todo 56).
LATER = {"SLO burn"}


def rules() -> list[dict[str, Any]]:
    found = []
    for path in sorted(ALERTS.glob("*.rules.yaml")):
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            found.extend({**rule, "_file": path.name} for rule in group["rules"])
    return found


def spec_alerts() -> list[str]:
    """The Alert column of the Grafana alert table in § Alerts."""
    text = SPEC.read_text(encoding="utf-8")
    table = text.split("| Alert | Condition | Suppressed while | Severity |", 1)[1]
    names = []
    for line in table.splitlines()[2:]:
        if not line.startswith("|"):
            break
        cell = line.split("|")[1].strip()
        names.append(re.sub(r"\s*\(root\)$", "", cell.replace("**", "")).strip())
    return names


def alert_name(spec_name: str) -> str:
    """The spec's "API error rate" is the rule's ApiErrorRate."""
    return "".join(word[0].upper() + word[1:].lower() for word in spec_name.split())


def anchors() -> set[str]:
    """GitHub's anchors for the runbook headings."""
    headings = re.findall(r"^#{2,3} (.+)$", RUNBOOKS.read_text(encoding="utf-8"), re.M)
    return {re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-") for heading in headings}


def test_every_alert_in_the_spec_has_a_rule_and_no_rule_is_extra() -> None:
    wanted = {alert_name(name) for name in spec_alerts() if name not in LATER}
    written = {rule["alert"] for rule in rules()}

    assert written == wanted, (
        f"missing: {sorted(wanted - written)}; not in the spec: {sorted(written - wanted)}"
    )


@pytest.mark.parametrize("rule", rules(), ids=lambda rule: f"{rule['_file']}:{rule['alert']}")
def test_every_rule_carries_domain_severity_owner_and_a_runbook(rule: dict[str, Any]) -> None:
    labels, annotations = rule["labels"], rule["annotations"]

    assert labels["domain"] in DOMAINS
    assert labels["domain"] == rule["_file"].removesuffix(".rules.yaml")
    assert labels["severity"] in {"critical", "warning"}
    assert labels["owner"]
    assert annotations["summary"]
    assert annotations["runbook_url"].startswith(RUNBOOK_URL)
    anchor = annotations["runbook_url"].removeprefix(RUNBOOK_URL)
    assert anchor in anchors(), f"docs/runbooks.md has no section #{anchor}"


def test_every_rules_file_has_a_promtool_test() -> None:
    for path in ALERTS.glob("*.rules.yaml"):
        test = ALERTS / "tests" / path.name.replace(".rules.yaml", ".test.yaml")
        assert test.exists(), f"{path.name} has no {test.name}"


def test_no_rule_reads_a_per_tenant_label() -> None:
    """Metrics carry no tenant ids (§ Metrics); a rule naming one would never match."""
    for rule in rules():
        assert not re.search(r"\b(account_id|installation_id|repo_id|job_id)\b", rule["expr"])
