"""Gateway credential generation and X.509 issuance; private material is only ever stored SOPS-encrypted."""

from __future__ import annotations

import datetime
import json
import os
import secrets as token_source
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nighthawk.config import ROOT, ConfigurationError, Platform, SecretReference
from nighthawk.secrets import Runner, _extract_sops_recipients, encrypt_secret, guard_production_recipients


def secret_key_exists(path: Path, key: str) -> bool:
    """SOPS keeps key names in clear text, so presence can be checked without decrypting."""
    if not path.exists():
        return False
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"{path}: cannot read SOPS file: {error}") from error
    return isinstance(document, dict) and key in document


def store_new_secret(
    reference: SecretReference, value: str, recipients: list[str], *, root: Path = ROOT,
    profile: str = "development", confirm_production_recipients: bool = False,
    runner: Runner = subprocess.run,
) -> Path:
    """Encrypt `value` at a file/key that holds nothing yet; never replaces an existing value."""
    target = root / reference.file
    if secret_key_exists(target, reference.key):
        raise ConfigurationError(
            f"{reference.file}: key {reference.key!r} already holds a value; rotate it instead of regenerating"
        )
    if not target.exists():
        return encrypt_secret(
            target.name, reference.key, value, recipients, secrets_dir=target.parent, profile=profile,
            confirm_production_recipients=confirm_production_recipients, runner=runner,
        )
    # The file exists and keeps its recipients. The value goes over stdin, not argv.
    guard_production_recipients(profile, _extract_sops_recipients(target), confirm_production_recipients)
    result = runner(
        ["sops", "set", "--value-stdin", str(target), f'["{reference.key}"]'],
        input=json.dumps(value), capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise ConfigurationError(f"sops could not add {reference.key!r}: {(result.stderr or result.stdout or '').strip()}")
    return target


def _decrypt(reference: SecretReference, root: Path, runner: Runner) -> str:
    source = root / reference.file
    if not secret_key_exists(source, reference.key):
        raise ConfigurationError(f"{reference.file}: key {reference.key!r} has not been created")
    result = runner(
        ["sops", "--decrypt", "--extract", f'["{reference.key}"]', str(source)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise ConfigurationError(f"sops decryption failed for {source}: {(result.stderr or result.stdout or '').strip()}")
    return result.stdout


def generate_credential(
    platform: Platform, credential_id: str, recipients: list[str], *, root: Path = ROOT,
    confirm_production_recipients: bool = False, runner: Runner = subprocess.run,
) -> Path:
    """Create a high-entropy gateway secret for a declared credential. The plaintext is never returned."""
    credential = next((item for item in platform.credentials if item.id == credential_id), None)
    if credential is None:
        raise ConfigurationError(f"unknown credential {credential_id!r}")
    return store_new_secret(
        platform.secrets[credential.secret_ref], token_source.token_urlsafe(32), recipients, root=root,
        profile=platform.profile, confirm_production_recipients=confirm_production_recipients, runner=runner,
    )


def _validity(valid_days: int) -> tuple[datetime.datetime, datetime.datetime]:
    if isinstance(valid_days, bool) or not isinstance(valid_days, int) or valid_days < 1:
        raise ConfigurationError("an explicit validity of at least one day is required")
    now = datetime.datetime.now(datetime.timezone.utc)
    return now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=valid_days)


def _private_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("ascii")


def init_ca(
    platform: Platform, recipients: list[str], valid_days: int, *, root: Path = ROOT,
    confirm_local_ca: bool = False, confirm_production_recipients: bool = False,
    runner: Runner = subprocess.run,
) -> str:
    """Create a locally managed client CA; returns the CA certificate's SHA-256 fingerprint."""
    gateway = platform.gateway
    if gateway.client_ca_key_secret_ref is None:
        raise ConfigurationError("gateway.client_ca_key_secret_ref is required for a locally managed CA")
    if platform.profile == "production" and not confirm_local_ca:
        raise ConfigurationError(
            "profile: production requires an operator-supplied client CA; "
            "a locally generated CA needs explicit confirmation"
        )
    key_reference = platform.secrets[gateway.client_ca_key_secret_ref]
    certificate_reference = platform.secrets[gateway.client_ca_secret_ref]
    for reference in (key_reference, certificate_reference):
        if secret_key_exists(root / reference.file, reference.key):
            raise ConfigurationError(f"{reference.file}: key {reference.key!r} already holds a value; refusing to replace a CA")
    not_before, not_after = _validity(valid_days)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"NightHawk gateway client CA ({gateway.hostname})")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(not_before).not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    common = {"root": root, "profile": platform.profile,
              "confirm_production_recipients": confirm_production_recipients, "runner": runner}
    store_new_secret(key_reference, _private_pem(key), recipients, **common)
    store_new_secret(
        certificate_reference, certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        recipients, **common,
    )
    return certificate.fingerprint(hashes.SHA256()).hex()


@dataclass(frozen=True)
class IssuedCertificate:
    certificate_path: Path
    key_path: Path
    fingerprint: str


def issue_certificate(
    platform: Platform, output_dir: Path, valid_days: int | None, *, credential_id: str | None = None,
    server: bool = False, root: Path = ROOT, runner: Runner = subprocess.run,
) -> IssuedCertificate:
    """Issue a collector client certificate for a declared identity, or the gateway server certificate."""
    if server == (credential_id is not None):
        raise ConfigurationError("choose exactly one of a credential or the gateway server certificate")
    if valid_days is None:
        raise ConfigurationError("an explicit validity period is required; there is no default")
    gateway = platform.gateway
    if server:
        stem, common_name = "gateway-server", gateway.hostname
        alternative_name: x509.GeneralName = x509.DNSName(gateway.hostname)
        usage = ExtendedKeyUsageOID.SERVER_AUTH
    else:
        credential = next((item for item in platform.credentials if item.id == credential_id), None)
        if credential is None or credential.certificate_identity is None:
            raise ConfigurationError(f"{credential_id!r} is not a credential that declares a certificate identity")
        stem, common_name = credential.id, credential.certificate_identity.rsplit("/", 1)[1]
        alternative_name = x509.UniformResourceIdentifier(credential.certificate_identity)
        usage = ExtendedKeyUsageOID.CLIENT_AUTH
    certificate_path, key_path = output_dir / f"{stem}.crt.pem", output_dir / f"{stem}.key.pem"
    for path in (certificate_path, key_path):
        if path.exists():
            raise ConfigurationError(f"{path}: refusing to overwrite an existing certificate or key")
    not_before, not_after = _validity(valid_days)
    if gateway.client_ca_key_secret_ref is None:
        raise ConfigurationError("gateway.client_ca_key_secret_ref is required to issue certificates locally")
    try:
        ca_key = serialization.load_pem_private_key(
            _decrypt(platform.secrets[gateway.client_ca_key_secret_ref], root, runner).encode("ascii"), None,
        )
        ca_certificate = x509.load_pem_x509_certificate(
            _decrypt(platform.secrets[gateway.client_ca_secret_ref], root, runner).encode("ascii"),
        )
    except (ValueError, TypeError, UnicodeError) as error:
        raise ConfigurationError(f"the stored client CA is unusable: {error}") from error
    if not_after > ca_certificate.not_valid_after_utc:
        raise ConfigurationError("requested validity outlives the client CA")
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_certificate.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(not_before).not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .add_extension(x509.SubjectAlternativeName([alternative_name]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_certificate.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(_private_pem(key))
    with certificate_path.open("x", encoding="ascii") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"))
    certificate_path.chmod(0o600)
    return IssuedCertificate(certificate_path, key_path, certificate.fingerprint(hashes.SHA256()).hex())
