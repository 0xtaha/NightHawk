"""Source-verified Pyroscope v2 override rendering; runtime acceptance is separate."""

from pathlib import Path

from nighthawk.config import ConfigurationError, Platform, ROOT, load_yaml, mapping


def pyroscope_overrides(
    platform: Platform, versions_path: Path = ROOT / "config" / "versions.yaml"
) -> dict[str, dict[str, dict[str, str]]]:
    versions = mapping(load_yaml(versions_path))
    backends = mapping(versions.get("backends"))
    backend = mapping(backends.get("pyroscope"))
    expected = {
        "version": "2.3.1",
        "storage": "v2",
        "retention_override": "retention_period",
    }
    if versions.get("schema_version") != 1 or backend != expected:
        raise ConfigurationError(
            "Pyroscope overrides require the source-verified 2.3.1 v2 retention_period contract; "
            "validate a new version/storage mode before changing the pin"
        )
    return {
        "overrides": {
            stream.backend_id: {"retention_period": f"{stream.signals['profiles'].retention_hours}h"}
            for stream in platform.streams if "profiles" in stream.signals
        }
    }
