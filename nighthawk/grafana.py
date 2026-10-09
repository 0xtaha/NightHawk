"""Grafana tenant provisioning: a secret-free desired state and an idempotent API reconciler.

Tenant isolation is enforced at the gateway, which binds each query credential to one
backend ID. Organizations keep customers' dashboards and data sources apart.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from nighthawk.config import ConfigurationError, Platform, Stream

# method, path, JSON body, organization ID -> (status, decoded JSON body)
Client = Callable[[str, str, object, int | None], tuple[int, object]]

MANAGED_UID_PREFIX = "nh-"
SIGNAL_TYPES = {
    "metrics": ("m", "prometheus", "/metrics/prometheus"),
    "logs": ("l", "loki", "/logs"),
    "traces": ("t", "tempo", "/traces"),
    "profiles": ("p", "grafana-pyroscope-datasource", "/profiles"),
}
COMPARED_FIELDS = ("name", "type", "url", "access", "basicAuth", "basicAuthUser", "isDefault", "jsonData")


def datasource_uid(backend_id: str, signal: str) -> str:
    """Stable and at most 37 characters; Grafana limits UIDs to 40 of [a-zA-Z0-9_-]."""
    return f"{MANAGED_UID_PREFIX}{SIGNAL_TYPES[signal][0]}-{hashlib.sha256(backend_id.encode('utf-8')).hexdigest()[:32]}"


def _json_data(stream: Stream, signal: str) -> dict:
    def uid(target: str) -> str | None:
        return datasource_uid(stream.backend_id, target) if target in stream.signals else None

    data: dict = {}
    if signal == "metrics":
        data["httpMethod"] = "POST"
        if uid("traces"):
            data["exemplarTraceIdDestinations"] = [{"name": "trace_id", "datasourceUid": uid("traces")}]
    elif signal == "logs":
        if uid("traces"):
            data["derivedFields"] = [{
                "name": "trace_id",
                "matcherRegex": r'"?trace_?id"?\s*[=:]\s*"?(\w+)',
                "datasourceUid": uid("traces"),
                "url": "${__value.raw}",
            }]
    elif signal == "traces":
        if uid("logs"):
            data["tracesToLogsV2"] = {"datasourceUid": uid("logs"), "customQuery": False, "filterByTraceID": True}
        if uid("metrics"):
            data["tracesToMetrics"] = {"datasourceUid": uid("metrics"), "queries": []}
            data["serviceMap"] = {"datasourceUid": uid("metrics")}
        if uid("profiles"):
            data["tracesToProfiles"] = {
                "datasourceUid": uid("profiles"), "customQuery": False,
                "profileTypeId": "process_cpu:cpu:nanoseconds:cpu:nanoseconds",
            }
    return data


def desired_state(platform: Platform) -> dict:
    """One organization per tenant; one data source per datastream and enabled signal."""
    base = platform.gateway.url(platform.gateway.grafana_entry_point)
    organizations: dict[str, list[dict]] = {}
    for stream in platform.streams:
        # With overlapping query credentials during a rotation, use the first by ID.
        credential = min(
            (item for item in platform.credentials
             if (item.tenant, item.datastream, item.permission) == (stream.tenant, stream.datastream, "query")),
            key=lambda item: item.id,
        )
        datasources = organizations.setdefault(stream.tenant, [])
        for signal in sorted(stream.signals):
            _, kind, path = SIGNAL_TYPES[signal]
            datasources.append({
                "uid": datasource_uid(stream.backend_id, signal),
                "name": f"{stream.datastream} {signal}",
                "type": kind,
                "url": base + path,
                "access": "proxy",
                "basicAuth": True,
                "basicAuthUser": credential.id,
                "secret_ref": credential.secret_ref,
                "isDefault": False,
                "jsonData": _json_data(stream, signal),
            })
    return {
        "schema_version": 1,
        "settings": {"auth.anonymous": {"enabled": False}},
        "organizations": [
            {"name": tenant, "datasources": sorted(datasources, key=lambda item: item["uid"])}
            for tenant, datasources in sorted(organizations.items())
        ],
    }


@dataclass
class Changes:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    undeclared: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.deleted)

    def lines(self, dry_run: bool) -> list[str]:
        verb = "would " if dry_run else ""
        lines = [f"{verb}create {item}" for item in self.created]
        lines += [f"{verb}update {item}" for item in self.updated]
        lines += [f"{verb}delete {item}" for item in self.deleted]
        lines += [f"undeclared, left in place: {item}" for item in self.undeclared]
        return lines or ["no changes"]


def http_client(url: str, admin_user: str, admin_password: str) -> Client:
    token = base64.b64encode(f"{admin_user}:{admin_password}".encode("utf-8")).decode("ascii")

    def request(method: str, path: str, body: object = None, org_id: int | None = None) -> tuple[int, object]:
        headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        if org_id is not None:
            headers["X-Grafana-Org-Id"] = str(org_id)
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        call = urllib.request.Request(url.rstrip("/") + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(call, timeout=30) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        except (urllib.error.URLError, OSError) as error:
            raise ConfigurationError(f"{method} {path}: cannot reach Grafana: {getattr(error, 'reason', error)}") from None
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, None

    return request


def _expect(client: Client, method: str, path: str, body: object, org_id: int | None, accepted: tuple[int, ...]) -> tuple[int, object]:
    status, payload = client(method, path, body, org_id)
    if status not in accepted:
        # Never echo the response or request body: either may contain credentials.
        raise ConfigurationError(f"{method} {path}: Grafana answered HTTP {status}")
    return status, payload


def reconcile(
    state: dict, client: Client, read_secret: Callable[[str], str], *,
    dry_run: bool = False, prune: bool = False, update_secrets: bool = False,
) -> Changes:
    """Create missing organizations and data sources and update changed ones. Organizations are never deleted."""
    changes = Changes()
    declared_orgs = {organization["name"] for organization in state["organizations"]}
    _, existing_orgs = _expect(client, "GET", "/api/orgs", None, None, (200,))
    for organization in existing_orgs if isinstance(existing_orgs, list) else []:
        if organization.get("id") != 1 and organization.get("name") not in declared_orgs:
            changes.undeclared.append(f"organization {organization.get('name')}")
    for organization in state["organizations"]:
        name = organization["name"]
        status, found = _expect(client, "GET", f"/api/orgs/name/{quote(name, safe='')}", None, None, (200, 404))
        org_id: int | None = found["id"] if status == 200 else None
        if org_id is None:
            changes.created.append(f"organization {name}")
            if not dry_run:
                _, created = _expect(client, "POST", "/api/orgs", {"name": name}, None, (200,))
                org_id = int(created["orgId"])
        existing: dict[str, dict] = {}
        if org_id is not None:
            _, listed = _expect(client, "GET", "/api/datasources", None, org_id, (200,))
            existing = {item["uid"]: item for item in listed}
        declared_uids = set()
        for datasource in organization["datasources"]:
            uid = datasource["uid"]
            declared_uids.add(uid)
            label = f"data source {name}/{datasource['name']} ({uid})"
            current = existing.get(uid)
            if current is not None:
                # The list response omits basicAuthUser, so compare against the full record.
                _, current = _expect(client, "GET", f"/api/datasources/uid/{uid}", None, org_id, (200,))
            differs = current is not None and any(
                current.get(key) != datasource[key] for key in COMPARED_FIELDS
            )
            if current is not None and not differs and not update_secrets:
                continue
            (changes.created if current is None else changes.updated).append(label)
            if dry_run:
                continue
            body = {key: value for key, value in datasource.items() if key != "secret_ref"}
            body["secureJsonData"] = {"basicAuthPassword": read_secret(datasource["secret_ref"])}
            if current is None:
                _expect(client, "POST", "/api/datasources", body, org_id, (200,))
            else:
                _expect(client, "PUT", f"/api/datasources/uid/{uid}", body, org_id, (200,))
        for uid, item in sorted(existing.items()):
            if not uid.startswith(MANAGED_UID_PREFIX) or uid in declared_uids:
                continue
            label = f"data source {name}/{item.get('name')} ({uid})"
            if not prune:
                changes.undeclared.append(label)
                continue
            changes.deleted.append(label)
            if not dry_run:
                _expect(client, "DELETE", f"/api/datasources/uid/{uid}", None, org_id, (200,))
    return changes


def secret_reader(platform: Platform, secrets_dir: Path) -> Callable[[str], str]:
    def read(secret_ref: str) -> str:
        reference = platform.secrets[secret_ref]
        try:
            return (secrets_dir / reference.file / reference.key).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError) as error:
            raise ConfigurationError(f"{secret_ref}: materialized secret is not readable ({type(error).__name__})") from None

    return read
