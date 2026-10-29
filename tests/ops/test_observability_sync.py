"""ops/observability/sync.py against a fake Grafana and Synthetic Monitoring on
``httpx.MockTransport`` (AGENTS.md hard rule 4)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import yaml

from ops.observability import sync
from ops.observability.sync import Settings, SyncError, run

TOKEN = "glsa_" + "S3cr3t" * 6
SM_TOKEN = "sm-" + "T0k3n" * 6
NAMESPACE = "/api/convert/prometheus/config/v1/rules/DMC-268%20alerts"


@dataclass
class FakeGrafana:
    """Answers like Grafana; ``state`` says what already exists."""

    folder: bool = False
    contact_point: bool = False
    mute_timings: set[str] = field(default_factory=set)
    rule_groups: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    probes: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"id": 1, "name": "Frankfurt"},
            {"id": 2, "name": "London"},
            {"id": 3, "name": "Paris"},
            {"id": 4, "name": "Ohio"},
        ]
    )
    fail: Callable[[httpx.Request], int | None] = lambda _: None
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.fail(request)
        if status is not None:
            return httpx.Response(status, json={"message": "nope"})
        method, path = request.method, request.url.raw_path.decode().split("?")[0]
        if method == "GET":
            return self._get(path, request)
        return httpx.Response(200, json={})

    def _get(self, path: str, request: httpx.Request) -> httpx.Response:
        if path == "/api/folders/dmc268":
            return httpx.Response(200 if self.folder else 404, json={})
        if path == "/api/v1/provisioning/contact-points":
            return httpx.Response(200, json=[{"name": "telegram"}] if self.contact_point else [])
        if path.startswith("/api/v1/provisioning/mute-timings/"):
            found = path.rsplit("/", 1)[1] in self.mute_timings
            return httpx.Response(200 if found else 404, json={})
        if path == NAMESPACE:
            groups = [{"name": name} for name in self.rule_groups]
            return httpx.Response(200, json={"DMC-268 alerts": groups})
        if path == "/api/v1/probe/list":
            return httpx.Response(200, json=self.probes)
        if path == "/api/v1/check/list":
            return httpx.Response(200, json=self.checks)
        return httpx.Response(404, json={})

    def writes(self) -> list[tuple[str, str]]:
        return [
            (r.method, r.url.raw_path.decode().split("?")[0])
            for r in self.requests
            if r.method != "GET"
        ]

    def body(self, method: str, path: str) -> Any:
        [request] = [
            r
            for r in self.requests
            if r.method == method and r.url.raw_path.decode().split("?")[0] == path
        ]
        return json.loads(request.content)


SETTINGS = Settings(
    grafana_url="https://stack.grafana.net",
    grafana_token=TOKEN,
    prom_datasource_uid="grafanacloud-prom",
    sentry_org_url="https://acme.de.sentry.io",
    sm_url="https://synthetic-monitoring-api.grafana.net",
    sm_token=SM_TOKEN,
    site_address="1-2-3-4.sslip.io",
)


def sync_with(grafana: FakeGrafana, *parts: str, settings: Settings = SETTINGS) -> list[str]:
    lines: list[str] = []
    run(settings, parts or sync.PARTS, transport=httpx.MockTransport(grafana), report=lines.append)
    return lines


def test_settings_come_from_the_environment_and_hide_the_tokens() -> None:
    settings = Settings.from_env(
        {"GRAFANA_URL": "https://stack.grafana.net/", "GRAFANA_SA_TOKEN": TOKEN}
    )

    assert settings.grafana_url == "https://stack.grafana.net"
    assert settings.prom_datasource_uid == "grafanacloud-prom"
    assert TOKEN not in repr(settings)


def test_dashboards_go_into_their_folder_and_overwrite() -> None:
    grafana = FakeGrafana()

    sync_with(grafana, "dashboards")

    assert grafana.writes()[0] == ("POST", "/api/folders")
    pushes = [r for r in grafana.requests if r.url.path == "/api/dashboards/db"]
    assert len(pushes) == 4
    for request in pushes:
        body = json.loads(request.content)
        assert body["folderUid"] == "dmc268" and body["overwrite"] is True
        assert "id" not in body["dashboard"]
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    api = next(
        json.loads(r.content)["dashboard"]
        for r in pushes
        if json.loads(r.content)["dashboard"]["uid"] == "dmc268-api"
    )
    [sentry] = [v for v in api["templating"]["list"] if v["name"] == "sentry_url"]
    assert sentry["query"] == "https://acme.de.sentry.io"


def test_an_existing_folder_is_not_created_again() -> None:
    grafana = FakeGrafana(folder=True)

    sync_with(grafana, "dashboards")

    assert ("POST", "/api/folders") not in grafana.writes()


def test_rules_are_imported_group_by_group_and_a_removed_group_is_deleted() -> None:
    grafana = FakeGrafana(rule_groups=["api", "retired"])

    sync_with(grafana, "rules")

    imports = [r for r in grafana.requests if r.method == "POST"]
    groups = [yaml.safe_load(r.content) for r in imports]
    assert [g["name"] for g in groups] == [
        "api", "business", "db", "edge", "host", "monitoring", "worker",
    ]  # fmt: skip
    assert all(
        r.headers["X-Grafana-Alerting-Datasource-UID"] == "grafanacloud-prom" for r in imports
    )
    assert all(r.headers["Content-Type"] == "application/yaml" for r in imports)
    assert sum(len(g["rules"]) for g in groups) == 24
    assert ("DELETE", f"{NAMESPACE}/retired") in grafana.writes()
    assert ("DELETE", f"{NAMESPACE}/api") not in grafana.writes()


def test_the_contact_point_is_created_once_with_placeholders_and_editable() -> None:
    grafana = FakeGrafana()

    sync_with(grafana, "alerting")

    [request] = [
        r for r in grafana.requests if r.method == "POST" and r.url.path.endswith("contact-points")
    ]
    body = json.loads(request.content)
    assert body["name"] == "telegram" and body["type"] == "telegram"
    assert body["settings"]["bottoken"].startswith("placeholder")
    assert request.headers["X-Disable-Provenance"] == "true"


def test_an_existing_contact_point_is_left_alone() -> None:
    """The real bot token is typed into Grafana by hand; sync must never overwrite it."""
    grafana = FakeGrafana(contact_point=True)

    lines = sync_with(grafana, "alerting")

    assert not [w for w in grafana.writes() if w[1].endswith("contact-points")]
    assert "  contact point telegram exists, left as it is" in lines


def test_the_policy_groups_by_domain_and_mutes_the_two_working_hours_rules() -> None:
    grafana = FakeGrafana(mute_timings={"canary-quiet-hours"})

    sync_with(grafana, "alerting")

    policy = grafana.body("PUT", "/api/v1/provisioning/policies")
    assert policy["receiver"] == "telegram"
    assert policy["group_by"] == ["domain"]
    assert (policy["group_wait"], policy["group_interval"], policy["repeat_interval"]) == (
        "30s",
        "5m",
        "4h",
    )
    assert {r["object_matchers"][0][2]: r["mute_time_intervals"] for r in policy["routes"]} == {
        "NoThroughput": ["no-throughput-quiet-hours"],
        "CanaryReviewStale": ["canary-quiet-hours"],
    }
    # A missing timing is created, an existing one updated.
    created = grafana.body("POST", "/api/v1/provisioning/mute-timings")
    assert created["name"] == "no-throughput-quiet-hours"
    assert ("PUT", "/api/v1/provisioning/mute-timings/canary-quiet-hours") in grafana.writes()


def test_a_mute_timing_mutes_everything_outside_its_window() -> None:
    timing = sync.mute_timing("x", "13:00", "19:00")

    weekdays, weekend = timing["time_intervals"]
    assert weekdays["times"] == [
        {"start_time": "00:00", "end_time": "13:00"},
        {"start_time": "19:00", "end_time": "24:00"},
    ]
    assert weekdays["weekdays"] == ["monday:friday"]
    assert weekend["weekdays"] == ["saturday", "sunday"]
    assert {weekdays["location"], weekend["location"]} == {"Europe/Berlin"}


def test_the_uptime_check_is_added_with_its_probes_resolved() -> None:
    grafana = FakeGrafana()

    sync_with(grafana, "synthetics")

    check = grafana.body("POST", "/api/v1/check/add")
    assert check["job"] == "dmc268-site"
    assert check["target"] == "https://1-2-3-4.sslip.io/api/health"
    assert check["probes"] == [1, 2, 3]
    assert check["frequency"] == 120_000
    sent = [r for r in grafana.requests if r.url.path.startswith("/api/v1/")]
    assert all(r.headers["Authorization"] == f"Bearer {SM_TOKEN}" for r in sent)


def test_an_existing_uptime_check_is_updated_in_place() -> None:
    grafana = FakeGrafana(checks=[{"id": 77, "tenantId": 9, "job": "dmc268-site"}])

    sync_with(grafana, "synthetics")

    update = grafana.body("POST", "/api/v1/check/update")
    assert (update["id"], update["tenantId"]) == (77, 9)
    assert ("POST", "/api/v1/check/add") not in grafana.writes()


def test_an_unknown_probe_location_names_the_available_ones() -> None:
    grafana = FakeGrafana(probes=[{"id": 4, "name": "Ohio"}])

    with pytest.raises(SyncError, match="Frankfurt.*available: \\['Ohio'\\]"):
        sync_with(grafana, "synthetics")


def test_synthetics_are_skipped_without_their_settings() -> None:
    grafana = FakeGrafana()
    settings = Settings(grafana_url="https://stack.grafana.net")

    lines = sync_with(grafana, "synthetics", settings=settings)

    assert grafana.requests == []
    assert lines[-1] == "  skipped: GRAFANA_SM_URL, GRAFANA_SM_TOKEN, SITE_ADDRESS not set"


def test_a_dry_run_reads_but_writes_nothing() -> None:
    grafana = FakeGrafana()

    lines = sync_with(grafana, "dashboards", "rules", "alerting")

    assert grafana.writes()  # the real run writes
    dry = FakeGrafana()
    run(SETTINGS, ("dashboards", "rules", "alerting"), transport=httpx.MockTransport(dry),
        dry_run=True, report=lines.append)  # fmt: skip
    assert dry.writes() == []
    assert any(line.startswith("  dry run: POST grafana/api/dashboards/db") for line in lines)


def test_an_api_error_names_the_call_and_status_but_never_the_token() -> None:
    grafana = FakeGrafana(fail=lambda r: 401 if r.url.path == "/api/dashboards/db" else None)

    with pytest.raises(SyncError) as raised:
        sync_with(grafana, "dashboards")

    assert "POST grafana/api/dashboards/db answered HTTP 401" in str(raised.value)
    assert TOKEN not in str(raised.value)


def test_without_a_grafana_url_the_sync_stops_at_once() -> None:
    with pytest.raises(SyncError, match="GRAFANA_URL is not set"):
        run(Settings(), ("dashboards",), report=lambda _: None)


def test_without_a_token_no_authorization_header_is_sent() -> None:
    """The lab's Grafana lets anonymous users in (deploy/lab/compose.yml)."""
    grafana = FakeGrafana(folder=True)

    sync_with(grafana, "dashboards", settings=Settings(grafana_url="http://127.0.0.1:3000"))

    assert all("Authorization" not in r.headers for r in grafana.requests)


def test_main_reports_a_failure_on_stderr_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    monkeypatch.setenv("GRAFANA_SA_TOKEN", TOKEN)

    assert sync.main(["--only", "dashboards"]) == 1

    error = capsys.readouterr().err
    assert "GRAFANA_URL is not set" in error
    assert TOKEN not in error


def test_main_refuses_an_unknown_part() -> None:
    with pytest.raises(SystemExit):
        sync.main(["--only", "dashbords"])
