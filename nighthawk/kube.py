"""What a self-hosted Kubernetes deployment consists of, derived from the platform document.

This module is the one place that knows which workloads exist, which secrets each reads,
and under which identity. The Vault requirements, the Helm values, and the NetworkPolicies
are all rendered from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from nighthawk import collector, grafana, storage
from nighthawk.config import ROOT, UPSTREAM_DESTINATION, ConfigurationError, Platform, Stream, mapping, string

NAMESPACE = "nighthawk"
# The gateway is the only workload that reads Secrets through the cluster API, so it has a
# namespace that holds nothing but its own certificate.
GATEWAY_NAMESPACE = "nighthawk-gateway"
# The Secret that carries the authority's certificate to workloads that call the gateway.
GATEWAY_TRUST = "gateway-trust"
# Signal backends by the signal that enables them.
BACKENDS = {signal: UPSTREAM_DESTINATION[signal] for signal in ("metrics", "logs", "traces", "profiles")}
# The identity the certificate manager's issuer uses. It signs; it reads no secret.
ISSUER = "certificate-issuer"
COLLECTOR_PROFILE = "k8s-cluster"
NODE_COLLECTOR_PROFILE = "k8s-node"
# The entry point collectors inside the cluster deliver through.
CLUSTER_ENTRY_POINT = "cluster-gateway"


@dataclass(frozen=True)
class Workload:
    """A workload with an identity of its own in Vault."""
    name: str
    # Secret references of the platform document this workload reads, sorted.
    secrets: tuple[str, ...]

    @property
    def service_account(self) -> str:
        return self.name

    @property
    def vault_role(self) -> str:
        return f"nighthawk-{self.name}"


def require_self_hosted(platform: Platform) -> None:
    if platform.deployment != "self-hosted-k8s":
        raise ConfigurationError(f"Kubernetes output is rendered for a self-hosted-k8s document, not {platform.deployment}")
    if platform.vault.kubernetes_auth_mount is None:
        raise ConfigurationError(
            "vault: a self-hosted-k8s deployment needs kubernetes_auth.mount, "
            "the Vault mount the cluster's service accounts authenticate at"
        )


def collector_stream(platform: Platform, tenant: str | None = None, datastream: str | None = None) -> Stream:
    """The datastream the cluster's own collectors deliver to: the named one, or the first."""
    if tenant is None and datastream is None:
        return platform.streams[0]
    for stream in platform.streams:
        if (stream.tenant, stream.datastream) == (tenant, datastream):
            return stream
    raise ConfigurationError(f"unknown tenant/datastream {tenant}/{datastream}")


def enabled_backends(platform: Platform) -> list[str]:
    return sorted({BACKENDS[signal] for stream in platform.streams for signal in stream.signals})


def workloads(
    platform: Platform, *, tenant: str | None = None, datastream: str | None = None,
    ingest_credential: str | None = None, query_credentials: tuple[str, ...] = (),
) -> list[Workload]:
    """Every workload that reads a secret, with exactly the secrets it reads."""
    require_self_hosted(platform)
    stream = collector_stream(platform, tenant, datastream)
    ingest = collector._select_credential(platform, stream, collector.PROFILES[COLLECTOR_PROFILE], ingest_credential)
    queries = grafana.query_credentials(platform, query_credentials)
    storage: dict[str, set[str]] = {}
    for name, binding in platform.bindings.items():
        backend = name.split("-", 1)[0]
        refs = storage.setdefault(backend, set())
        if binding.identity.type == "secret":
            refs.add(binding.identity.ref)
        if binding.tls.ca_secret_ref:
            refs.add(binding.tls.ca_secret_ref)
    reads: dict[str, set[str]] = {
        "authz": {item.secret_ref for item in platform.credentials},
        "collector": {ingest.secret_ref},
        "grafana": {platform.grafana.admin_secret_ref},
        "grafana-provisioner": {platform.grafana.admin_secret_ref} | {item.secret_ref for item in queries.values()},
    }
    for backend in enabled_backends(platform):
        reads[backend] = storage.get(backend, set())
    if platform.storage_provider == "seaweedfs":
        reads["object-storage"] = {
            binding.identity.ref for binding in platform.bindings.values() if binding.identity.type == "secret"
        }
    result = [Workload(name, tuple(sorted(refs))) for name, refs in sorted(reads.items())]
    _check_paths(platform, result)
    return result


def _check_paths(platform: Platform, items: list[Workload]) -> None:
    """Vault grants access per path, not per key: a path may only hold secrets with the same readers."""
    readers: dict[str, set[str]] = {}
    for workload in items:
        for ref in workload.secrets:
            readers.setdefault(ref, set()).add(workload.name)
    by_path: dict[str, dict[str, frozenset[str]]] = {}
    for ref, reference in platform.secrets.items():
        by_path.setdefault(reference.path, {})[ref] = frozenset(readers.get(ref, ()))
    for path, refs in sorted(by_path.items()):
        if len(set(refs.values())) > 1:
            listing = "; ".join(f"{ref} is read by {', '.join(sorted(names)) or 'no workload'}" for ref, names in sorted(refs.items()))
            raise ConfigurationError(
                f"secrets: Vault path {path} holds secrets with different readers ({listing}). A workload's Vault "
                "policy covers a whole path, so give secrets with different readers different paths"
            )


