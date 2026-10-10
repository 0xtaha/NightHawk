"""Single-process backend configuration for the Docker profile, rendered from the storage bindings.

Every setting was checked by starting the pinned image with it; see
docs/07-docker-compose.md. Storage credentials are never rendered: each backend
expands them from its environment (`-config.expand-env=true`).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import yaml

from nighthawk.config import ConfigurationError, Platform, ROOT, StorageBinding, load_yaml, mapping

# Mount points the Compose file provides.
OVERRIDES_DIR = "/etc/nighthawk/overrides"
DATA_DIR = "/data"
TEMPO_DATA_DIR = "/var/tempo"
ACCESS_KEY = "${NIGHTHAWK_S3_ACCESS_KEY}"
SECRET_KEY = "${NIGHTHAWK_S3_SECRET_KEY}"
RELOAD_PERIOD = "10s"
# Version and Compose architecture each renderer was verified against.
REVIEWED = {
    "mimir": {"version": "3.2.1", "compose_architecture": "classic"},
    "loki": {"version": "3.7.8"},
    "tempo": {"version": "3.0.3", "compose_architecture": "monolithic"},
    "pyroscope": {"version": "2.3.1", "storage": "v2"},
}
SIGNAL_BACKEND = {"metrics": "mimir", "logs": "loki", "traces": "tempo", "profiles": "pyroscope"}


def _endpoint(binding: StorageBinding) -> str:
    return urlsplit(binding.endpoint).netloc


def _port(address: str) -> int:
    port = urlsplit(address).port
    if port is None:
        raise ConfigurationError(f"{address}: an explicit port is required")
    return port


def _mimir(platform: Platform) -> dict:
    blocks = platform.bindings["mimir-blocks"]
    return {
        "target": "all",
        "multitenancy_enabled": True,
        "usage_stats": {"enabled": False},
        "server": {"http_listen_port": _port(platform.gateway.upstreams["metrics"]), "grpc_listen_port": 9095},
        "common": {"storage": {"backend": "s3", "s3": {
            "endpoint": _endpoint(blocks),
            "region": blocks.region,
            "access_key_id": ACCESS_KEY,
            "secret_access_key": SECRET_KEY,
            "bucket_lookup_type": "path" if blocks.force_path_style else "virtual-hosted",
        }}},
        "blocks_storage": {
            "s3": {"bucket_name": blocks.bucket},
            "tsdb": {"dir": f"{DATA_DIR}/tsdb"},
            "bucket_store": {"sync_dir": f"{DATA_DIR}/tsdb-sync"},
        },
        "ruler_storage": {"s3": {"bucket_name": platform.bindings["mimir-ruler"].bucket}},
        "alertmanager_storage": {"s3": {"bucket_name": platform.bindings["mimir-alertmanager"].bucket}},
        # The compactor applies compactor_blocks_retention_period from the overrides.
        "compactor": {"data_dir": f"{DATA_DIR}/compactor"},
        "ruler": {"rule_path": f"{DATA_DIR}/ruler"},
        "alertmanager": {"data_dir": f"{DATA_DIR}/alertmanager"},
        "ingester": {"ring": {"replication_factor": 1}},
        "store_gateway": {"sharding_ring": {"replication_factor": 1}},
        # The default is a relative path, which a non-root user cannot write.
        "activity_tracker": {"filepath": f"{DATA_DIR}/metrics-activity.log"},
        "tenant_federation": {"enabled": False},
        "runtime_config": {"file": f"{OVERRIDES_DIR}/mimir-overrides.yaml", "period": RELOAD_PERIOD},
    }


def _loki(platform: Platform) -> dict:
    chunks = platform.bindings["loki-chunks"]
    return {
        "auth_enabled": True,
        "analytics": {"reporting_enabled": False},
        "server": {"http_listen_port": _port(platform.gateway.upstreams["logs"]), "grpc_listen_port": 9095},
        "common": {
            "path_prefix": DATA_DIR,
            "replication_factor": 1,
            "ring": {"kvstore": {"store": "inmemory"}},
            "storage": {"s3": {
                "endpoint": _endpoint(chunks),
                "region": chunks.region,
                "bucketnames": chunks.bucket,
                "access_key_id": ACCESS_KEY,
                "secret_access_key": SECRET_KEY,
                "s3forcepathstyle": chunks.force_path_style,
                "insecure": False,
            }},
        },
        "schema_config": {"configs": [{
            "from": "2024-01-01", "store": "tsdb", "object_store": "s3", "schema": "v13",
            "index": {"prefix": "index_", "period": "24h"},
        }]},
        # Without retention_enabled the per-tenant retention_period deletes nothing.
        "compactor": {
            "working_directory": f"{DATA_DIR}/compactor", "retention_enabled": True, "delete_request_store": "s3",
        },
        "querier": {"multi_tenant_queries_enabled": False},
        "runtime_config": {"file": f"{OVERRIDES_DIR}/loki-overrides.yaml", "period": RELOAD_PERIOD},
    }


def _tempo(platform: Platform) -> dict:
    traces = platform.bindings["tempo-traces"]
    upstreams = platform.gateway.upstreams
    return {
        # Off by default in Tempo.
        "multitenancy_enabled": True,
        "usage_report": {"reporting_enabled": False},
        "server": {"http_listen_port": _port(upstreams["traces.query"]), "grpc_listen_port": 9095},
        # The receivers bind to localhost unless told otherwise.
        "distributor": {"receivers": {"otlp": {"protocols": {
            "grpc": {"endpoint": f"0.0.0.0:{_port(upstreams['traces.otlp_grpc'])}"},
            "http": {"endpoint": f"0.0.0.0:{_port(upstreams['traces.otlp_http'])}"},
        }}}},
        # On by default in Tempo.
        "query_frontend": {"multi_tenant_queries_enabled": False},
        "storage": {"trace": {
            "backend": "s3",
            "s3": {
                "endpoint": _endpoint(traces),
                "bucket": traces.bucket,
                "region": traces.region,
                "access_key": ACCESS_KEY,
                "secret_key": SECRET_KEY,
                "forcepathstyle": traces.force_path_style,
            },
            "wal": {"path": f"{TEMPO_DATA_DIR}/wal"},
        }},
        "overrides": {
            "per_tenant_override_config": f"{OVERRIDES_DIR}/tempo-overrides.yaml",
            "per_tenant_override_period": RELOAD_PERIOD,
        },
    }


def _pyroscope(platform: Platform) -> dict:
    profiles = platform.bindings["pyroscope-profiles"]
    return {
        "target": "all",
        "multitenancy_enabled": True,
        "analytics": {"reporting_enabled": False},
        "server": {"http_listen_port": _port(platform.gateway.upstreams["profiles"]), "grpc_listen_port": 9095},
        "storage": {"backend": "s3", "s3": {
            "endpoint": _endpoint(profiles),
            "bucket_name": profiles.bucket,
            "region": profiles.region,
            "access_key_id": ACCESS_KEY,
            "secret_access_key": SECRET_KEY,
            "force_path_style": profiles.force_path_style,
        }},
        "runtime_config": {"file": f"{OVERRIDES_DIR}/pyroscope-overrides.yaml", "period": RELOAD_PERIOD},
    }


_RENDERERS = {"metrics": _mimir, "logs": _loki, "traces": _tempo, "profiles": _pyroscope}


def render_backends(
    platform: Platform, versions_path: Path = ROOT / "config" / "versions.yaml"
) -> dict[str, str] | None:
    """Return {backend: configuration YAML}, or None where none is rendered.

    Docker and the development profile of self-hosted Kubernetes run every backend as one
    process with the same reviewed configuration. The distributed production configuration is
    produced with the Kubernetes values, not here.
    """
    monolithic = platform.deployment == "docker" or (
        platform.deployment == "self-hosted-k8s" and platform.profile == "development"
    )
    if not monolithic:
        return None
    pinned = mapping(mapping(load_yaml(versions_path)).get("backends"))
    enabled = {signal for stream in platform.streams for signal in stream.signals}
    documents: dict[str, str] = {}
    for signal in sorted(enabled):
        backend = SIGNAL_BACKEND[signal]
        actual = mapping(pinned.get(backend))
        for key, value in REVIEWED[backend].items():
            if actual.get(key) != value:
                raise ConfigurationError(
                    f"{backend} configuration was reviewed for {key} {value}, not {actual.get(key)}; "
                    "review the settings before changing the pin"
                )
        documents[backend] = yaml.safe_dump(_RENDERERS[signal](platform), sort_keys=True)
    return documents
