"""Push the dashboards, alert rules, alert routing and synthetic check to Grafana.

docs/observability.md § Dashboards, § Alerts, § Synthetic checks. Idempotent: run it
again and nothing changes. Run by .github/workflows/observability-sync.yml (manual), or
by hand against the lab (deploy/lab/README.md):

    python -m ops.observability.sync                      # everything
    python -m ops.observability.sync --only dashboards,rules
    python -m ops.observability.sync --dry-run            # print the writes, make none

Settings, from the environment:

- ``GRAFANA_URL``, ``GRAFANA_SA_TOKEN``: the stack and its service account (dashboards,
  rules, alerting). Without a token no Authorization header is sent (the lab's Grafana
  lets anonymous users in).
- ``GRAFANA_PROM_DATASOURCE_UID``: the Prometheus data source the rules query, default
  ``grafanacloud-prom``.
- ``SENTRY_ORG_URL``: optional, the Sentry link on the API dashboard.
- ``GRAFANA_SM_URL``, ``GRAFANA_SM_TOKEN``, ``SITE_ADDRESS``: Synthetic Monitoring's API
  and the site to probe. Without them the synthetic check is skipped with a warning.

Rules go through Grafana's import of Prometheus-format rules, so they become
Grafana-managed rules routed by the notification policy below to the ``telegram``
contact point. ⚠️ That path waits for the checklist's "Rule routing check" in Grafana
Cloud. The contact point is created once with placeholder values and never touched
again: the real bot token and chat id are typed into Grafana by hand.
"""

import argparse
import copy
import json
import os
import sys
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

HERE = Path(__file__).resolve().parent
DASHBOARDS = HERE / "dashboards"
ALERTS = HERE / "alerts"
UPTIME_CHECK = HERE / "synthetics" / "uptime.json"

FOLDER_UID = "dmc268"
FOLDER_TITLE = "DMC-268"
#: The rules' folder; the import API names it by title.
RULES_NAMESPACE = "DMC-268 alerts"
CONTACT_POINT = "telegram"
PLACEHOLDER = "placeholder: enter the bot token and chat id in Grafana"
DEFAULT_PROM_DATASOURCE_UID = "grafanacloud-prom"
PARTS = ("dashboards", "rules", "alerting", "synthetics")
TIMEOUT_SECONDS = 30.0

#: Working hours (Europe/Berlin, Mon-Fri) for the two rules notified only then. Each
#: window starts one lookback after 09:00, so a rule that looks back 4 h never reports
#: the night (docs/observability.md § Alerts). Outside it, notifications are muted.
QUIET_HOURS = {
    "NoThroughput": ("no-throughput-quiet-hours", "13:00", "19:00"),
    "CanaryReviewStale": ("canary-quiet-hours", "13:15", "23:15"),
}
LOCATION = "Europe/Berlin"


class SyncError(Exception):
    """A Grafana API call failed. The text names the call and the status, never a token."""


@dataclass
class Settings:
    grafana_url: str = ""
    grafana_token: str = field(default="", repr=False)
    prom_datasource_uid: str = DEFAULT_PROM_DATASOURCE_UID
    sentry_org_url: str = ""
    sm_url: str = ""
    sm_token: str = field(default="", repr=False)
    site_address: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env

        def text(name: str) -> str:
            return source.get(name, "").strip()

        return cls(
            grafana_url=text("GRAFANA_URL").rstrip("/"),
            grafana_token=text("GRAFANA_SA_TOKEN"),
            prom_datasource_uid=text("GRAFANA_PROM_DATASOURCE_UID") or DEFAULT_PROM_DATASOURCE_UID,
            sentry_org_url=text("SENTRY_ORG_URL").rstrip("/"),
            sm_url=text("GRAFANA_SM_URL").rstrip("/"),
            sm_token=text("GRAFANA_SM_TOKEN"),
            site_address=text("SITE_ADDRESS"),
        )


