"""Strict, non-secret configuration loading and cross-resource validation."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator
from yaml.nodes import MappingNode


ROOT = Path(__file__).resolve().parent.parent
SIGNAL_BUCKETS = {
    "metrics": ("mimir-blocks", "mimir-ruler", "mimir-alertmanager"),
    "logs": ("loki-chunks",),
    "traces": ("tempo-traces",),
    "profiles": ("pyroscope-profiles",),
}
BUCKET_OWNER = {
    bucket: signal for signal, buckets in SIGNAL_BUCKETS.items() for bucket in buckets
}
BUCKET_OWNER["loki-ruler"] = "logs"
MAX_RETENTION_HOURS = 2_562_047


class ConfigurationError(ValueError):
    """Configuration cannot safely be used."""


class StrictLoader(yaml.SafeLoader):
    """Reject duplicate explicit keys, retaining YAML merge override semantics."""


def _construct_mapping(loader: StrictLoader, node: MappingNode, deep: bool = False) -> dict:
    keys: set[str] = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConfigurationError(f"line {key_node.start_mark.line + 1}: keys must be strings")
        if key in keys:
            raise ConfigurationError(f"line {key_node.start_mark.line + 1}: duplicate key {key!r}")
        keys.add(key)
    loader.flatten_mapping(node)
    return loader.construct_mapping(node, deep=deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def mapping(value: object) -> dict:
    if not isinstance(value, dict):
        raise ConfigurationError("expected a mapping")
    return value


def sequence(value: object) -> list:
    if not isinstance(value, list):
        raise ConfigurationError("expected a list")
    return value


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("expected a string")
    return value


def integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError("expected an integer")
    return value


def boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError("expected a boolean")
    return value


def _check_json_tree(value: object, ancestors: frozenset[int] = frozenset()) -> None:
    if len(ancestors) > 64:
        raise ConfigurationError("configuration nesting exceeds 64 levels")
    if isinstance(value, (dict, list)):
        if id(value) in ancestors:
            raise ConfigurationError("recursive YAML aliases are not allowed")
        nested = ancestors | {id(value)}
        items = value.values() if isinstance(value, dict) else value
        if isinstance(value, dict):
            for key in value:
                _check_json_tree(key, nested)
        for item in items:
            _check_json_tree(item, nested)
    elif isinstance(value, str) and any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigurationError("control characters are not allowed in configuration strings")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ConfigurationError("non-finite numbers are not allowed")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ConfigurationError(f"unsupported YAML value type: {type(value).__name__}")


def load_yaml(path: Path) -> object:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"cannot load {path}: {error}") from error
    _check_json_tree(document)
    return document


def _validate_document(document: object, path: Path, schema_name: str) -> dict:
    schema = json.loads((ROOT / "config" / schema_name).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: str(list(error.path)))
    if errors:
        details = []
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            details.append(f"{location}: {error.message}")
        raise ConfigurationError(f"{path}:\n" + "\n".join(details))
    return mapping(document)


def load_document(path: Path, schema_name: str) -> dict:
    return _validate_document(load_yaml(path), path, schema_name)


@dataclass(frozen=True)
class SignalPolicy:
    retention_hours: int
    # Samples per second for metrics (Mimir enforces a sample rate), bytes per second otherwise.
    ingestion_rate: int
    query_concurrency: int


@dataclass(frozen=True)
class Stream:
    tenant: str
    datastream: str
    backend_id: str
    signals: dict[str, SignalPolicy]
    drop_fields: tuple[str, ...]
    allow_privileged_profiling: bool


@dataclass(frozen=True)
class Credential:
    id: str
    secret_ref: str
    tenant: str
    datastream: str
    backend_id: str
    permission: str
    certificate_identity: str | None


@dataclass(frozen=True)
class SecretReference:
    """A key at a path inside the Vault key-value mount."""

    path: str
    key: str


@dataclass(frozen=True)
class Vault:
    """Where Vault is and which mounts and roles the platform uses. Never a credential."""

    address: str
    namespace: str | None
    ca_file: str | None
    kv_mount: str
    pki_mount: str
    server_role: str
    client_role: str
    # Self-hosted Kubernetes only: the mount where the cluster's service accounts authenticate,
    # and the address the cluster reaches Vault at when it differs from `address`.
    kubernetes_auth_mount: str | None = None
    cluster_address: str | None = None

    @property
    def address_in_cluster(self) -> str:
        return self.cluster_address or self.address

    @property
    def loopback(self) -> bool:
        host = urlsplit(self.address).hostname or ""
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host == "localhost"

    @property
    def plaintext(self) -> bool:
        return urlsplit(self.address).scheme != "https"


@dataclass(frozen=True)
class TLS:
    enabled: bool
    # "system" roots, the platform "pki" authority, or a CA certificate stored as a "secret".
    trust: str
    ca_secret_ref: str | None


@dataclass(frozen=True)
class Identity:
    type: str
    ref: str


@dataclass(frozen=True)
class Capabilities:
    versioning: bool
    lifecycle: bool
    workload_identity: bool


@dataclass(frozen=True)
class StorageBinding:
    protocol: str
    endpoint: str
    region: str
    bucket: str
    force_path_style: bool
    tls: TLS
    identity: Identity
    capabilities: Capabilities


@dataclass(frozen=True)
class EntryPoint:
    """One gateway listener; several selected network rules may share its port."""

    name: str
    port: int
    scope: str
    rules: tuple[str, ...]


@dataclass(frozen=True)
class Gateway:
    hostname: str
    entry_points: tuple[EntryPoint, ...]
    grafana_entry_point: str
    auth_service: str
    upstreams: dict[str, str]
    revoked_certificate_fingerprints: tuple[str, ...]

    def entry_point(self, rule_id: str) -> EntryPoint:
        for entry in self.entry_points:
            if rule_id in entry.rules:
                return entry
        raise ConfigurationError(f"gateway: {rule_id!r} is not a selected entry point")

    def url(self, rule_id: str) -> str:
        return f"https://{self.hostname}:{self.entry_point(rule_id).port}"


@dataclass(frozen=True)
class Grafana:
    hostname: str
    admin_secret_ref: str


@dataclass(frozen=True)
class Platform:
    schema_version: int
    deployment: str
    profile: str
    vault: Vault
    gateway: Gateway
    grafana: Grafana
    storage_provider: str
    bindings: dict[str, StorageBinding]
    secrets: dict[str, SecretReference]
    streams: tuple[Stream, ...]
    credentials: tuple[Credential, ...]
    # Only a self-hosted Kubernetes deployment has a cluster of its own to join nodes to.
    cluster_join_token_secret_ref: str | None = None

    def manifest(self) -> dict:
        return asdict(self)


def _require_secret(ref: str, secrets: dict[str, SecretReference], context: str) -> None:
    if ref not in secrets:
        raise ConfigurationError(f"{context}: unknown secret reference {ref!r}")


def _storage(
    data: dict, deployment: str, secrets: dict[str, SecretReference], required: set[str]
) -> tuple[str, dict[str, StorageBinding]]:
    provider = string(data["provider"])
    expected = "aws" if deployment == "aws" else "seaweedfs"
    if provider != expected:
        raise ConfigurationError(f"{deployment} requires {expected} storage, not {provider}")
    bindings: dict[str, StorageBinding] = {}
    bucket_names: set[str] = set()
    identity_owners: dict[str, str] = {}
    raw_bindings = mapping(data["bindings"])
    missing = required - raw_bindings.keys()
    if missing:
        raise ConfigurationError(f"missing storage bindings: {', '.join(sorted(missing))}")
    for name, raw in sorted(raw_bindings.items()):
        item = mapping(raw)
        endpoint = string(item["endpoint"])
        try:
            url = urlsplit(endpoint)
            if url.port == 0:
                raise ValueError("port must be greater than zero")
        except ValueError as error:
            raise ConfigurationError(f"{name}: invalid endpoint: {error}") from error
        if (
            url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.path not in ("", "/")
            or url.query or url.fragment or any(char.isspace() for char in endpoint)
        ):
            raise ConfigurationError(f"{name}: endpoint must be an HTTPS origin without credentials")
        bucket = string(item["bucket"])
        if ".." in bucket or ".-" in bucket or "-." in bucket:
            raise ConfigurationError(f"{name}: invalid bucket name {bucket!r}")
        try:
            ipaddress.ip_address(bucket)
        except ValueError:
            pass
        else:
            raise ConfigurationError(f"{name}: bucket must not be an IP address")
        if bucket in bucket_names:
            raise ConfigurationError(f"{name}: storage buckets must not overlap")
        bucket_names.add(bucket)
        tls = mapping(item["tls"])
        ca = tls["ca_secret_ref"]
        trust = string(tls.get("trust", "system" if ca is None else "secret"))
        if (trust == "secret") != (ca is not None):
            raise ConfigurationError(f"{name}: tls.ca_secret_ref is required for, and only for, tls.trust: secret")
        if ca is not None:
            _require_secret(string(ca), secrets, name)
        # The local SeaweedFS server certificate is signed by the platform PKI authority.
        if (provider == "seaweedfs") != (trust == "pki"):
            raise ConfigurationError(
                f"{name}: local SeaweedFS storage requires tls.trust: pki, and only local storage may use it"
            )
        identity = mapping(item["identity"])
        identity_type, ref = string(identity["type"]), string(identity["ref"])
        capabilities = mapping(item["capabilities"])
        workload_identity = boolean(capabilities["workload_identity"])
        if provider == "aws":
            if identity_type != "irsa" or not re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+", ref):
                raise ConfigurationError(f"{name}: AWS requires an IRSA role ARN")
            if not workload_identity:
                raise ConfigurationError(f"{name}: AWS requires workload identity capability")
        else:
            if identity_type != "secret" or workload_identity:
                raise ConfigurationError(f"{name}: local SeaweedFS requires a secret identity")
            _require_secret(ref, secrets, name)
        owner = BUCKET_OWNER[name]
        if ref in identity_owners and identity_owners[ref] != owner:
            raise ConfigurationError(f"{name}: storage identity is shared across signal backends")
        identity_owners[ref] = owner
        bindings[name] = StorageBinding(
            "s3", endpoint, string(item["region"]), bucket, boolean(item["force_path_style"]),
            TLS(True, trust, ca), Identity(identity_type, ref),
            Capabilities(boolean(capabilities["versioning"]), boolean(capabilities["lifecycle"]), workload_identity),
        )
    return provider, bindings


SCOPE_EXPOSURE = ("loopback", "private", "restricted-external")
UPSTREAM_SIGNAL = {
    "metrics": "metrics", "logs": "logs", "profiles": "profiles",
    "traces.query": "traces", "traces.otlp_grpc": "traces", "traces.otlp_http": "traces",
}
UPSTREAM_DESTINATION = {
    "metrics": "mimir", "logs": "loki", "traces": "tempo", "profiles": "pyroscope", "grafana": "grafana",
}


def _vault(data: dict, profile: str, deployment: str) -> Vault:
    pki = mapping(data["pki"])
    vault = Vault(
        string(data["address"]), data.get("namespace"), data.get("ca_file"), string(data["kv_mount"]),
        string(pki["mount"]), string(pki["server_role"]), string(pki["client_role"]),
    )
    if "kubernetes_auth" in data:
        if deployment != "self-hosted-k8s":
            raise ConfigurationError(f"vault: kubernetes_auth applies to a self-hosted-k8s deployment only, not {deployment}")
        cluster = mapping(data["kubernetes_auth"])
        vault = replace(vault, kubernetes_auth_mount=string(cluster["mount"]), cluster_address=cluster.get("cluster_address"))
        if vault.kubernetes_auth_mount in (vault.kv_mount, vault.pki_mount):
            raise ConfigurationError("vault: kubernetes_auth.mount must differ from kv_mount and pki.mount")
        if vault.cluster_address is not None:
            in_cluster = urlsplit(vault.cluster_address)
            try:
                in_cluster.port
            except ValueError as error:
                raise ConfigurationError(f"vault: invalid kubernetes_auth.cluster_address: {error}") from error
            if in_cluster.scheme == "http" and profile == "production":
                raise ConfigurationError(
                    f"vault: profile: production refuses the plaintext kubernetes_auth.cluster_address {vault.cluster_address}"
                )
    try:
        port = urlsplit(vault.address).port
    except ValueError as error:
        raise ConfigurationError(f"vault: invalid address: {error}") from error
    if port == 0:
        raise ConfigurationError("vault: invalid address: port must be greater than zero")
    if vault.plaintext and not vault.loopback:
        raise ConfigurationError(f"vault: {vault.address} is plaintext; only a loopback address may use http")
    if profile == "production" and (vault.plaintext or vault.loopback):
        raise ConfigurationError(
            f"vault: profile: production refuses the plaintext or loopback address {vault.address}"
        )
    if vault.kv_mount == vault.pki_mount:
        raise ConfigurationError("vault: kv_mount and pki.mount must differ")
    if vault.server_role == vault.client_role:
        raise ConfigurationError("vault: pki.server_role and pki.client_role must differ")
    return vault


def _gateway(data: dict, enabled_signals: set[str], rules: list[dict]) -> Gateway:
    listeners = {rule["id"]: rule for rule in rules if rule["destination"] == "gateway"}
    by_port: dict[int, list[dict]] = {}
    for raw in sequence(data["entry_points"]):
        rule_id = string(raw)
        if rule_id not in listeners:
            raise ConfigurationError(
                f"gateway: entry point {rule_id!r} is not a network rule whose destination is the gateway"
            )
        by_port.setdefault(listeners[rule_id]["port"], []).append(listeners[rule_id])
    # A listener shared by several rules is treated as exposed as its most exposed rule.
    entry_points = tuple(
        EntryPoint(
            f"port-{port}", port,
            max((rule["scope"] for rule in selected), key=SCOPE_EXPOSURE.index),
            tuple(sorted(rule["id"] for rule in selected)),
        )
        for port, selected in sorted(by_port.items())
    )
    grafana_entry_point = string(data["grafana_entry_point"])
    if not any(grafana_entry_point in entry.rules for entry in entry_points):
        raise ConfigurationError(f"gateway: grafana_entry_point {grafana_entry_point!r} is not a selected entry point")

    def check_port(name: str, address: str, destination: str) -> str:
        port = urlsplit(address).port
        if not any(
            rule["source"] == "gateway" and rule["destination"] == destination and rule["port"] == port
            for rule in rules
        ):
            raise ConfigurationError(
                f"gateway: {name} port {port} is not a network rule from gateway to {destination}"
            )
        return address

    auth_service = check_port("auth_service", string(data["auth_service"]), "auth-service")
    raw_upstreams = mapping(data["upstreams"])
    upstreams: dict[str, str] = {}
    for signal, raw in sorted(raw_upstreams.items()):
        parts = {signal: raw} if isinstance(raw, str) else {
            f"{signal}.{key}": value for key, value in sorted(mapping(raw).items())
        }
        for name, address in parts.items():
            upstreams[name] = check_port(f"upstreams.{name}", string(address), UPSTREAM_DESTINATION[signal])
    missing = enabled_signals - raw_upstreams.keys()
    if missing:
        raise ConfigurationError(f"gateway: missing upstreams for enabled signals: {', '.join(sorted(missing))}")
    return Gateway(
        string(data["hostname"]), entry_points, grafana_entry_point, auth_service, upstreams,
        tuple(sorted(string(item) for item in sequence(data["revoked_certificate_fingerprints"]))),
    )


def load_platform(
    path: Path, storage_output: Path | None = None, network: Path | None = None
) -> Platform:
    document = mapping(load_yaml(path))
    if storage_output is not None:
        storage = mapping(load_yaml(storage_output))
        if set(storage) != {"provider", "bindings"}:
            raise ConfigurationError(
                f"{storage_output}: expected the direct storage value; use 'terraform output -json storage'"
            )
        document["storage"] = storage
    if document.get("schema_version") == 1:
        raise ConfigurationError(
            f"{path}: schema_version 1 is no longer supported. Version 2 stores secrets in Vault: add a "
            "`vault` section (address, kv_mount, pki), change each `secrets` entry from {file, key} to "
            "{path, key}, remove gateway.client_ca_secret_ref and gateway.client_ca_key_secret_ref, and "
            "set tls.trust on storage bindings. See docs/02-configuration.md."
        )
    data = _validate_document(document, path, "platform.schema.json")
    secrets = {
        name: SecretReference(string(mapping(item)["path"]), string(mapping(item)["key"]))
        for name, item in sorted(mapping(data["secrets"]).items())
    }
    locations = [(secret.path, secret.key) for secret in secrets.values()]
    if len(set(locations)) != len(locations):
        raise ConfigurationError("secret references must not alias the same Vault path and key")
    streams: list[Stream] = []
    pairs: dict[tuple[str, str], str] = {}
    tenant_ids: set[str] = set()
    backend_ids: set[str] = set()
    required_buckets: set[str] = set()
    for raw_tenant in sequence(data["tenants"]):
        tenant = mapping(raw_tenant)
        tenant_id = string(tenant["id"])
        if tenant_id in tenant_ids:
            raise ConfigurationError(f"duplicate tenant {tenant_id!r}")
        tenant_ids.add(tenant_id)
        for raw_stream in sequence(tenant["datastreams"]):
            stream = mapping(raw_stream)
            stream_id, backend = string(stream["id"]), string(stream["backend_id"])
            pair = (tenant_id, stream_id)
            if pair in pairs:
                raise ConfigurationError(f"duplicate datastream {tenant_id}/{stream_id}")
            if backend in backend_ids:
                raise ConfigurationError(f"duplicate backend ID {backend!r}")
            pairs[pair] = backend
            backend_ids.add(backend)
            signals: dict[str, SignalPolicy] = {}
            for name, raw_signal in sorted(mapping(stream["signals"]).items()):
                signal = mapping(raw_signal)
                hours = int(string(signal["retention"])[:-1])
                if hours > MAX_RETENTION_HOURS or (name == "logs" and hours < 24):
                    raise ConfigurationError(f"{tenant_id}/{stream_id}/{name}: unsupported retention duration")
                rate_field = (
                    "ingestion_rate_samples_per_second" if name == "metrics" else "ingestion_rate_bytes_per_second"
                )
                signals[name] = SignalPolicy(
                    hours, integer(signal[rate_field]), integer(signal["query_concurrency"]),
                )
                required_buckets.update(SIGNAL_BUCKETS[name])
            collection = mapping(stream["collection"])
            streams.append(Stream(
                tenant_id, stream_id, backend, signals,
                tuple(sorted(string(field) for field in sequence(collection["drop_fields"]))),
                boolean(collection["allow_privileged_profiling"]),
            ))
    credentials: list[Credential] = []
    credential_ids: set[str] = set()
    used_secrets: set[str] = set()
    certificates: set[str] = set()
    permissions: dict[tuple[str, str], set[str]] = {pair: set() for pair in pairs}
    for raw in sequence(data["credentials"]):
        item = mapping(raw)
        credential_id = string(item["id"])
        if credential_id in credential_ids:
            raise ConfigurationError(f"duplicate credential {credential_id!r}")
        credential_ids.add(credential_id)
        secret_ref = string(item["secret_ref"])
        _require_secret(secret_ref, secrets, credential_id)
        if secret_ref in used_secrets:
            raise ConfigurationError(f"{credential_id}: credentials must have distinct secret references")
        used_secrets.add(secret_ref)
        pair = (string(item["tenant"]), string(item["datastream"]))
        if pair not in pairs:
            raise ConfigurationError(f"{credential_id}: unknown tenant/datastream {pair}")
        permission = string(item["permission"])
        permissions[pair].add(permission)
        cert = item.get("certificate_identity")
        if cert is not None:
            cert = string(cert)
            prefix = f"spiffe://nighthawk/{pair[0]}/{pair[1]}/"
            if permission != "ingest" or not cert.startswith(prefix):
                raise ConfigurationError(f"{credential_id}: certificate must identify its ingestion tenant/datastream")
            if cert in certificates:
                raise ConfigurationError(f"{credential_id}: duplicate certificate identity")
            certificates.add(cert)
        credentials.append(Credential(
            credential_id, secret_ref, pair[0], pair[1], pairs[pair], permission, cert,
        ))
    for pair, allowed in permissions.items():
        if allowed != {"ingest", "query"}:
            raise ConfigurationError(f"{pair}: separate ingestion and query credentials are required")
    deployment = string(data["deployment"])
    provider, bindings = _storage(mapping(data["storage"]), deployment, secrets, required_buckets)
    storage_refs = {
        binding.identity.ref for binding in bindings.values() if binding.identity.type == "secret"
    }
    if used_secrets & storage_refs:
        raise ConfigurationError("gateway credentials must not reuse object-storage secrets")
    gateway = _gateway(
        mapping(data["gateway"]),
        {name for stream in streams for name in stream.signals},
        load_network(network if network is not None else ROOT / "config" / "network.yaml"),
    )
    trust_refs = {binding.tls.ca_secret_ref for binding in bindings.values() if binding.tls.ca_secret_ref}
    if (used_secrets | storage_refs) & trust_refs:
        raise ConfigurationError("storage CA references must not reuse credential or object-storage secrets")
    raw_grafana = mapping(data["grafana"])
    grafana = Grafana(string(raw_grafana["hostname"]), string(raw_grafana["admin_secret_ref"]))
    if grafana.hostname == gateway.hostname:
        raise ConfigurationError("grafana: hostname must differ from the gateway hostname")
    _require_secret(grafana.admin_secret_ref, secrets, "grafana")
    if grafana.admin_secret_ref in used_secrets | storage_refs | trust_refs:
        raise ConfigurationError("grafana: admin_secret_ref must not reuse another secret")
    join_token_ref = None
    if "cluster" in data:
        if deployment != "self-hosted-k8s":
            raise ConfigurationError(f"cluster: only a self-hosted-k8s deployment has a cluster join token, not {deployment}")
        join_token_ref = string(mapping(data["cluster"])["join_token_secret_ref"])
        _require_secret(join_token_ref, secrets, "cluster")
        if join_token_ref in used_secrets | storage_refs | trust_refs | {grafana.admin_secret_ref}:
            raise ConfigurationError("cluster: join_token_secret_ref must not reuse another secret")
    profile = string(data["profile"])
    return Platform(
        2, deployment, profile, _vault(mapping(data["vault"]), profile, deployment), gateway, grafana, provider, bindings, secrets,
        tuple(sorted(streams, key=lambda stream: (stream.tenant, stream.datastream))),
        tuple(sorted(credentials, key=lambda credential: credential.id)),
        join_token_ref,
    )


def validate_migration(platform: Platform, previous: Platform) -> None:
    old_pairs = {(stream.tenant, stream.datastream): stream.backend_id for stream in previous.streams}
    old_ids = {stream.backend_id: (stream.tenant, stream.datastream) for stream in previous.streams}
    for stream in platform.streams:
        pair = (stream.tenant, stream.datastream)
        if pair in old_pairs and old_pairs[pair] != stream.backend_id:
            raise ConfigurationError(f"{pair}: backend ID changes require a data migration")
        if stream.backend_id in old_ids and old_ids[stream.backend_id] != pair:
            raise ConfigurationError(f"{stream.backend_id}: backend ID cannot be reassigned to another tenant/datastream")


def release(version: str) -> tuple[int, ...]:
    """Parse a dotted release such as `2.1.2`, ignoring any build or edition suffix."""
    match = re.match(r"v?([0-9]+(?:\.[0-9]+)*)", version)
    if match is None:
        raise ConfigurationError(f"{version!r} is not a release version")
    return tuple(int(part) for part in match.group(1).split("."))


def vault_supports(matrix: dict, version: str) -> bool:
    """Whether a Vault server version lies in the matrix's supported range."""
    supported = mapping(mapping(mapping(matrix["secrets_store"])["vault"])["supported"])
    return release(string(supported["minimum"])) <= release(version) < release(string(supported["below"]))


