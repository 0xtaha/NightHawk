"""SOPS + age secret lifecycle: prerequisite checks first, then generation/encryption/rotation."""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from nighthawk.config import ROOT, ConfigurationError, Platform, load_platform, mapping, sequence, string

Runner = Callable[..., subprocess.CompletedProcess]

LOCAL_RECIPIENTS_REGISTRY = ROOT / ".generated" / "age-recipients.local.json"
MATERIALIZED_SECRETS_DIR = ROOT / ".materialized-secrets"
SECRETS_DIR = ROOT / "secrets"


@dataclass(frozen=True)
class ToolCheck:
    tool: str
    expected_version: str
    found_version: str | None
    ok: bool
    detail: str


def _check_tool(tool: str, expected_version: str, runner: Runner) -> ToolCheck:
    try:
        result = runner([tool, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return ToolCheck(tool, expected_version, None, False, f"{tool} is not available: {error}")
    if result.returncode != 0:
        return ToolCheck(
            tool, expected_version, None, False,
            f"{tool} --version exited with code {result.returncode}",
        )
    output = (result.stdout or "") + (result.stderr or "")
    match = re.search(r"v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", output)
    if match is None:
        return ToolCheck(tool, expected_version, None, False, f"could not parse {tool} version from {output.strip()!r}")
    found_version = match.group(1)
    expected = expected_version.lstrip("v")
    if found_version != expected:
        return ToolCheck(
            tool, expected_version, found_version, False,
            f"{tool} {found_version} does not match the pinned version {expected_version}",
        )
    return ToolCheck(tool, expected_version, found_version, True, f"{tool} {found_version} matches the pinned version")


def doctor(matrix: dict, runner: Runner = subprocess.run) -> list[ToolCheck]:
    """Resolve sops/age prerequisite status against the compatibility matrix's pinned versions."""
    secrets_tools = mapping(matrix["secrets_tools"])
    checks: list[ToolCheck] = []
    for tool in ("sops", "age"):
        expected_version = string(mapping(secrets_tools[tool])["version"])
        checks.append(_check_tool(tool, expected_version, runner))
    return checks


def ensure_doctor_ok(matrix: dict, runner: Runner = subprocess.run) -> None:
    """Raise ConfigurationError before any secrets-workflow command touches a file if prerequisites fail."""
    failures = [check for check in doctor(matrix, runner) if not check.ok]
    if failures:
        details = "; ".join(check.detail for check in failures)
        raise ConfigurationError(f"secrets-workflow prerequisite check failed: {details}")


def _load_registry(path: Path | None = None) -> set[str]:
    path = path if path is not None else LOCAL_RECIPIENTS_REGISTRY
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ConfigurationError(f"{path}: cannot read local recipients registry: {error}") from error
    return set(sequence(data))


def _record_registry(recipient: str, path: Path | None = None) -> None:
    path = path if path is not None else LOCAL_RECIPIENTS_REGISTRY
    recipients = _load_registry(path)
    recipients.add(recipient)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(recipients), indent=2) + "\n", encoding="utf-8")


def generate_recipient(
    path: Path, overwrite: bool = False, runner: Runner = subprocess.run, registry_path: Path | None = None,
) -> str:
    """Generate a fresh age key pair at `path`; return the public recipient string."""
    if path.exists() and not overwrite:
        raise ConfigurationError(f"{path}: refusing to overwrite an existing age key without an explicit overwrite flag")
    result = runner(["age-keygen", "-o", str(path)], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise ConfigurationError(f"age-keygen failed: {(result.stderr or result.stdout or '').strip()}")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    output = (result.stderr or "") + (result.stdout or "") + content
    match = re.search(r"(age1[0-9a-z]+)", output)
    if match is None:
        raise ConfigurationError("age-keygen did not report a public recipient")
    recipient = match.group(1)
    _record_registry(recipient, registry_path)
    return recipient


def _extract_sops_recipients(path: Path) -> set[str]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"{path}: cannot read SOPS metadata: {error}") from error
    sops_metadata = mapping(mapping(document).get("sops", {}))
    return {string(mapping(entry)["recipient"]) for entry in sequence(sops_metadata.get("age", []))}


