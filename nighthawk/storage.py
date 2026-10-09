"""Local SeaweedFS provisioning inputs: per-backend S3 identities and the bucket list."""

from __future__ import annotations

import json
from typing import Callable

from nighthawk.config import BUCKET_OWNER, ConfigurationError, Platform

SIGNAL_BACKEND = {"metrics": "mimir", "logs": "loki", "traces": "tempo", "profiles": "pyroscope"}
# What a signal backend needs on its own buckets. SeaweedFS's Write includes delete.
ACTIONS = ("Read", "Write", "List")


def parse_identity(text: str, reference: str) -> tuple[str, str]:
    """Decode a storage identity secret without ever echoing its content."""
    try:
        value = json.loads(text)
        access_key, secret_key = value["access_key"], value["secret_key"]
        if set(value) != {"access_key", "secret_key"} or not all(
            isinstance(item, str) and item for item in (access_key, secret_key)
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ConfigurationError(
            f"{reference}: storage identity must be JSON with access_key and secret_key; "
            "create it with generate-storage-identity"
        ) from None
    return access_key, secret_key


def _local_identities(platform: Platform) -> dict[str, list[str]]:
    if platform.storage_provider != "seaweedfs":
        raise ConfigurationError("local object storage is provisioned only for SeaweedFS bindings")
    identities: dict[str, list[str]] = {}
    for binding in platform.bindings.values():
        identities.setdefault(binding.identity.ref, []).append(binding.bucket)
    return identities


def buckets(platform: Platform) -> list[str]:
    return sorted(bucket for owned in _local_identities(platform).values() for bucket in owned)


def s3_identities(platform: Platform, read_secret: Callable[[str], str]) -> dict:
    """SeaweedFS S3 configuration: one identity per backend, limited to its buckets, nothing anonymous."""
    identities = []
    for reference, owned in sorted(_local_identities(platform).items()):
        access_key, secret_key = parse_identity(read_secret(reference), reference)
        identities.append({
            "name": reference,
            "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
            "actions": [f"{action}:{bucket}" for bucket in sorted(owned) for action in ACTIONS],
        })
    return {"identities": identities}


def backend_environment(platform: Platform, read_secret: Callable[[str], str]) -> dict[str, str]:
    """Per-backend env file content carrying that backend's S3 keys."""
    environment: dict[str, str] = {}
    for name, binding in sorted(platform.bindings.items()):
        backend = SIGNAL_BACKEND[BUCKET_OWNER[name]]
        if binding.identity.type != "secret" or backend in environment:
            continue
        access_key, secret_key = parse_identity(read_secret(binding.identity.ref), binding.identity.ref)
        environment[backend] = f"NIGHTHAWK_S3_ACCESS_KEY={access_key}\nNIGHTHAWK_S3_SECRET_KEY={secret_key}\n"
    return environment
