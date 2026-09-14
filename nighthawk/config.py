"""Strict, non-secret configuration loading and cross-resource validation."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import asdict, dataclass
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
    ingestion_rate_bytes_per_second: int
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
    file: str
    key: str


@dataclass(frozen=True)
class TLS:
    enabled: bool
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
class Platform:
    schema_version: int
    deployment: str
    profile: str
    storage_provider: str
    bindings: dict[str, StorageBinding]
    secrets: dict[str, SecretReference]
    streams: tuple[Stream, ...]
    credentials: tuple[Credential, ...]

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
        if ca is not None:
            _require_secret(string(ca), secrets, name)
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
            TLS(True, ca), Identity(identity_type, ref),
            Capabilities(boolean(capabilities["versioning"]), boolean(capabilities["lifecycle"]), workload_identity),
        )
    return provider, bindings


def load_platform(path: Path, storage_output: Path | None = None) -> Platform:
    document = mapping(load_yaml(path))
    if storage_output is not None:
        storage = mapping(load_yaml(storage_output))
        if set(storage) != {"provider", "bindings"}:
            raise ConfigurationError(
                f"{storage_output}: expected the direct storage value; use 'terraform output -json storage'"
            )
        document["storage"] = storage
    data = _validate_document(document, path, "platform.schema.json")
    secrets = {
        name: SecretReference(string(mapping(item)["file"]), string(mapping(item)["key"]))
        for name, item in sorted(mapping(data["secrets"]).items())
    }
    locations = [(secret.file, secret.key) for secret in secrets.values()]
    if len(set(locations)) != len(locations):
        raise ConfigurationError("secret references must not alias the same encrypted file/key")
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
                signals[name] = SignalPolicy(
                    hours, integer(signal["ingestion_rate_bytes_per_second"]),
                    integer(signal["query_concurrency"]),
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
    return Platform(
        1, deployment, string(data["profile"]), provider, bindings, secrets,
        tuple(sorted(streams, key=lambda stream: (stream.tenant, stream.datastream))),
        tuple(sorted(credentials, key=lambda credential: credential.id)),
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
