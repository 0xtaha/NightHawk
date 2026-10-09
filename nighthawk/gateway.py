"""Tenant gateway rendering: one route table feeds the Traefik configuration and the auth policy.

Only rows of `ROUTES` are routed. There is no catch-all router, so backend
administrative, ring, deletion, and rule-management endpoints are unreachable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from nighthawk.authz import secret_digest
from nighthawk.config import ConfigurationError, Platform

# Mount points the deployment provides to the proxy; the files come from the trust lifecycle.
CONFIG_DIR = "/etc/nighthawk/gateway"
CLIENT_CA_FILE = f"{CONFIG_DIR}/client-ca.pem"
SERVER_CERT_FILE = f"{CONFIG_DIR}/gateway-server.crt.pem"
SERVER_KEY_FILE = f"{CONFIG_DIR}/gateway-server.key.pem"
DYNAMIC_FILE = f"{CONFIG_DIR}/traefik-dynamic.yaml"
MIN_SECRET_LENGTH = 32
GRAFANA_SERVICE = "grafana-ui"


@dataclass(frozen=True)
class Route:
    name: str
    signal: str
    permission: str
    transport: str
    # A Traefik v3 matcher on the external request path.
    match: str
    upstream: str
    # None forwards the path unchanged; ("strip", prefix) or ("replace", path) rewrites it.
    rewrite: tuple[str, str] | None
    description: str


def _query(prefix: str, *endpoints: str) -> str:
    return f"PathRegexp(`^{prefix}/({'|'.join(endpoints)})$`)"


VALUE = "[^/]+"
ROUTES = (
    Route("metrics-remote-write", "metrics", "ingest", "HTTP", "Path(`/metrics/api/v1/push`)",
          "metrics", ("strip", "/metrics"), "Prometheus remote write"),
    Route("metrics-otlp-http", "metrics", "ingest", "OTLP HTTP", "Path(`/v1/metrics`)",
          "metrics", ("replace", "/otlp/v1/metrics"), "OTLP metrics"),
    Route("metrics-query", "metrics", "query", "HTTP", _query(
        "/metrics/prometheus/api/v1", "query", "query_range", "query_exemplars", "series", "labels",
        f"label/{VALUE}/values", "metadata", "rules", "alerts", "format_query", "status/buildinfo",
    ), "metrics", ("strip", "/metrics"), "Prometheus-compatible read API"),
    Route("logs-push", "logs", "ingest", "HTTP", "Path(`/logs/loki/api/v1/push`)",
          "logs", ("strip", "/logs"), "Loki push"),
    Route("logs-otlp-http", "logs", "ingest", "OTLP HTTP", "Path(`/v1/logs`)",
          "logs", ("replace", "/otlp/v1/logs"), "OTLP logs"),
    Route("logs-query", "logs", "query", "HTTP", _query(
        "/logs/loki/api/v1", "query", "query_range", "labels", f"label/{VALUE}/values", "series",
        "index/stats", "index/volume", "index/volume_range", "patterns", "detected_fields",
        "detected_labels", "tail",
    ), "logs", ("strip", "/logs"), "Loki read API"),
    Route("traces-otlp-http", "traces", "ingest", "OTLP HTTP", "Path(`/v1/traces`)",
          "traces.otlp_http", None, "OTLP traces"),
    Route("traces-otlp-grpc", "traces", "ingest", "OTLP gRPC",
          "Path(`/opentelemetry.proto.collector.trace.v1.TraceService/Export`)",
          "traces.otlp_grpc", None, "OTLP traces"),
    Route("traces-query", "traces", "query", "HTTP", _query(
        "/traces/api", "echo", f"traces/{VALUE}", f"v2/traces/{VALUE}", "search", "search/tags",
        f"search/tag/{VALUE}/values", "v2/search/tags", f"v2/search/tag/{VALUE}/values",
        "metrics/query", "metrics/query_range",
    ), "traces.query", ("strip", "/traces"), "Tempo read API"),
    Route("profiles-push", "profiles", "ingest", "HTTP", "Path(`/profiles/push.v1.PusherService/Push`)",
          "profiles", ("strip", "/profiles"), "Pyroscope push"),
    Route("profiles-ingest", "profiles", "ingest", "HTTP", "Path(`/profiles/ingest`)",
          "profiles", ("strip", "/profiles"), "Pyroscope ingest API"),
    Route("profiles-query", "profiles", "query", "HTTP", "PathPrefix(`/profiles/querier.v1.QuerierService/`)",
          "profiles", ("strip", "/profiles"), "Pyroscope query service"),
)


def _active_routes(platform: Platform) -> list[Route]:
    return [route for route in ROUTES if route.upstream in platform.gateway.upstreams]


def _auth_name(entry: str, signal: str, permission: str) -> str:
    return f"auth-{entry}-{signal}-{permission}"


def traefik_dynamic(platform: Platform) -> dict:
    gateway = platform.gateway
    routers: dict[str, dict] = {}
    middlewares: dict[str, dict] = {
        # The verified TLS peer certificate, for the auth service. Must precede forwardAuth.
        "client-certificate": {"passTLSClientCert": {"pem": True}},
        # The gateway credential is not passed on to backends.
        "strip-credentials": {"headers": {"customRequestHeaders": {"Authorization": ""}}},
    }
    services: dict[str, dict] = {}
    for route in _active_routes(platform):
        service = route.upstream.replace(".", "-")
        services[service] = {"loadBalancer": {"servers": [{"url": gateway.upstreams[route.upstream]}]}}
        rewrite: list[str] = []
        if route.rewrite is not None:
            kind, value = route.rewrite
            name = f"{kind}-{value.strip('/').replace('/', '-')}"
            middlewares[name] = (
                {"stripPrefix": {"prefixes": [value]}} if kind == "strip" else {"replacePath": {"path": value}}
            )
            rewrite = [name]
        for entry in gateway.entry_points:
            auth = _auth_name(entry.name, route.signal, route.permission)
            middlewares[auth] = {"forwardAuth": {
                # The route class is fixed here by the operator; clients cannot influence it.
                "address": f"{gateway.auth_service}/verify?entry={entry.name}&signal={route.signal}&permission={route.permission}",
                # Required for the certificate header to reach the auth service. Client-supplied
                # X-Forwarded-* headers are removed at the entry point before this runs.
                "trustForwardHeader": True,
                "authRequestHeaders": ["Authorization", "X-Scope-OrgID"],
                # Replaces any client value; the header is deleted if the auth response omits it.
                "authResponseHeaders": ["X-Scope-OrgID"],
                "maxResponseBodySize": 4096,
            }}
            routers[f"{entry.name}-{route.name}"] = {
                "entryPoints": [entry.name],
                "rule": f"Host(`{gateway.hostname}`) && {route.match}",
                "middlewares": ["client-certificate", auth, "strip-credentials", *rewrite],
                "service": service,
                "tls": {},
            }
    # The Grafana UI: its own host name, its own login, no tenant binding. Telemetry routers
    # are bound to the gateway host name, so this host can never reach a signal backend.
    services[GRAFANA_SERVICE] = {"loadBalancer": {"servers": [{"url": gateway.upstreams["grafana"]}]}}
    for entry in gateway.entry_points:
        routers[f"{entry.name}-{GRAFANA_SERVICE}"] = {
            "entryPoints": [entry.name],
            "rule": f"Host(`{platform.grafana.hostname}`)",
            "service": GRAFANA_SERVICE,
            "tls": {},
        }
    return {
        "http": {"routers": routers, "middlewares": middlewares, "services": services},
        "tls": {
            "certificates": [{"certFile": SERVER_CERT_FILE, "keyFile": SERVER_KEY_FILE}],
            "options": {"default": {
                "minVersion": "VersionTLS12",
                # "If given": query credentials and local collectors have no certificate.
                # The auth service decides which credentials must present one.
                "clientAuth": {"caFiles": [CLIENT_CA_FILE], "clientAuthType": "VerifyClientCertIfGiven"},
            }},
        },
    }


def traefik_static(platform: Platform, rules: list[dict]) -> dict:
    metrics = [rule for rule in rules if rule["destination"] == "gateway-metrics"]
    if len(metrics) != 1:
        raise ConfigurationError("the network contract must declare exactly one rule to gateway-metrics")
    entry_points: dict[str, dict] = {
        entry.name: {
            "address": f":{entry.port}",
            # TLS-only, and header aliases of the managed X-Forwarded-* names are dropped.
            "http": {"tls": {}, "aliasHeadersStrategy": "delete"},
        }
        for entry in platform.gateway.entry_points
    }
    entry_points["metrics"] = {"address": f":{metrics[0]['port']}"}
    return {
        "entryPoints": entry_points,
        "providers": {"file": {"filename": DYNAMIC_FILE, "watch": True}},
        "metrics": {"prometheus": {"entryPoint": "metrics"}},
        # Health endpoint for `traefik healthcheck`, on the private metrics listener.
        "ping": {"entryPoint": "metrics"},
    }


def routes_markdown(platform: Platform) -> str:
    lines = [
        "# Gateway routes",
        "",
        f"Generated from the route table for `https://{platform.gateway.hostname}`. Nothing else is routed.",
        "",
        "| Route | Signal | Permission | Transport | External match | Upstream | Path sent upstream |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for route in _active_routes(platform):
        if route.rewrite is None:
            sent = "unchanged"
        elif route.rewrite[0] == "strip":
            sent = f"`{route.rewrite[1]}` prefix removed"
        else:
            sent = f"`{route.rewrite[1]}`"
        match = route.match.replace("|", "\\|")
        lines.append(
            f"| {route.name} | {route.signal} | {route.permission} | {route.transport} | `` {match} `` | "
            f"`{platform.gateway.upstreams[route.upstream]}` | {sent} |"
        )
    lines += [
        "",
        f"The Grafana UI is served for `https://{platform.grafana.hostname}` on the same listeners and",
        "forwarded to Grafana without tenant authentication; Grafana performs its own login.",
    ]
    plaintext = sorted(
        f"`{name}` ({address})" for name, address in platform.gateway.upstreams.items()
        if address.startswith(("http://", "h2c://"))
    )
    if platform.gateway.auth_service.startswith("http://"):
        plaintext.append(f"`auth_service` ({platform.gateway.auth_service})")
    lines += ["", "## Plaintext links behind the gateway", ""]
    lines += [f"- {item}" for item in plaintext] or ["None."]
    lines += ["", "These must stay on a private network; they are not encrypted.", ""]
    return "\n".join(lines)


def render_artifacts(platform: Platform, rules: list[dict]) -> dict[str, str]:
    return {
        "gateway/traefik-dynamic.yaml": yaml.safe_dump(traefik_dynamic(platform), sort_keys=True),
        "gateway/traefik-static.yaml": yaml.safe_dump(traefik_static(platform, rules), sort_keys=True),
        "gateway/routes.md": routes_markdown(platform),
    }


def policy_bundle(platform: Platform, secrets_dir: Path) -> dict:
    """Build the digest-only auth policy from materialized credential secrets."""
    credentials: dict[str, dict] = {}
    signals = {(stream.tenant, stream.datastream): sorted(stream.signals) for stream in platform.streams}
    for credential in platform.credentials:
        reference = platform.secrets[credential.secret_ref]
        try:
            secret = (secrets_dir / reference.file / reference.key).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError) as error:
            raise ConfigurationError(
                f"{credential.id}: materialized secret is not readable ({type(error).__name__})"
            ) from None
        if len(secret) < MIN_SECRET_LENGTH:
            raise ConfigurationError(
                f"{credential.id}: gateway secret is shorter than {MIN_SECRET_LENGTH} characters"
            )
        credentials[credential.id] = {
            "secret_sha256": secret_digest(secret),
            "backend_id": credential.backend_id,
            "permission": credential.permission,
            "signals": signals[(credential.tenant, credential.datastream)],
            "certificate_identity": credential.certificate_identity,
        }
    return {
        "schema_version": 1,
        "entry_points": {entry.name: entry.scope for entry in platform.gateway.entry_points},
        "revoked_certificate_fingerprints": list(platform.gateway.revoked_certificate_fingerprints),
        "credentials": credentials,
    }


def write_policy_bundle(bundle: dict, output: Path) -> None:
    """Write owner-only and replace atomically, so a reader never sees a partial policy."""
    temporary = output.with_name(output.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise
