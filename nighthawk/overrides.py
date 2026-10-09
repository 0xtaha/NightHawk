"""Per-tenant runtime overrides for all four backends, gated on the reviewed pins.

Each key was read from the backend's source at the pinned tag; see
docs/06-tenant-provisioning.md. A contract value no backend key enforces is
reported as unenforced instead of being dropped or approximated.
"""

from __future__ import annotations

from pathlib import Path

from nighthawk.config import ConfigurationError, Platform, ROOT, SignalPolicy, load_yaml, mapping
from nighthawk.retention import pyroscope_overrides

BYTES_PER_MB = 1_048_576
# Versions whose override keys were verified. Another pin fails until the mapping is reviewed.
REVIEWED = {"mimir": "3.2.1", "loki": "3.7.8", "tempo": "3.0.3"}
SIGNAL_BACKEND = {"metrics": "mimir", "logs": "loki", "traces": "tempo", "profiles": "pyroscope"}
UNENFORCED_QUERY_CONCURRENCY = {
    "mimir": "no per-tenant cap on concurrent queries; max_query_parallelism bounds sub-queries of one query "
             "and max_queriers_per_tenant bounds assigned queriers",
    "loki": "no per-tenant cap on concurrent queries; max_query_parallelism bounds sub-queries of one query "
            "and max_queriers_per_tenant bounds assigned queriers",
    "tempo": "no per-tenant query concurrency override exists",
    "pyroscope": "max_query_parallelism is not used by the v2 read path",
}


def _megabytes(bytes_per_second: int) -> float:
    return round(bytes_per_second / BYTES_PER_MB, 6)


def _mimir(policy: SignalPolicy) -> dict:
    return {
        "compactor_blocks_retention_period": f"{policy.retention_hours}h",
        "ingestion_rate": policy.ingestion_rate,
    }


def _loki(policy: SignalPolicy) -> dict:
    return {
        "retention_period": f"{policy.retention_hours}h",
        "ingestion_rate_mb": _megabytes(policy.ingestion_rate),
    }


def _tempo(policy: SignalPolicy) -> dict:
    # Tempo does not merge a tenant's entry with its defaults: an unset burst is 0 and every
    # write is refused. The burst is one second of the declared rate.
    return {
        "compaction": {"block_retention": f"{policy.retention_hours}h"},
        "ingestion": {"rate_limit_bytes": policy.ingestion_rate, "burst_size_bytes": policy.ingestion_rate},
    }


_RENDERERS = {"metrics": _mimir, "logs": _loki, "traces": _tempo}


def render_overrides(
    platform: Platform, versions_path: Path = ROOT / "config" / "versions.yaml"
) -> tuple[dict[str, dict], list[dict]]:
    """Return ({backend: runtime override document}, unenforced limits)."""
    backends = mapping(mapping(load_yaml(versions_path)).get("backends"))
    for backend, reviewed in REVIEWED.items():
        pinned = mapping(backends.get(backend)).get("version")
        if pinned != reviewed:
            raise ConfigurationError(
                f"{backend} overrides were reviewed for {reviewed}, not {pinned}; "
                "review the override keys before changing the pin"
            )
    # Applies the same gate to Pyroscope (version, storage mode, retention field).
    documents: dict[str, dict] = {"pyroscope": pyroscope_overrides(platform, versions_path)}
    for backend in REVIEWED:
        documents[backend] = {"overrides": {}}
    unenforced: list[dict] = []
    for stream in platform.streams:
        for signal, policy in sorted(stream.signals.items()):
            backend = SIGNAL_BACKEND[signal]
            if signal == "profiles":
                documents[backend]["overrides"][stream.backend_id]["ingestion_rate_mb"] = _megabytes(policy.ingestion_rate)
            else:
                documents[backend]["overrides"][stream.backend_id] = _RENDERERS[signal](policy)
            unenforced.append({
                "backend": backend,
                "backend_id": stream.backend_id,
                "field": "query_concurrency",
                "value": policy.query_concurrency,
                "reason": UNENFORCED_QUERY_CONCURRENCY[backend],
            })
    return documents, sorted(unenforced, key=lambda item: (item["backend"], item["backend_id"]))