# ---- rendering ---------------------------------------------------------------------------

PLATFORM_CHART = "nighthawk-platform"
# The label every NetworkPolicy selects on: a workload's name in the network contract.
COMPONENT_LABEL = "nighthawk.io/component"
# How long a certificate is valid and how long before expiry it is replaced.
CERTIFICATE_DURATION, CERTIFICATE_RENEW_BEFORE = "2160h", "720h"
DEVELOPMENT_NOTICE = (
    "This is the development profile: a single node without high availability. "
    "Retention values in the platform document are examples, not production defaults."
)
# Monolithic backends the platform chart runs in the development profile, as Compose runs them.
MONOLITHIC = {
    "mimir": {"dataDir": "/data", "readyPort": 8080, "size": "10Gi"},
    "tempo": {"dataDir": "/var/tempo", "readyPort": 3200, "size": "10Gi", "extraArgs": ["-target=all"]},
}
STORAGE_INIT_SCRIPT = ROOT / "docker-compose" / "storage-init.sh"


def image(matrix: dict, name: str) -> str:
    """An image reference by tag and digest, from the compatibility matrix."""
    pin = mapping(mapping(matrix["container_images"])[name])
    return f"{string(pin['repository'])}:{string(pin['tag'])}@{string(pin['digest'])}"


def _dump(document: object) -> str:
    return yaml.safe_dump(document, sort_keys=True, default_flow_style=False, width=1000)


def _secret(platform: Platform, ref: str, shape: str | None = None) -> dict:
    reference = platform.secrets[ref]
    entry = {"ref": ref, "path": reference.path, "key": reference.key}
    if shape:
        entry["shape"] = shape
    return entry


def gateway_certificate(platform: Platform) -> dict:
    return {
        "name": "gateway-server", "secretName": "gateway-server-tls", "kind": "server",
        "commonName": platform.gateway.hostname,
        "dnsNames": [platform.gateway.hostname, platform.grafana.hostname],
        "duration": CERTIFICATE_DURATION, "renewBefore": CERTIFICATE_RENEW_BEFORE,
    }


def _certificates(platform: Platform, ingest) -> list[dict]:
    common = {"duration": CERTIFICATE_DURATION, "renewBefore": CERTIFICATE_RENEW_BEFORE}
    # The gateway's own certificate is requested in the gateway's namespace. This one exists
    # so that its Secret carries the authority's certificate to the gateway's callers.
    certificates = [{**gateway_certificate(platform), "name": GATEWAY_TRUST, "secretName": GATEWAY_TRUST}]
    if platform.storage_provider == "seaweedfs":
        hostnames = sorted({urlsplit(binding.endpoint).hostname or "" for binding in platform.bindings.values()})
        certificates.append({
            "name": "storage-server", "secretName": "storage-server-tls", "kind": "server",
            "commonName": hostnames[0], "dnsNames": hostnames, **common,
        })
    if ingest.certificate_identity is not None:
        certificates.append({
            "name": "collector-client", "secretName": "collector-client-tls", "kind": "client",
            "commonName": ingest.certificate_identity.rsplit("/", 1)[1], "uris": [ingest.certificate_identity], **common,
        })
    return certificates


@dataclass(frozen=True)
class Selection:
    """The choices a rendering was made with, so every output agrees on them."""
    tenant: str | None = None
    datastream: str | None = None
    ingest_credential: str | None = None
    query_credentials: tuple[str, ...] = ()


