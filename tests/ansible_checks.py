"""Repository checks for the Ansible tree: no contract value hard-coded, no secret in an inventory."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

SCANNED_SUFFIXES = {".yml", ".yaml", ".j2", ".cfg", ".conf", ".service", ".sh"}
SECRET_NAME = re.compile(r"(password|passwd|secret|token|private_key|credential|api_key)", re.IGNORECASE)
# A variable that names where a secret is, not the secret itself.
LOCATION_SUFFIX = re.compile(r"(_file|_path|_dir|_ref|_name|_id|_days|_minutes|_seconds)$")


def contract_values(inputs: dict) -> dict[str, str]:
    """Every port, version, checksum, and fingerprint the rendered inputs provide, with what it is."""
    data, values = inputs["nighthawk"], {}
    for role, rules in data["firewall"].items():
        for rule in rules:
            values[str(rule["port"])] = f"port of network rule {rule['id']}"
    for name in ("loopback_port", "external_port"):
        if data["gateway"][name] is not None:
            values[str(data["gateway"][name])] = f"gateway {name}"
    for artifact, pin in data["pins"].items():
        for field in ("version", "compose_plugin_version"):
            if field in pin:
                values[pin[field]] = f"{artifact} {field}"
        for kind in ("sha256", "signing_key_fingerprints"):
            for key, value in pin.get(kind, {}).items():
                values[value] = f"{artifact} {kind} {key}"
    return values


def literal_contract_values(root: Path, inputs: dict) -> list[str]:
    """Files under `root` that spell out a value the rendered inputs provide."""
    problems: list[str] = []
    values = contract_values(inputs)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES or "molecule" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for value, meaning in sorted(values.items()):
            # A whole token: not part of a longer number, version, or word.
            if re.search(rf"(?<![\w.]){re.escape(value)}(?![\w.])", text):
                problems.append(f"{path.relative_to(root)}: literal {value} ({meaning}); take it from the rendered inputs")
    return problems


def _walk(value: object, trail: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, trail + (str(key),))
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, trail)
    else:
        yield trail, value


def inventory_secrets(root: Path) -> list[str]:
    """Secret-named variables given a literal value in an inventory or variables file under `root`."""
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in (".yml", ".yaml"):
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for trail, value in _walk(document):
            name = trail[-1] if trail else ""
            if SECRET_NAME.search(name) and not LOCATION_SUFFIX.search(name) and isinstance(value, str) and value.strip():
                problems.append(f"{path.relative_to(root)}: {'.'.join(trail)} holds a literal value")
    return problems