# Helm charts that package a backend the matrix also pins.
CHART_BACKEND = {"loki": "loki", "tempo_distributed": "tempo", "mimir_distributed": "mimir", "pyroscope": "pyroscope"}


def chart_version_problems(matrix: dict) -> list[str]:
    """Charts whose application version disagrees with the backend pin without a recorded reason, or agree despite one."""
    problems: list[str] = []
    charts, backends = mapping(matrix["grafana_charts"]), mapping(matrix["backends"])
    for chart, backend in sorted(CHART_BACKEND.items()):
        entry = mapping(charts[chart])
        packaged, pinned = string(entry["app_version"]), string(mapping(backends[backend])["version"])
        excepted = "app_version_exception" in entry
        if packaged != pinned and not excepted:
            problems.append(
                f"grafana_charts.{chart} packages {backend} {packaged} but backends.{backend} pins {pinned}; "
                "pin a chart release that matches, or record an app_version_exception with a reason"
            )
        elif packaged == pinned and excepted:
            problems.append(
                f"grafana_charts.{chart} records an app_version_exception, but it packages the pinned "
                f"{backend} {pinned}; remove the stale exception"
            )
    return problems


def helm_chart_problems(matrix: dict) -> list[str]:
    """Locked charts whose version, or the application they package, disagrees with the pin they name."""
    problems: list[str] = []
    for name, raw in sorted(mapping(matrix["helm_charts"]).items()):
        entry = mapping(raw)
        for field, reference in (("version", "version_of"), ("app_version", "app_version_of")):
            if reference not in entry:
                continue
            try:
                pinned = str(resolve_pin(matrix, string(entry[reference])))
            except (KeyError, ConfigurationError):
                problems.append(f"helm_charts.{name}.{reference} names {entry[reference]}, which the matrix does not have")
                continue
            if str(entry[field]) != pinned:
                problems.append(
                    f"helm_charts.{name}.{field} is {entry[field]} but {entry[reference]} pins {pinned}; "
                    "lock the chart release that matches and record its digest"
                )
    return problems


