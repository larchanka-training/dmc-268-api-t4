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

The rules files are Prometheus-format (promtool tests them). They become Grafana-managed
rules through the alerting provisioning API, in the form Grafana's own import of
Prometheus rules produces, so the notification policy below routes them to the
``telegram`` contact point. The import API itself is not served by Grafana Cloud (404,
2026-10-30), hence the provisioning API. The contact point is created once with
placeholder values and never touched again: the real bot token and chat id are typed
into Grafana by hand.
"""

import argparse
import copy
import hashlib
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
#: The rules' folder.
RULES_FOLDER_UID = "dmc268-alerts"
RULES_FOLDER_TITLE = "DMC-268 alerts"
#: Rules query a minute in the past, as Grafana's import sets them: Alloy pushes once a
#: minute, so the newest minute may not have arrived yet.
QUERY_RANGE = {"from": 660, "to": 60}
CONTACT_POINT = "telegram"
PLACEHOLDER = "placeholder: enter the bot token and chat id in Grafana"
DEFAULT_PROM_DATASOURCE_UID = "grafanacloud-prom"
PARTS = ("dashboards", "rules", "alerting", "synthetics")
#: Pushed only when asked for (``--only drill``): the "No throughput" drill copy. The next
#: ``rules`` sync deletes it, since its group is not among the rules files.
DRILL = "drill"
DRILL_ALERTS = ALERTS / "drill"
TIMEOUT_SECONDS = 30.0

#: Working hours (Europe/Berlin, Mon-Fri) for the two rules notified only then. Each
#: window starts one lookback after 09:00, so a rule that looks back 4 h never reports
#: the night (docs/observability.md § Alerts). Outside it, notifications are muted.
QUIET_HOURS = {
    "NoThroughput": ("no-throughput-quiet-hours", "13:00", "19:00"),
    "CanaryReviewStale": ("canary-quiet-hours", "13:15", "23:15"),
}
LOCATION = "Europe/Berlin"


#: What Grafana answers when creating a folder whose uid exists: 412 in Grafana 13, 409
#: in older versions.
FOLDER_EXISTS = frozenset({409, 412})


class SyncError(Exception):
    """A Grafana API call failed. The text names the call and the status, never a token."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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
            grafana_url=base_url(text("GRAFANA_URL")),
            grafana_token=text("GRAFANA_SA_TOKEN"),
            prom_datasource_uid=text("GRAFANA_PROM_DATASOURCE_UID") or DEFAULT_PROM_DATASOURCE_UID,
            sentry_org_url=base_url(text("SENTRY_ORG_URL")),
            sm_url=base_url(text("GRAFANA_SM_URL")),
            sm_token=text("GRAFANA_SM_TOKEN"),
            site_address=text("SITE_ADDRESS"),
        )


def base_url(value: str) -> str:
    """A base URL without its trailing slash; a bare host gets ``https://``, since
    Grafana's pages show some API URLs without one."""
    value = value.rstrip("/")
    if value and "://" not in value:
        value = f"https://{value}"
    return value


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
        response = self._call("GET", path, headers={"Accept": "application/json"})
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
        response = self._call(
            method, path, json=json_body, content=content, headers=dict(headers or {})
        )
        return self._decode(response, method, path)

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """The request; a transport failure names the API and the call (never a token)."""
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise SyncError(
                f"{method} {self._name}{path} failed: {type(error).__name__}: {error}"
            ) from error

    def _decode(self, response: httpx.Response, method: str, path: str) -> Any:
        if response.status_code >= 400:
            # The body is Grafana's error message; it never echoes the token.
            raise SyncError(
                f"{method} {self._name}{path} answered HTTP {response.status_code}: "
                f"{response.text[:300]}",
                response.status_code,
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


def ensure_folder(grafana: Api, uid: str, title: str, report: Callable[[str], None]) -> None:
    # Created, not looked up first: below Admin, Grafana answers a lookup of a folder that
    # does not exist with 403 (folders:read), since no permission exists on it yet.
    try:
        grafana.send("POST", "/api/folders", json_body={"uid": uid, "title": title})
    except SyncError as error:
        if error.status not in FOLDER_EXISTS:
            raise
        report(f"  folder {title} exists")
    else:
        report(f"  folder {title} ready")


def sync_dashboards(grafana: Api, settings: Settings, report: Callable[[str], None]) -> None:
    ensure_folder(grafana, FOLDER_UID, FOLDER_TITLE, report)
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


def rule_groups(folder: Path = ALERTS) -> list[dict[str, Any]]:
    """Every group of every rules file in ``folder``, in file order."""
    groups: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.rules.yaml")):
        groups.extend(yaml.safe_load(path.read_text(encoding="utf-8"))["groups"])
    names = [group["name"] for group in groups]
    if len(names) != len(set(names)):
        raise SyncError(f"rule group names repeat: {names}")
    return groups


def rule_uid(group: str, alert: str) -> str:
    """Stable, so a sync replaces a rule instead of adding a copy; Grafana allows 40
    characters of [a-zA-Z0-9-_]."""
    uid = f"dmc268-{group}-{alert.lower()}"
    if len(uid) > 40:
        uid = "dmc268-" + hashlib.sha1(f"{group}/{alert}".encode()).hexdigest()[:20]
    return uid


def grafana_rule(rule: Mapping[str, Any], group: str, datasource_uid: str) -> dict[str, Any]:
    """A Prometheus alerting rule as a Grafana-managed one, in the form Grafana's own
    import produces (read back from Grafana 13.2): the PromQL as an instant query, a
    math step that makes every returned series 1, and a threshold above 0. So it fires
    once per returned series, and no data is OK, as in Prometheus."""
    expression = {"type": "__expr__", "uid": "__expr__"}
    return {
        "uid": rule_uid(group, rule["alert"]),
        "title": rule["alert"],
        "ruleGroup": group,
        "folderUID": RULES_FOLDER_UID,
        "condition": "threshold",
        "data": [
            {
                "refId": "query",
                "queryType": "prometheus",
                "relativeTimeRange": QUERY_RANGE,
                "datasourceUid": datasource_uid,
                "model": {
                    "refId": "query",
                    "datasource": {"type": "prometheus", "uid": datasource_uid},
                    "expr": rule["expr"],
                    "instant": True,
                    "range": False,
                },
            },
            {
                "refId": "prometheus_math",
                "queryType": "math",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "prometheus_math",
                    "datasource": expression,
                    "type": "math",
                    "expression": "is_number($query) || is_nan($query) || is_inf($query)",
                },
            },
            {
                "refId": "threshold",
                "queryType": "threshold",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "threshold",
                    "datasource": expression,
                    "type": "threshold",
                    "expression": "prometheus_math",
                    "conditions": [{"evaluator": {"type": "gt", "params": [0]}}],
                },
            },
        ],
        "noDataState": "OK",
        "execErrState": "OK",
        "for": rule.get("for", "0s"),
        "labels": dict(rule.get("labels", {})),
        "annotations": dict(rule.get("annotations", {})),
        "isPaused": False,
        # Resolved at the first evaluation without its series, as in Prometheus.
        "missingSeriesEvalsToResolve": 1,
    }