def platform_values(
    platform: Platform, matrix: dict, artifacts: dict[str, str], selection: Selection = Selection(),
    metrics_port: int = 8082,
) -> dict:
    """Values shared by every stage of the platform chart. `artifacts` are the rendered contracts."""
    require_self_hosted(platform)
    vault = platform.vault
    stream = collector_stream(platform, selection.tenant, selection.datastream)
    ingest = collector._select_credential(
        platform, stream, collector.PROFILES[COLLECTOR_PROFILE], selection.ingest_credential,
    )
    backends = enabled_backends(platform)
    entries = []
    restarts = {
        "authz": [{"kind": "Deployment", "name": "authz"}],
        "object-storage": [{"kind": "StatefulSet", "name": "seaweedfs"}],
        **{name: [{"kind": "StatefulSet", "name": name}] for name in backends},
    }
    for workload in workloads(
        platform, tenant=selection.tenant, datastream=selection.datastream,
        ingest_credential=selection.ingest_credential, query_credentials=selection.query_credentials,
    ):
        shape = (
            "storage-identity" if workload.name in backends
            else "grafana-admin" if workload.name == "grafana" else None
        )
        entry = {
            "name": workload.name, "role": workload.vault_role,
            "secrets": [_secret(platform, ref, shape) for ref in workload.secrets],
        }
        if workload.name in restarts:
            entry["restart"] = restarts[workload.name]
        if workload.name in ("collector", "pyroscope"):
            # The collectors discover what to scrape through the cluster API, and the upstream
            # Pyroscope chart finds its own metastore through it.
            entry["apiAccess"] = True
        entries.append(entry)
    files: dict[str, dict[str, str]] = {
        "platform": {"platform.yaml": artifacts["platform.yaml"]},
        "overrides": {name: content for name, content in artifacts.items() if name.endswith("-overrides.yaml")},
        "grafana": {"desired-state.json": artifacts["grafana/desired-state.json"]},
    }
    for kind, profile in (("cluster", COLLECTOR_PROFILE), ("node", NODE_COLLECTOR_PROFILE)):
        files[f"collector-{kind}"] = collector.render_collector(
            platform, stream.tenant, stream.datastream, profile, entry_point=CLUSTER_ENTRY_POINT,
            credential_id=ingest.id, self_monitoring=kind == "cluster" and "metrics" in stream.signals,
        )
    if platform.storage_provider == "seaweedfs":
        files["storage"] = {
            "buckets.txt": "".join(f"{bucket}\n" for bucket in storage.buckets(platform)),
            "storage-init.sh": STORAGE_INIT_SCRIPT.read_text(encoding="utf-8"),
        }
    monolithic = {}
    if platform.profile == "development":
        files["backends"] = {
            f"{name}.yaml": artifacts[f"backends/{name}.yaml"] for name in backends if name in MONOLITHIC
        }
        identities = {name: next(
            binding.identity.ref for key, binding in sorted(platform.bindings.items()) if key.split("-", 1)[0] == name
        ) for name in backends}
        for name in backends:
            if name not in MONOLITHIC:
                continue
            settings = MONOLITHIC[name]
            ports = sorted({
                (f"port-{urlsplit(address).port}", urlsplit(address).port)
                for key, address in platform.gateway.upstreams.items()
                if UPSTREAM_DESTINATION[key.split(".")[0] if "." in key else key] == name
            })
            monolithic[name] = {
                "args": [
                    f"-config.file=/etc/nighthawk/contracts/backends/{name}.yaml", "-config.expand-env=true",
                    *settings.get("extraArgs", []),
                ],
                "identity": identities[name], "dataDir": settings["dataDir"], "readyPort": settings["readyPort"],
                "size": settings["size"], "ports": [{"name": label, "port": port} for label, port in ports],
            }
    cluster_port = next(
        (entry.port for entry in platform.gateway.entry_points if "cluster-gateway" in entry.rules), None,
    )
    if cluster_port is None:
        raise ConfigurationError(
            "gateway: a self-hosted-k8s deployment needs the cluster-gateway entry point, "
            "which collectors inside the cluster deliver through"
        )
    queries = grafana.query_credentials(platform, selection.query_credentials)
    return {
        "profile": platform.profile,
        "images": {
            "platform": "", "seaweedfs": image(matrix, "seaweedfs"), "alloy": image(matrix, "alloy"),
            **{name: image(matrix, name) for name in MONOLITHIC},
        },
        "vault": {"address": vault.address_in_cluster, "kvMount": vault.kv_mount, "authMount": vault.kubernetes_auth_mount},
        "workloads": entries,
        "pki": {
            "mount": vault.pki_mount, "serverRole": vault.server_role, "clientRole": vault.client_role,
            "issuerRole": f"nighthawk-{ISSUER}", "issuerServiceAccount": ISSUER,
        },
        "certificates": _certificates(platform, ingest),
        "gateway": {
            "hostname": platform.gateway.hostname, "grafanaHostname": platform.grafana.hostname,
            "clusterPort": cluster_port,
        },
        "hostAliases": [],
        "files": files,
        "backends": monolithic,
        "grafana": {
            "url": platform.gateway.upstreams["grafana"],
            "adminSecret": _secret(platform, platform.grafana.admin_secret_ref),
            "provisionArguments": [
                part for pair, credential in sorted(queries.items())
                if credential.id in selection.query_credentials for part in ("--credential", credential.id)
            ],
        },
        "collectors": {
            "credential": _secret(platform, ingest.secret_ref),
            "trustSecret": GATEWAY_TRUST,
            "clientCertificate": "collector-client-tls" if ingest.certificate_identity is not None else "",
            "gatewayMetricsAddress": f"traefik-internal.{GATEWAY_NAMESPACE}.svc:{metrics_port}",
        },
        "notice": DEVELOPMENT_NOTICE if platform.profile == "development" else "",
    }


