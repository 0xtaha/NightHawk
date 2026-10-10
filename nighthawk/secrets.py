"""Secret lifecycle on Vault's key-value store: prerequisite check, store, rotate, materialize.

Values are read from Vault only by these functions and are never placed in process arguments.
Writes use check-and-set, so a key is never replaced by accident and a concurrent change is refused.
"""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Mapping

from nighthawk.config import ROOT, ConfigurationError, Platform, SecretReference, mapping, string, vault_supports
from nighthawk.vault import Auth, Client, Transport, VaultError, connect, pki_roles

MATERIALIZED_SECRETS_DIR = ROOT / ".materialized-secrets"


def materialized_path(output_dir: Path, reference: SecretReference) -> Path:
    """Where `materialize` writes one referenced secret."""
    return output_dir / "kv" / reference.path / reference.key


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def doctor(
    matrix: dict, platform: Platform, auth: Auth = Auth(), environ: Mapping[str, str] | None = None,
    transport: Transport | None = None,
) -> tuple[list[Check], Client | None]:
    """Check the declared Vault; return the checks and, when the credential is accepted, a client."""
    vault = platform.vault
    checks: list[Check] = []
    try:
        health = Client(vault, None, transport).health()
    except VaultError as error:
        return [Check("vault", False, str(error))], None
    if not health.get("initialized") or health.get("sealed"):
        state = "sealed" if health.get("initialized") else "not initialized"
        return [Check("vault", False, f"Vault at {vault.address} is {state}")], None
    version = str(health.get("version", ""))
    checks.append(Check("vault", True, f"Vault {version} at {vault.address} is reachable and unsealed"))
    supported = mapping(mapping(mapping(matrix["secrets_store"])["vault"])["supported"])
    span = f"{string(supported['minimum'])} up to, not including, {string(supported['below'])}"
    try:
        in_range = vault_supports(matrix, version)
    except ConfigurationError:
        in_range = False
    checks.append(Check(
        "version", in_range,
        f"Vault {version} is supported" if in_range else f"Vault {version or 'of unknown version'} is outside the supported range {span}",
    ))
    try:
        client = connect(platform, auth, environ, transport)
        client.lookup_self()
    except VaultError as error:
        checks.append(Check("credential", False, str(error)))
        return checks, None
    checks.append(Check("credential", True, "the Vault credential is accepted"))
    kv = client.mount_type(vault.kv_mount)
    if kv is None:
        checks.append(Check("kv_mount", False, f"key-value mount {vault.kv_mount} does not exist or is not readable"))
    elif kv[0] != "kv" or str(kv[1].get("version")) != "2":
        checks.append(Check("kv_mount", False, f"{vault.kv_mount} is not a key-value version 2 mount"))
    else:
        checks.append(Check("kv_mount", True, f"key-value mount {vault.kv_mount} is version 2"))
    pki = client.mount_type(vault.pki_mount)
    if pki is None:
        checks.append(Check("pki_mount", False, f"PKI mount {vault.pki_mount} does not exist or is not readable"))
    elif pki[0] != "pki":
        checks.append(Check("pki_mount", False, f"{vault.pki_mount} is not a PKI mount"))
    elif not client.pki_ca_pem():
        checks.append(Check("pki_mount", False, f"PKI mount {vault.pki_mount} has no certificate authority"))
    else:
        checks.append(Check("pki_mount", True, f"PKI mount {vault.pki_mount} has a certificate authority"))
    wanted = pki_roles(platform)
    for role in (vault.server_role, vault.client_role):
        definition = client.pki_role(role) if pki is not None else None
        if definition is None:
            checks.append(Check(f"role {role}", False, f"PKI role {role} does not exist or is not readable"))
            continue
        refused = _not_allowed(definition, wanted[role])
        checks.append(Check(
            f"role {role}", not refused,
            f"PKI role {role} is present" if not refused else
            f"PKI role {role} does not allow {', '.join(refused)}; apply the rendered vault/pki-roles.json",
        ))
    return checks, client


def _not_allowed(role: dict, wanted: dict) -> list[str]:
    """Declared hostnames and collector identities that a PKI role in Vault would refuse to sign."""
    if role.get("allow_any_name"):
        return []
    domains, patterns = list(role.get("allowed_domains") or []), list(role.get("allowed_uri_sans") or [])

    def domain_allowed(name: str) -> bool:
        return any(
            (role.get("allow_bare_domains") and name == domain)
            or (role.get("allow_subdomains") and name.endswith(f".{domain}"))
            or (role.get("allow_glob_domains") and fnmatchcase(name, domain))
            for domain in domains
        )

    refused = [name for name in wanted["allowed_domains"] if not domain_allowed(name)]
    refused += [name for name in wanted["allowed_uri_sans"] if not any(fnmatchcase(name, pattern) for pattern in patterns)]
    return refused