def supported_architectures(matrix: dict) -> list[str]:
    """Every architecture some supported operating system entry lists."""
    return sorted({
        string(architecture)
        for entries in mapping(matrix["os_support"]).values() for entry in sequence(entries)
        for architecture in sequence(mapping(entry)["architectures"])
    })


def artifact_checksum_problems(matrix: dict) -> list[str]:
    """Host-installed binaries that lack a checksum for an architecture the matrix supports."""
    problems: list[str] = []
    for name, artifact in sorted(mapping(matrix["host_artifacts"]).items()):
        checksums = mapping(artifact).get("sha256")
        if checksums is None:
            continue
        for architecture in supported_architectures(matrix):
            if f"linux_{architecture}" not in mapping(checksums):
                problems.append(f"host_artifacts.{name} has no checksum for linux_{architecture}, a supported architecture")
    return problems


def load_versions(path: Path = ROOT / "config" / "versions.yaml") -> dict:
    matrix = load_document(path, "versions.schema.json")
    problems = chart_version_problems(matrix) + artifact_checksum_problems(matrix) + helm_chart_problems(matrix)
    if problems:
        raise ConfigurationError(f"{path}:\n" + "\n".join(problems))
    vault = mapping(mapping(matrix["secrets_store"])["vault"])
    supported = mapping(vault["supported"])
    tested = string(vault["version"])
    if not vault_supports(matrix, tested):
        raise ConfigurationError(
            f"{path}: the tested Vault version {tested} is outside the supported range "
            f"{supported['minimum']} (inclusive) to {supported['below']} (exclusive)"
        )
    if string(mapping(vault["image"])["tag"]) != tested:
        raise ConfigurationError(f"{path}: the Vault test image tag must equal the tested version {tested}")
    return matrix