def guard_production_recipients(
    profile: str, recipients: set[str], confirmed: bool, registry_path: Path | None = None,
) -> None:
    """Reject locally auto-generated age recipients for `profile: production` unless explicitly confirmed."""
    if profile != "production":
        return
    if not recipients:
        raise ConfigurationError("profile: production requires an explicit list of age recipients")
    auto_generated = _load_registry(registry_path)
    if not confirmed and recipients & auto_generated:
        raise ConfigurationError(
            "profile: production rejects locally auto-generated age recipients without explicit confirmation"
        )


def encrypt_secret(
    file_name: str, key: str, value: str, recipients: list[str],
    secrets_dir: Path = SECRETS_DIR, profile: str = "development",
    confirm_production_recipients: bool = False, runner: Runner = subprocess.run,
) -> Path:
    """Encrypt `value` under `key` into a new SOPS file `secrets_dir/file_name`."""
    if not recipients:
        raise ConfigurationError("encryption requires at least one age recipient")
    guard_production_recipients(profile, set(recipients), confirm_production_recipients)
    secrets_dir.mkdir(parents=True, exist_ok=True)
    target = secrets_dir / file_name
    plaintext = yaml.safe_dump({key: value})
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, dir=secrets_dir)
    try:
        handle.write(plaintext)
        handle.close()
        temp_path = Path(handle.name)
        result = runner(
            ["sops", "--encrypt", "--age", ",".join(recipients), "--output", str(target), str(temp_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise ConfigurationError(f"sops encryption failed: {(result.stderr or result.stdout or '').strip()}")
    finally:
        Path(handle.name).unlink(missing_ok=True)
    return target


def rotate_secret(
    file_name: str, key: str, new_value: str, secrets_dir: Path = SECRETS_DIR, runner: Runner = subprocess.run,
) -> Path:
    """Re-encrypt `file_name`'s `key` with `new_value`, keeping the same recipients and reference."""
    target = secrets_dir / file_name
    if not target.exists():
        raise ConfigurationError(f"{target}: cannot rotate a secret that does not exist")
    result = runner(
        ["sops", "--set", f'["{key}"] {json.dumps(new_value)}', str(target)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise ConfigurationError(f"sops rotation failed: {(result.stderr or result.stdout or '').strip()}")
    return target


def materialize(
    platform_path: Path, root: Path = ROOT, output_dir: Path = MATERIALIZED_SECRETS_DIR,
    confirm_production_recipients: bool = False, runner: Runner = subprocess.run,
) -> Platform:
    """Decrypt every secret referenced by a validated platform document into `output_dir`.

    `reference.file` is stored relative to the repository root (e.g. `secrets/local.sops.yaml`),
    so sources are resolved against `root`, not a `secrets/`-only directory.
    """
    platform = load_platform(platform_path)
    if output_dir.exists():
        mode = stat.S_IMODE(output_dir.stat().st_mode)
        if mode != 0o700:
            raise ConfigurationError(
                f"{output_dir}: refusing to materialize into an existing directory with unexpected permissions {oct(mode)}"
            )
    else:
        output_dir.mkdir(mode=0o700)
    for reference in platform.secrets.values():
        source = root / reference.file
        recipients = _extract_sops_recipients(source)
        guard_production_recipients(platform.profile, recipients, confirm_production_recipients)
        result = runner(
            ["sops", "--decrypt", "--extract", f'["{reference.key}"]', str(source)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise ConfigurationError(f"sops decryption failed for {source}: {(result.stderr or result.stdout or '').strip()}")
        target_dir = output_dir / reference.file
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_path = target_dir / reference.key
        target_path.write_text(result.stdout, encoding="utf-8")
        target_path.chmod(0o600)
    return platform


def cleanup(output_dir: Path = MATERIALIZED_SECRETS_DIR) -> None:
    """Remove the decrypted contents of a prior materialization directory."""
    if output_dir.exists():
        shutil.rmtree(output_dir)