def ensure_doctor_ok(
    matrix: dict, platform: Platform, auth: Auth = Auth(), environ: Mapping[str, str] | None = None,
    transport: Transport | None = None,
) -> Client:
    """Return a client only when every prerequisite holds; otherwise raise before anything is read or written."""
    checks, client = doctor(matrix, platform, auth, environ, transport)
    failures = [check.detail for check in checks if not check.ok]
    if failures or client is None:
        raise ConfigurationError("Vault prerequisite check failed: " + "; ".join(failures))
    return client


def _reference(platform: Platform, secret_ref: str) -> SecretReference:
    if secret_ref not in platform.secrets:
        raise ConfigurationError(f"{secret_ref!r} is not a secret reference of this platform document")
    return platform.secrets[secret_ref]


def _check_value(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ConfigurationError("a secret value must be a non-empty string")


def secret_exists(client: Client, reference: SecretReference) -> bool:
    return reference.key in client.kv_read(reference.path)[0]


def store_secret(client: Client, platform: Platform, secret_ref: str, value: str) -> SecretReference:
    """Store `value` at a reference that holds nothing yet. Every other key at the path is kept."""
    reference = _reference(platform, secret_ref)
    _check_value(value)
    data, version = client.kv_read(reference.path)
    if reference.key in data:
        raise ConfigurationError(
            f"{secret_ref}: {reference.path} key {reference.key!r} already holds a value; use rotate-secret to replace it"
        )
    client.kv_write(reference.path, {**data, reference.key: value}, version)
    return reference


def rotate_secret(client: Client, platform: Platform, secret_ref: str, value: str) -> SecretReference:
    """Replace an existing value with a new version. Every other key at the path is kept."""
    reference = _reference(platform, secret_ref)
    _check_value(value)
    data, version = client.kv_read(reference.path)
    if reference.key not in data:
        raise ConfigurationError(
            f"{secret_ref}: {reference.path} key {reference.key!r} holds no value; cannot rotate a secret that does not exist"
        )
    client.kv_write(reference.path, {**data, reference.key: value}, version)
    return reference


def read_secrets(client: Client, platform: Platform) -> dict[str, str]:
    """Every referenced secret by reference name; fails naming all that hold no value."""
    by_path: dict[str, dict[str, str]] = {}
    values: dict[str, str] = {}
    missing: list[str] = []
    for name, reference in sorted(platform.secrets.items()):
        if reference.path not in by_path:
            by_path[reference.path] = client.kv_read(reference.path)[0]
        value = by_path[reference.path].get(reference.key)
        if not isinstance(value, str) or not value:
            missing.append(f"{name} ({reference.path} key {reference.key})")
        else:
            values[name] = value
    if missing:
        raise ConfigurationError("these secrets hold no value in Vault: " + ", ".join(missing))
    return values


def materialize(platform: Platform, client: Client, output_dir: Path = MATERIALIZED_SECRETS_DIR) -> None:
    """Read every secret a validated platform document references into owner-only files under `output_dir`."""
    if output_dir.exists():
        mode = stat.S_IMODE(output_dir.stat().st_mode)
        if mode != 0o700:
            raise ConfigurationError(
                f"{output_dir}: refusing to materialize into an existing directory with unexpected permissions {oct(mode)}"
            )
    # Everything is read before anything is written, so a missing secret leaves no partial result.
    values = read_secrets(client, platform)
    output_dir.mkdir(mode=0o700, exist_ok=True)
    wanted: set[Path] = set()
    for name, value in values.items():
        target = materialized_path(output_dir, platform.secrets[name])
        directory = output_dir
        for part in target.parent.relative_to(output_dir).parts:
            directory = directory / part
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        if not target.exists() or target.read_text(encoding="utf-8") != value:
            target.write_text(value, encoding="utf-8")
        target.chmod(0o600)
        wanted.add(target)
    root = output_dir / "kv"
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and path not in wanted:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def cleanup(output_dir: Path = MATERIALIZED_SECRETS_DIR) -> None:
    """Remove the contents of a prior materialization directory."""
    if output_dir.exists():
        shutil.rmtree(output_dir)