def is_os_supported(matrix: dict, target: str, distribution: str, version: str, architecture: str) -> bool:
    """Check distribution/version/architecture against one of os_support's declared target sets.

    `target` must be "docker_hosts" or "k3s_nodes"; an unrecognized target or any
    undeclared distribution/version/architecture combination is reported unsupported.
    """
    os_support = mapping(matrix.get("os_support", {}))
    for entry in sequence(os_support.get(target, [])):
        item = mapping(entry)
        if (
            string(item["distribution"]) == distribution
            and version in sequence(item["versions"])
            and architecture in sequence(item["architectures"])
        ):
            return True
    return False


def resolve_pin(matrix: dict, dotted_path: str) -> object:
    """Resolve a dotted component path (e.g. 'validation_tools.terraform.version') from the matrix."""
    node: object = matrix
    for part in dotted_path.split("."):
        node = mapping(node)[part]
    return node


def _pin_components(document: dict, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], dict]:
    components: dict[tuple[str, ...], dict] = {}
    for key, value in document.items():
        if isinstance(value, dict):
            if "runtime_verified" in value:
                components[path + (key,)] = value
            else:
                components.update(_pin_components(value, path + (key,)))
    return components


def validate_pin_changes(matrix: dict, previous: dict) -> None:
    """Reject a version/mode change on a runtime_verified component unless the same update resets it to false."""
    current = _pin_components(matrix)
    old = _pin_components(previous)
    for key, pin in current.items():
        old_pin = old.get(key)
        if old_pin is None:
            continue
        changed_fields = sorted(
            field for field in pin
            if field != "runtime_verified" and pin.get(field) != old_pin.get(field)
        )
        if changed_fields and old_pin.get("runtime_verified") is True and pin.get("runtime_verified") is not False:
            name = ".".join(key)
            raise ConfigurationError(
                f"{name}: changing {', '.join(changed_fields)} requires resetting runtime_verified to false"
            )