def sync_rules(
    grafana: Api,
    settings: Settings,
    report: Callable[[str], None],
    *,
    folder: Path = ALERTS,
    prune: bool = True,
) -> None:
    """Every group of ``folder``; with ``prune``, the rules of any other group in the
    rules' Grafana folder are deleted (the drill pushes without it)."""
    ensure_folder(grafana, RULES_FOLDER_UID, RULES_FOLDER_TITLE, report)
    groups = rule_groups(folder)
    for group in groups:
        name = group["name"]
        rules = [grafana_rule(rule, name, settings.prom_datasource_uid) for rule in group["rules"]]
        # A PUT replaces the whole group, so a rule removed from it goes too.
        grafana.send(
            "PUT",
            f"/api/v1/provisioning/folder/{RULES_FOLDER_UID}/rule-groups/{quote(name)}",
            json_body={
                "title": name,
                "folderUid": RULES_FOLDER_UID,
                "interval": interval_seconds(group.get("interval", "1m")),
                "rules": rules,
            },
        )
        report(f"  rule group {name}: {len(rules)} rules")
    if not prune:
        return
    # A group removed from the files is removed from Grafana too.
    wanted = {group["name"] for group in groups}
    for listed in grafana.get("/api/v1/provisioning/alert-rules") or []:
        if listed.get("folderUID") == RULES_FOLDER_UID and listed.get("ruleGroup") not in wanted:
            grafana.send("DELETE", f"/api/v1/provisioning/alert-rules/{quote(listed['uid'])}")
            report(f"  rule {listed.get('title')} of group {listed.get('ruleGroup')} deleted")


def interval_seconds(value: str) -> int:
    """A Prometheus duration like 1m or 30s, in seconds."""
    units = {"s": 1, "m": 60, "h": 3600}
    return int(value[:-1]) * units[value[-1]]


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
        steps: dict[str, Callable[[Api, Settings, Callable[[str], None]], None]] = {
            "dashboards": sync_dashboards,
            "rules": sync_rules,
            "alerting": sync_alerting,
            DRILL: lambda g, s, r: sync_rules(g, s, r, folder=DRILL_ALERTS, prune=False),
        }
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
    parser.add_argument(
        "--only",
        default="",
        help=f"comma-separated parts of {', '.join(PARTS)}; or {DRILL} for the drill rule",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the writes; reads still run")
    args = parser.parse_args(argv)
    parts = [part.strip() for part in args.only.split(",") if part.strip()] or list(PARTS)
    unknown = [part for part in parts if part not in (*PARTS, DRILL)]
    if unknown:
        parser.error(f"unknown parts {unknown}; choose from {', '.join((*PARTS, DRILL))}")
    try:
        run(Settings.from_env(), parts, dry_run=args.dry_run)
    except (SyncError, httpx.HTTPError) as error:
        print(f"sync failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
