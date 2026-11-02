"""ops/observability/dashboards: the committed JSON is what build.py generates, and
every dashboard follows the rules of docs/observability.md § Dashboards."""

import re
from typing import Any

import pytest

from ops.observability.dashboards.build import (
    DASHBOARDS,
    Dashboard,
    dashboard_json,
    path_of,
    render,
)


@pytest.mark.parametrize("dashboard", DASHBOARDS, ids=lambda d: d.uid)
def test_the_committed_json_is_current(dashboard: Dashboard) -> None:
    assert path_of(dashboard).read_text(encoding="utf-8") == render(dashboard), (
        "stale: run uv run python -m ops.observability.dashboards.build"
    )


def test_the_four_dashboards_of_the_spec_exist_with_unique_uids() -> None:
    assert [d.title for d in DASHBOARDS] == [
        "DMC-268 Overview",
        "DMC-268 API",
        "DMC-268 Pipeline",
        "DMC-268 Infrastructure",
    ]
    assert len({d.uid for d in DASHBOARDS}) == len(DASHBOARDS)


@pytest.mark.parametrize("dashboard", DASHBOARDS, ids=lambda d: d.uid)
def test_every_dashboard_shows_deploys_and_queries_the_prometheus_variable(
    dashboard: Dashboard,
) -> None:
    data = dashboard_json(dashboard)

    deploys = [a for a in data["annotations"]["list"] if a["name"] == "Deploys"]
    assert deploys and deploys[0]["target"]["tags"] == ["deploy"]
    for panel in data["panels"]:
        assert panel["gridPos"]["x"] + panel["gridPos"]["w"] <= 24
        for target in panel.get("targets", []):
            assert target["datasource"]["uid"] == "${prometheus}"


@pytest.mark.parametrize("dashboard", DASHBOARDS, ids=lambda d: d.uid)
def test_no_panel_reads_a_per_tenant_label(dashboard: Dashboard) -> None:
    exprs: list[Any] = [
        t["expr"] for p in dashboard_json(dashboard)["panels"] for t in p.get("targets", [])
    ]

    for expr in exprs:
        assert not re.search(r"\b(account_id|installation_id|repo_id|job_id)\b", expr), expr