def check_pins(matrix: dict, root: Path = ROOT) -> list[str]:
    """Compare each tracked_consumers entry against its matrix pin; return human-readable mismatches."""
    mismatches: list[str] = []
    for raw in sequence(matrix.get("tracked_consumers", [])):
        entry = mapping(raw)
        rel_path = string(entry["path"])
        pattern = string(entry["pattern"])
        component = string(entry["component"])
        expected = str(resolve_pin(matrix, component))
        file_path = root / rel_path
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigurationError(f"{rel_path}: cannot read tracked consumer file: {error}") from error
        match = re.search(pattern, content)
        if match is None:
            raise ConfigurationError(f"{rel_path}: pattern for {component} matched no content")
        actual = match.group(1)
        if actual != expected:
            mismatches.append(f"{rel_path}: {component} pin is {expected!r} but file declares {actual!r}")
    return mismatches


def load_network(path: Path) -> list[dict]:
    data = load_document(path, "network.schema.json")
    rules = [mapping(rule) for rule in sequence(data["rules"])]
    ids: set[str] = set()
    flows: set[tuple] = set()
    for rule in rules:
        name = string(rule["id"])
        if name in ids:
            raise ConfigurationError(f"duplicate network rule {name!r}")
        ids.add(name)
        flow = tuple(rule[key] for key in ("source", "destination", "protocol", "port", "scope"))
        if flow in flows:
            raise ConfigurationError(f"{name}: duplicate network flow")
        flows.add(flow)
    return sorted(rules, key=lambda rule: rule["id"])