def _replace_prefix(value: object, old: str, new: str) -> object:
    if isinstance(value, dict):
        return {key: _replace_prefix(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_prefix(item, old, new) for item in value]
    if isinstance(value, str) and (value == old or value.startswith(old + "/")):
        return new + value[len(old):]
    return value


def _image_parts(matrix: dict, name: str) -> dict:
    pin = mapping(mapping(matrix["container_images"])[name])
    registry, _, repository = string(pin["repository"]).partition("/")
    return {"registry": registry, "repository": repository, "tag": string(pin["tag"]), "digest": string(pin["digest"])}


def _backend_secret(platform: Platform, backend: str) -> str:
    identity = next(
        binding.identity.ref for key, binding in sorted(platform.bindings.items()) if key.split("-", 1)[0] == backend
    )
    return f"vault-{backend}-{identity}"


STORAGE_CA_VOLUME = {"name": "storage-ca", "secret": {"secretName": "storage-server-tls", "items": [{"key": "ca.crt", "path": "ca.pem"}]}}
STORAGE_CA_MOUNT = {"name": "storage-ca", "mountPath": "/etc/nighthawk/storage", "readOnly": True}
STORAGE_CA_ENV = {"name": "SSL_CERT_FILE", "value": "/etc/nighthawk/storage/ca.pem"}
OVERRIDES_VOLUME = {"name": "overrides", "configMap": {"name": "nighthawk-overrides"}}
OVERRIDES_MOUNT = {"name": "overrides", "mountPath": "/etc/nighthawk/overrides", "readOnly": True}


def loki_values(platform: Platform, matrix: dict, artifacts: dict[str, str]) -> dict:
    """The upstream chart as packaging only: one replica running the configuration reviewed under Compose."""
    parts = _image_parts(matrix, "loki")
    config = _replace_prefix(yaml.safe_load(artifacts["backends/loki.yaml"]), "/data", "/var/loki")
    bucket = platform.bindings["loki-chunks"].bucket
    return {
        "fullnameOverride": "loki",
        "deploymentMode": "Monolithic",
        "loki": {
            "image": parts, "structuredConfig": config, "auth_enabled": True,
            "podLabels": {COMPONENT_LABEL: "loki"},
            # The chart insists on these although structuredConfig replaces its whole configuration.
            "storage": {"type": "s3", "bucketNames": {name: bucket for name in ("chunks", "ruler", "admin")}},
        },
        "serviceAccount": {"create": False, "name": "loki"},
        "singleBinary": {
            "replicas": 1,
            "extraEnv": [STORAGE_CA_ENV],
            "extraEnvFrom": [{"secretRef": {"name": _backend_secret(platform, "loki")}}],
            "extraVolumes": [STORAGE_CA_VOLUME, OVERRIDES_VOLUME],
            "extraVolumeMounts": [STORAGE_CA_MOUNT, OVERRIDES_MOUNT],
            "persistence": {"enabled": True, "size": "10Gi"},
        },
        # Everything below is something the chart adds by default that this deployment does
        # not use: its own gateway, caches, canary, rule sidecar, and self-test.
        "write": {"replicas": 0}, "read": {"replicas": 0}, "backend": {"replicas": 0},
        "gateway": {"enabled": False},
        "chunksCache": {"enabled": False},
        "resultsCache": {"enabled": False},
        "lokiCanary": {"enabled": False},
        "test": {"enabled": False},
        "sidecar": {"rules": {"enabled": False}},
        "memcached": {"enabled": False},
        # Namespaced, and with the rule sidecar off, the chart creates no role at all.
        "rbac": {"namespaced": True},
        "monitoring": {"selfMonitoring": {"enabled": False}},
    }


def pyroscope_values(platform: Platform, matrix: dict, artifacts: dict[str, str]) -> dict:
    parts = _image_parts(matrix, "pyroscope")
    return {
        "pyroscope": {
            "fullnameOverride": "pyroscope",
            "replicaCount": 1,
            "image": {"registry": parts["registry"], "repository": parts["repository"], "tag": f"{parts['tag']}@{parts['digest']}"},
            "extraLabels": {COMPONENT_LABEL: "pyroscope"},
            "serviceAccount": {"create": False, "name": "pyroscope"},
            "config": artifacts["backends/pyroscope.yaml"],
            # The chart passes its own runtime-config flag first; the last one given is the one that counts.
            "extraArgs": {
                "config.expand-env": "true", "self-profiling.disable-push": "true", "log.level": "info",
                "runtime-config.file": "/etc/nighthawk/overrides/pyroscope-overrides.yaml",
            },
            "extraEnvVars": {"SSL_CERT_FILE": STORAGE_CA_ENV["value"]},
            "extraEnvFrom": [{"secretRef": {"name": _backend_secret(platform, "pyroscope")}}],
            "extraVolumes": [STORAGE_CA_VOLUME, OVERRIDES_VOLUME],
            "extraVolumeMounts": [STORAGE_CA_MOUNT, OVERRIDES_MOUNT],
            "persistence": {"enabled": True, "size": "10Gi"},
        },
        "alloy": {"enabled": False},
        "agent": {"enabled": False},
        "minio": {"enabled": False},
    }


def grafana_values(platform: Platform, matrix: dict) -> dict:
    parts = _image_parts(matrix, "grafana")
    external = next(
        (entry.port for entry in platform.gateway.entry_points if platform.gateway.grafana_entry_point in entry.rules), 443,
    )
    port = urlsplit(platform.gateway.upstreams["grafana"]).port
    return {
        "fullnameOverride": "grafana",
        "replicas": 1,
        "image": {"registry": parts["registry"], "repository": parts["repository"], "tag": parts["tag"], "sha": parts["digest"].removeprefix("sha256:")},
        "podLabels": {COMPONENT_LABEL: "grafana"},
        "serviceAccount": {"create": False, "name": "grafana"},
        "rbac": {"create": False},
        "testFramework": {"enabled": False},
        "admin": {"existingSecret": f"vault-grafana-{platform.grafana.admin_secret_ref}", "userKey": "admin-user", "passwordKey": "admin-password"},
        "service": {"port": port, "targetPort": 3000},
        # A StatefulSet, so its volume outlives the release like every other volume here.
        "useStatefulSet": True,
        "persistence": {"enabled": True, "size": "5Gi"},
        "initChownData": {"enabled": False},
        "env": {
            "GF_AUTH_ANONYMOUS_ENABLED": "false",
            "GF_USERS_ALLOW_SIGN_UP": "false",
            "GF_SERVER_DOMAIN": platform.grafana.hostname,
            "GF_SERVER_ROOT_URL": f"https://{platform.grafana.hostname}:{external}/",
            "GF_ANALYTICS_REPORTING_ENABLED": "false",
            "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
            "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
            # Data sources call the gateway, whose certificate is signed by the authority in Vault.
            "SSL_CERT_FILE": "/run/nighthawk/grafana/gateway-ca.pem",
        },
        "extraSecretMounts": [{
            "name": "gateway-ca", "secretName": GATEWAY_TRUST, "mountPath": "/run/nighthawk/grafana",
            "readOnly": True, "items": [{"key": "ca.crt", "path": "gateway-ca.pem"}],
        }],
        "hostAliases": [],
    }


def traefik_values(platform: Platform, matrix: dict, rules: list[dict], artifacts: dict[str, str]) -> dict:
    """The upstream chart with the route table this project renders, through the file provider."""
    parts = _image_parts(matrix, "traefik")
    metrics = next(rule["port"] for rule in rules if rule["destination"] == "gateway-metrics")
    ports: dict[str, object] = {
        # The chart's defaults; this deployment defines its own entry points.
        "web": None, "websecure": None,
        "metrics": {"port": metrics, "exposedPort": metrics, "expose": {"default": False, "internal": True}, "protocol": "TCP"},
    }
    for entry in platform.gateway.entry_points:
        external = entry.scope == "restricted-external"
        ports[entry.name] = {
            # The proxy runs unprivileged, so it listens above 1024 and the service maps the contract's port.
            "port": gateway_container_port(entry.port),
            "exposedPort": entry.port, "protocol": "TCP",
            "expose": {"default": external, "internal": True},
            "http": {"aliasHeadersStrategy": "delete", "tls": {"enabled": True}},
        }
    dynamic = yaml.safe_load(artifacts["gateway/traefik-dynamic.yaml"])
    tls = dynamic.pop("tls")
    options = tls["options"]["default"]
    secret = "gateway-server-tls"
    return {
        "fullnameOverride": "traefik",
        "image": {"registry": parts["registry"], "repository": parts["repository"].removeprefix("library/"), "tag": f"{parts['tag']}@{parts['digest']}"},
        "deployment": {"replicas": 1, "podLabels": {COMPONENT_LABEL: "gateway"}},
        "providers": {
            # Routes come from the file this project renders. The chart restarts the proxy when
            # that file changes, so nothing depends on a file watcher.
            "file": {"enabled": True, "watch": False, "content": dynamic},
            # Only for the certificate and the client authority: the proxy watches their Secret
            # itself, so a renewed certificate is served without a restart.
            "kubernetesCRD": {"enabled": True, "namespaces": [GATEWAY_NAMESPACE], "allowCrossNamespace": False},
            "kubernetesIngress": {"enabled": False},
        },
        "ingressClass": {"enabled": False},
        "ingressRoute": {"dashboard": {"enabled": False}},
        "api": {"dashboard": False},
        # A role in the gateway's own namespace only, where its certificate is the only Secret.
        "rbac": {"enabled": True, "namespaced": True},
        # The chart leaves the flag out when watching is off, and the proxy's default is on.
        "additionalArguments": ["--providers.file.watch=false"],
        "ports": ports,
        "metrics": {"prometheus": {"entryPoint": "metrics"}},
        "service": {
            "type": "LoadBalancer",
            # The address pods use; only this service carries the in-cluster entry point.
            "additionalServices": {"internal": {"type": "ClusterIP"}},
        },
        "extraObjects": [
            {
                "apiVersion": "traefik.io/v1alpha1", "kind": "TLSStore",
                "metadata": {"name": "default", "namespace": GATEWAY_NAMESPACE},
                "spec": {"defaultCertificate": {"secretName": secret}},
            },
            {
                "apiVersion": "traefik.io/v1alpha1", "kind": "TLSOption",
                "metadata": {"name": "default", "namespace": GATEWAY_NAMESPACE},
                "spec": {
                    "minVersion": options["minVersion"],
                    "clientAuth": {"secretNames": [secret], "clientAuthType": options["clientAuth"]["clientAuthType"]},
                },
            },
        ],
    }


def _check_upstreams(platform: Platform) -> None:
    """The gateway is in another namespace than what it calls, so each address names the namespace."""
    addresses = {"auth_service": platform.gateway.auth_service, **{f"upstreams.{key}": value for key, value in platform.gateway.upstreams.items()}}
    services = {"auth_service": "authz", **{f"upstreams.{key}": UPSTREAM_DESTINATION[key.split(".")[0]] for key in platform.gateway.upstreams}}
    for field, address in sorted(addresses.items()):
        expected = f"{services[field]}.{NAMESPACE}.svc"
        if urlsplit(address).hostname != expected:
            raise ConfigurationError(
                f"gateway.{field}: {address} must name the service the deployment creates, {expected}, "
                "because the gateway runs in a namespace of its own"
            )


def gateway_container_port(port: int) -> int:
    """The proxy runs unprivileged, so a contract port below 1024 is a container port above it."""
    return port if port > 1024 else port + 8000


# Where each name of the network contract lives in the cluster: namespace and component label.
# Names that are not here are outside the platform's namespaces and get no policy.
def _placement(platform: Platform) -> dict[str, list[tuple[str, str]]]:
    backends = enabled_backends(platform)
    placement = {
        "gateway": [(GATEWAY_NAMESPACE, "gateway")], "gateway-metrics": [(GATEWAY_NAMESPACE, "gateway")],
        "auth-service": [(NAMESPACE, "auth-service")], "grafana": [(NAMESPACE, "grafana")],
        "collector": [(NAMESPACE, "collector")], "provisioner": [(NAMESPACE, "provisioner")],
        "signal-backend": [(NAMESPACE, name) for name in backends],
        **{name: [(NAMESPACE, name)] for name in backends},
    }
    if platform.storage_provider == "seaweedfs":
        placement["object-storage"] = [(NAMESPACE, "object-storage")]
    if platform.profile == "production":
        placement["kafka"] = [(NAMESPACE, "kafka")]
        placement["database"] = [(NAMESPACE, "database")]
    return placement


# Contract names that are not pods of the platform, as a policy peer.
SPECIAL_PEERS = {
    "cluster-workload": {"kind": "all-pods"}, "cluster-dns": {"kind": "dns"},
    "cluster-server": {"kind": "cluster-api"}, "cluster-node": {"kind": "nodes"},
    "authorized-collector": {"kind": "anywhere"},
}


def network_policies(platform: Platform, rules: list[dict]) -> list[dict]:
    """One policy per component: what it may receive and open, each entry naming its contract rule.

    Everything else is denied by the default-deny policy the chart adds to each namespace.
    """
    placement = _placement(platform)
    selected = {rule_id for entry in platform.gateway.entry_points for rule_id in entry.rules}
    policies: dict[tuple[str, str], dict] = {}

    def policy(namespace: str, component: str) -> dict:
        return policies.setdefault((namespace, component), {
            "namespace": namespace, "component": component, "ingress": [], "egress": [],
        })

    def peers(name: str) -> list[dict]:
        if name in placement:
            return [{"kind": "component", "namespace": namespace, "component": component} for namespace, component in placement[name]]
        return [SPECIAL_PEERS[name]] if name in SPECIAL_PEERS else []

    for rule in sorted(rules, key=lambda item: item["id"]):
        if rule["destination"] == "gateway" and rule["id"] not in selected:
            continue
        port = gateway_container_port(rule["port"]) if rule["destination"] == "gateway" else rule["port"]
        entry = {"rule": rule["id"], "port": port, "protocol": rule["protocol"].upper()}
        sources, destinations = peers(rule["source"]), peers(rule["destination"])
        for namespace, component in placement.get(rule["destination"], []):
            for peer in sources:
                policy(namespace, component)["ingress"].append({**entry, "peer": peer})
        for namespace, component in placement.get(rule["source"], []):
            for peer in destinations:
                policy(namespace, component)["egress"].append({**entry, "peer": peer})
    # Name resolution is needed by every pod; the contract states it once for all workloads.
    dns = [rule for rule in rules if rule["source"] == "cluster-workload" and rule["destination"] == "cluster-dns"]
    for item in policies.values():
        item["egress"] += [
            {"rule": rule["id"], "port": rule["port"], "protocol": rule["protocol"].upper(), "peer": {"kind": "dns"}}
            for rule in sorted(dns, key=lambda rule: rule["id"])
        ]
    for item in policies.values():
        item["rules"] = sorted({entry["rule"] for entry in item["ingress"] + item["egress"]})
        # One entry per peer with all its ports: the same allowances in far fewer filter rules.
        for direction in ("ingress", "egress"):
            grouped: dict[str, dict] = {}
            for entry in item[direction]:
                group = grouped.setdefault(_dump(entry["peer"]), {"peer": entry["peer"], "ports": []})
                port = {"port": entry["port"], "protocol": entry["protocol"], "rule": entry["rule"]}
                if not any((known["port"], known["protocol"]) == (port["port"], port["protocol"]) for known in group["ports"]):
                    group["ports"].append(port)
            item[direction] = [grouped[key] for key in sorted(grouped)]
    return [policies[key] for key in sorted(policies)]


PRODUCTION_NOT_RENDERED = (
    "Kubernetes workloads are rendered for the development profile only; the production profile "
    "(distributed backends, Kafka, replicated storage, shared PostgreSQL) is not implemented yet"
)

# How long each release may take to become healthy, in seconds.
WAIT_SECONDS = 600


def _addon_image(matrix: dict, key: str) -> dict:
    pin = mapping(mapping(matrix["kubernetes_images"])[key])
    return {"repository": string(pin["repository"]), "tag": f"{string(pin['tag'])}@{string(pin['digest'])}"}


def _addon_values(matrix: dict) -> dict[str, dict]:
    certificate_images = {
        part: {"image": {"digest": string(mapping(mapping(matrix["kubernetes_images"])[f"cert_manager_{part}"])["digest"])}}
        for part in ("webhook", "cainjector", "startupapicheck")
    }
    return {
        "metallb": {
            "controller": {"image": _addon_image(matrix, "metallb_controller")},
            # Layer 2 announcements only: no routing daemon is installed.
            "speaker": {"image": _addon_image(matrix, "metallb_speaker"), "frr": {"enabled": False}},
            "frrk8s": {"enabled": False},
        },
        "cert_manager": {
            "crds": {"enabled": True},
            "image": {"digest": string(mapping(mapping(matrix["kubernetes_images"])["cert_manager_controller"])["digest"])},
            **certificate_images,
        },
        "vault_secrets_operator": {
            "controller": {
                "manager": {"image": _addon_image(matrix, "vault_secrets_operator")},
                "kubeRbacProxy": {"image": _addon_image(matrix, "kube_rbac_proxy")},
            },
            # The chart's own test pod names its image without a version.
            "tests": {"enabled": False},
        },
        "strimzi": {"watchNamespaces": [NAMESPACE]},
    }


# Add-ons a cluster needs before the platform: (chart key in the matrix, namespace, profiles).
ADDONS = (
    ("metallb", "metallb-system", ("development", "production")),
    ("cert_manager", "cert-manager", ("development", "production")),
    ("vault_secrets_operator", "vault-secrets-operator", ("development", "production")),
    # One node has no use for replicated storage, an ingest log, or a replicated database.
    ("longhorn", "longhorn-system", ("production",)),
    ("strimzi", "strimzi", ("production",)),
    ("cloudnative_pg", "cnpg-system", ("production",)),
)


def addons(platform: Platform, matrix: dict) -> tuple[list[dict], dict[str, str]]:
    """The add-on releases of this profile, in order, and their values files."""
    releases, files = [], {}
    values = _addon_values(matrix)
    for key, namespace, profiles in ADDONS:
        if platform.profile not in profiles:
            continue
        name = key.replace("_", "-")
        files[f"kubernetes/values/addon-{name}.yaml"] = _dump(values.get(key, {}))
        releases.append({
            "kind": "release", "name": name, "chart": key, "namespace": namespace,
            "values": [f"addon-{name}.yaml"], "wait_seconds": WAIT_SECONDS,
        })
    return releases, files


def charts(matrix: dict, keys: set[str]) -> dict[str, dict]:
    """Where each upstream chart comes from and the digest its package must have."""
    pinned = mapping(matrix["helm_charts"])
    missing = sorted(keys - pinned.keys())
    if missing:
        raise ConfigurationError(f"the compatibility matrix locks no chart for: {', '.join(missing)}")
    return {
        key: {field: string(mapping(pinned[key])[field]) for field in ("chart", "repository", "version", "sha256")}
        for key in sorted(keys)
    }


def _stage(name: str, **enabled: bool) -> dict:
    return {"name": name, "chart": PLATFORM_CHART, "values": ["platform.yaml", f"{name}.yaml"], "stage": enabled}


def render_kubernetes(
    platform: Platform, rules: list[dict], matrix: dict, artifacts: dict[str, str], selection: Selection = Selection(),
) -> dict[str, str]:
    """Everything a deployment installs, as files under `kubernetes/`. Holds no secret and reads none."""
    metrics_port = next(rule["port"] for rule in rules if rule["destination"] == "gateway-metrics")
    shared = platform_values(platform, matrix, artifacts, selection, metrics_port)
    _check_upstreams(platform)
    if platform.profile != "development":
        raise ConfigurationError(PRODUCTION_NOT_RENDERED)
    backends = enabled_backends(platform)
    chart_values: dict[str, dict] = {"traefik": traefik_values(platform, matrix, rules, artifacts), "grafana": grafana_values(platform, matrix)}
    if "loki" in backends:
        chart_values["loki"] = loki_values(platform, matrix, artifacts)
    if "pyroscope" in backends:
        chart_values["pyroscope"] = pyroscope_values(platform, matrix, artifacts)
    expected_secrets = sorted(
        {f"vault-{entry['name']}-{secret['ref']}" for entry in shared["workloads"] for secret in entry["secrets"]}
        | {certificate["secretName"] for certificate in shared["certificates"]}
    )
    policies = network_policies(platform, rules)
    api_port = next(rule["port"] for rule in rules if rule["id"] == "operator-cluster-api")
    network = {"networkPolicies": policies, "clusterApi": {"addresses": [], "port": api_port}}
    stages = [
        # Default deny first, in both namespaces, so nothing ever runs without it.
        # Applied resource by resource rather than as a release, so a slow policy engine is
        # never handed every policy at once.
        {**_stage("nighthawk-network", network=True), "kind": "manifests", "cluster_api": True, "extra_values": network},
        {
            **_stage("nighthawk-gateway-network", network=True), "kind": "manifests", "namespace": GATEWAY_NAMESPACE,
            "cluster_api": True, "extra_values": network,
        },
        {**_stage("nighthawk-foundation", foundation=True), "expect_secrets": expected_secrets},
        _stage("nighthawk-storage", storage=True),
        {**_stage("nighthawk-storage-init", storageInit=True), "wait_job": "storage-init"},
        _stage("nighthawk-backends", backends=True),
        *({"name": name, "chart": name, "values": [f"{name}.yaml"]} for name in ("loki", "pyroscope") if name in chart_values),
        _stage("nighthawk-authz", authz=True),
        {
            **_stage("nighthawk-gateway-identity", gatewayIdentity=True), "namespace": GATEWAY_NAMESPACE,
            "expect_secrets": [gateway_certificate(platform)["secretName"]],
            "extra_values": {"certificates": [gateway_certificate(platform)]},
        },
        {
            "name": "traefik", "chart": "traefik", "values": ["traefik.yaml"], "namespace": GATEWAY_NAMESPACE,
            "gateway_service": "traefik-internal",
        },
        {"name": "grafana", "chart": "grafana", "values": ["grafana.yaml"], "host_aliases": True},
        {**_stage("grafana-provision", provision=True), "kind": "job", "job": "grafana-provision", "host_aliases": True},
        {**_stage("nighthawk-collectors", collectors=True), "host_aliases": True},
    ]
    files = {"kubernetes/values/platform.yaml": _dump(shared)}
    addon_releases, addon_files = addons(platform, matrix)
    files.update(addon_files)
    releases = []
    for stage in stages:
        enabled = stage.pop("stage", None)
        if enabled is not None:
            files[f"kubernetes/values/{stage['name']}.yaml"] = _dump({"enabled": enabled, **stage.pop("extra_values", {})})
        releases.append({"kind": "release", "namespace": NAMESPACE, "wait_seconds": WAIT_SECONDS, **stage})
    for release in releases:
        # The upstream chart names in releases.yaml are matrix keys.
        release["chart"] = release["chart"].replace("-", "_") if release["chart"] != PLATFORM_CHART else PLATFORM_CHART
    for name, values in chart_values.items():
        files[f"kubernetes/values/{name}.yaml"] = _dump(values)
    upstream = {release["chart"] for release in releases + addon_releases if release["chart"] != PLATFORM_CHART}
    files["kubernetes/releases.yaml"] = (
        "# Generated by `nighthawk render-contracts`; do not edit. The deployment installs these in order.\n"
        + _dump({
            "profile": platform.profile, "namespace": NAMESPACE, "gateway_namespace": GATEWAY_NAMESPACE,
            "notice": shared["notice"],
            "gateway_hostnames": [platform.gateway.hostname, platform.grafana.hostname],
            "platform_chart": PLATFORM_CHART,
            "charts": charts(matrix, upstream),
            "addons": addon_releases,
            "releases": releases,
        })
    )
    return files