class Api:
    """One JSON API: Grafana's, or Synthetic Monitoring's."""

    def __init__(
        self,
        name: str,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        dry_run: bool = False,
        report: Callable[[str], None] = print,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=base_url, headers=headers, transport=transport, timeout=TIMEOUT_SECONDS
        )
        self._name = name
        self._dry_run = dry_run
        self._report = report

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, *, missing_ok: bool = False) -> Any:
        """The decoded body; None for a 404 when ``missing_ok``."""
        response = self._client.get(path, headers={"Accept": "application/json"})
        if missing_ok and response.status_code == 404:
            return None
        return self._decode(response, "GET", path)

    def send(
        self,
        method: str,
        path: str,
        *,
        json_body: object = None,
        content: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if self._dry_run:
            self._report(f"  dry run: {method} {self._name}{path}")
            return None
        response = self._client.request(
            method, path, json=json_body, content=content, headers=dict(headers or {})
        )
        return self._decode(response, method, path)

    def _decode(self, response: httpx.Response, method: str, path: str) -> Any:
        if response.status_code >= 400:
            # The body is Grafana's error message; it never echoes the token.
            raise SyncError(
                f"{method} {self._name}{path} answered HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text


# --- dashboards --------------------------------------------------------------------


def dashboard_files() -> list[Path]:
    return sorted(DASHBOARDS.glob("*.json"))


def prepared_dashboard(path: Path, settings: Settings) -> dict[str, Any]:
    dashboard: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    dashboard.pop("id", None)
    if settings.sentry_org_url:
        for variable in dashboard.get("templating", {}).get("list", []):
            if variable.get("name") == "sentry_url":
                variable["query"] = settings.sentry_org_url
    return dashboard


def sync_dashboards(grafana: Api, settings: Settings, report: Callable[[str], None]) -> None:
    if grafana.get(f"/api/folders/{FOLDER_UID}", missing_ok=True) is None:
        grafana.send("POST", "/api/folders", json_body={"uid": FOLDER_UID, "title": FOLDER_TITLE})
        report(f"  folder {FOLDER_TITLE} created")
    for path in dashboard_files():
        dashboard = prepared_dashboard(path, settings)
        grafana.send(
            "POST",
            "/api/dashboards/db",
            json_body={
                "dashboard": dashboard,
                "folderUid": FOLDER_UID,
                "overwrite": True,
                "message": "ops/observability/sync.py",
            },
        )
        report(f"  dashboard {dashboard['title']} pushed")


# --- rules -------------------------------------------------------------------------


def rule_groups() -> list[dict[str, Any]]:
    """Every group of every rules file, in file order."""
    groups: list[dict[str, Any]] = []
    for path in sorted(ALERTS.glob("*.rules.yaml")):
        groups.extend(yaml.safe_load(path.read_text(encoding="utf-8"))["groups"])
    names = [group["name"] for group in groups]
    if len(names) != len(set(names)):
        raise SyncError(f"rule group names repeat: {names}")
    return groups


def sync_rules(grafana: Api, settings: Settings, report: Callable[[str], None]) -> None:
    namespace = f"/api/convert/prometheus/config/v1/rules/{quote(RULES_NAMESPACE)}"
    headers = {
        "Content-Type": "application/yaml",
        "X-Grafana-Alerting-Datasource-UID": settings.prom_datasource_uid,
    }
    groups = rule_groups()
    for group in groups:
        grafana.send(
            "POST", namespace, content=yaml.safe_dump(group, sort_keys=False), headers=headers
        )
        report(f"  rule group {group['name']}: {len(group['rules'])} rules")
    # A group removed from the files is removed from Grafana too.
    existing = grafana.get(namespace, missing_ok=True) or {}
    wanted = {group["name"] for group in groups}
    for listed in existing.get(RULES_NAMESPACE, []) if isinstance(existing, dict) else []:
        name = listed.get("name")
        if name and name not in wanted:
            grafana.send("DELETE", f"{namespace}/{quote(name)}")
            report(f"  rule group {name} deleted")


def quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


# --- alerting: contact point, mute timings, notification policy ---------------------


def mute_timing(name: str, start: str, end: str) -> dict[str, Any]:
    """Muted outside ``start``-``end`` on weekdays, and all weekend."""
    return {
        "name": name,
        "time_intervals": [
            {
                "times": [
                    {"start_time": "00:00", "end_time": start},
                    {"start_time": end, "end_time": "24:00"},
                ],
                "weekdays": ["monday:friday"],
                "location": LOCATION,
            },
            {"weekdays": ["saturday", "sunday"], "location": LOCATION},
        ],
    }


def notification_policy() -> dict[str, Any]:
    """Everything to the telegram contact point, grouped by domain (§ Alerts)."""
    return {
        "receiver": CONTACT_POINT,
        "group_by": ["domain"],
        "group_wait": "30s",
        "group_interval": "5m",
        "repeat_interval": "4h",
        "routes": [
            {
                "receiver": CONTACT_POINT,
                "object_matchers": [["alertname", "=", alertname]],
                "mute_time_intervals": [timing],
            }
            for alertname, (timing, _, _) in QUIET_HOURS.items()
        ],
    }


def sync_alerting(grafana: Api, settings: Settings, report: Callable[[str], None]) -> None:
    points = grafana.get(f"/api/v1/provisioning/contact-points?name={CONTACT_POINT}") or []
    if not points:
        # Editable in the UI (no provenance), so the real values can be typed in there.
        grafana.send(
            "POST",
            "/api/v1/provisioning/contact-points",
            json_body={
                "name": CONTACT_POINT,
                "type": "telegram",
                "settings": {"bottoken": PLACEHOLDER, "chatid": PLACEHOLDER},
            },
            headers={"X-Disable-Provenance": "true"},
        )
        report(f"  contact point {CONTACT_POINT} created with placeholder values")
    else:
        report(f"  contact point {CONTACT_POINT} exists, left as it is")
    for timing, start, end in QUIET_HOURS.values():
        body = mute_timing(timing, start, end)
        path = f"/api/v1/provisioning/mute-timings/{timing}"
        if grafana.get(path, missing_ok=True) is None:
            grafana.send("POST", "/api/v1/provisioning/mute-timings", json_body=body)
        else:
            grafana.send("PUT", path, json_body=body)
        report(f"  mute timing {timing}: outside {start}-{end} {LOCATION}, Mon-Fri")
    grafana.send("PUT", "/api/v1/provisioning/policies", json_body=notification_policy())
    report("  notification policy: all to telegram, grouped by domain")


# --- synthetic check ---------------------------------------------------------------


def uptime_check(settings: Settings, probe_ids: Mapping[str, int]) -> dict[str, Any]:
    check: dict[str, Any] = json.loads(UPTIME_CHECK.read_text(encoding="utf-8"))
    check["target"] = check["target"].replace("${SITE_ADDRESS}", settings.site_address)
    unknown = [name for name in check["probes"] if name not in probe_ids]
    if unknown:
        raise SyncError(f"unknown probe locations {unknown}; available: {sorted(probe_ids)}")
    check["probes"] = [probe_ids[name] for name in check["probes"]]
    return check


def sync_synthetics(sm: Api, settings: Settings, report: Callable[[str], None]) -> None:
    probes = {probe["name"]: probe["id"] for probe in sm.get("/api/v1/probe/list") or []}
    check = uptime_check(settings, probes)
    existing = [c for c in sm.get("/api/v1/check/list") or [] if c.get("job") == check["job"]]
    if existing:
        update = copy.deepcopy(check)
        update["id"], update["tenantId"] = existing[0]["id"], existing[0]["tenantId"]
        sm.send("POST", "/api/v1/check/update", json_body=update)
        report(f"  synthetic check {check['job']} updated")
    else:
        sm.send("POST", "/api/v1/check/add", json_body=check)
        report(f"  synthetic check {check['job']} added")


# --- main --------------------------------------------------------------------------


def run(
    settings: Settings,
    parts: Sequence[str] = PARTS,
    *,
    transport: httpx.BaseTransport | None = None,
    dry_run: bool = False,
    report: Callable[[str], None] = print,
) -> None:
    grafana_parts = [part for part in parts if part != "synthetics"]
    if grafana_parts:
        if not settings.grafana_url:
            raise SyncError("GRAFANA_URL is not set")
        grafana = Api(
            "grafana",
            settings.grafana_url,
            settings.grafana_token,
            transport=transport,
            dry_run=dry_run,
            report=report,
        )
        steps = {"dashboards": sync_dashboards, "rules": sync_rules, "alerting": sync_alerting}
        try:
            for part in grafana_parts:
                report(f"{part}:")
                steps[part](grafana, settings, report)
        finally:
            grafana.close()
    if "synthetics" in parts:
        report("synthetics:")
        missing = [
            name
            for name, value in (
                ("GRAFANA_SM_URL", settings.sm_url),
                ("GRAFANA_SM_TOKEN", settings.sm_token),
                ("SITE_ADDRESS", settings.site_address),
            )
            if not value
        ]
        if missing:
            report(f"  skipped: {', '.join(missing)} not set")
            return
        sm = Api(
            "synthetic-monitoring",
            settings.sm_url,
            settings.sm_token,
            transport=transport,
            dry_run=dry_run,
            report=report,
        )
        try:
            sync_synthetics(sm, settings, report)
        finally:
            sm.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push dashboards, rules and checks to Grafana.")
    parser.add_argument("--only", default="", help=f"comma-separated parts of {', '.join(PARTS)}")
    parser.add_argument("--dry-run", action="store_true", help="print the writes; reads still run")
    args = parser.parse_args(argv)
    parts = [part.strip() for part in args.only.split(",") if part.strip()] or list(PARTS)
    unknown = [part for part in parts if part not in PARTS]
    if unknown:
        parser.error(f"unknown parts {unknown}; choose from {', '.join(PARTS)}")
    try:
        run(Settings.from_env(), parts, dry_run=args.dry_run)
    except (SyncError, httpx.HTTPError) as error:
        print(f"sync failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
