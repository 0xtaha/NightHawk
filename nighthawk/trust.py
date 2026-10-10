"""Gateway credential generation and X.509 issuance through Vault.

Credentials and storage identities are stored only in Vault. Certificates are signed by the
authority in Vault's PKI mount from a locally generated key; that authority's key never leaves Vault.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets as token_source
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nighthawk.config import ConfigurationError, Platform, SecretReference
from nighthawk.secrets import store_secret
from nighthawk.vault import Client

# How far the granted expiry may differ from the requested one before the certificate is refused.
VALIDITY_TOLERANCE = datetime.timedelta(minutes=10)


def generate_credential(platform: Platform, credential_id: str, client: Client) -> SecretReference:
    """Create a high-entropy gateway secret for a declared credential. The plaintext is never returned."""
    credential = next((item for item in platform.credentials if item.id == credential_id), None)
    if credential is None:
        raise ConfigurationError(f"unknown credential {credential_id!r}")
    return store_secret(client, platform, credential.secret_ref, token_source.token_urlsafe(32))


def generate_cluster_token(platform: Platform, client: Client) -> tuple[SecretReference, bool]:
    """Create the cluster join token once; return its reference and whether it was created now.

    An existing token is never replaced, because nodes already hold it, and Vault is not written to.
    """
    secret_ref = platform.cluster_join_token_secret_ref
    if secret_ref is None:
        raise ConfigurationError("the platform document declares no cluster.join_token_secret_ref")
    reference = platform.secrets[secret_ref]
    if reference.key in client.kv_read(reference.path)[0]:
        return reference, False
    # k3s accepts any string; hexadecimal avoids every quoting question in a file or a unit.
    return store_secret(client, platform, secret_ref, token_source.token_hex(32)), True


def storage_identity_refs(platform: Platform) -> list[str]:
    return sorted({
        binding.identity.ref for binding in platform.bindings.values() if binding.identity.type == "secret"
    })


def generate_storage_identity(platform: Platform, identity_ref: str, client: Client) -> SecretReference:
    """Create an S3 access key and secret key for a declared local storage identity."""
    if identity_ref not in storage_identity_refs(platform):
        raise ConfigurationError(f"{identity_ref!r} is not a storage identity of this platform document")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    value = json.dumps({
        "access_key": "".join(token_source.choice(alphabet) for _ in range(20)),
        "secret_key": token_source.token_urlsafe(30),
    }, sort_keys=True)
    return store_secret(client, platform, identity_ref, value)


def storage_hostnames(platform: Platform) -> list[str]:
    if platform.storage_provider != "seaweedfs":
        raise ConfigurationError("a storage server certificate is issued only for local SeaweedFS bindings")
    return sorted({urlsplit(binding.endpoint).hostname or "" for binding in platform.bindings.values()})


def authority_certificate(client: Client) -> str:
    """The public certificate of the authority in the declared PKI mount."""
    pem = client.pki_ca_pem()
    if not pem:
        raise ConfigurationError(f"PKI mount {client.settings.pki_mount} has no certificate authority")
    return pem


def _private_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode("ascii")


@dataclass(frozen=True)
class IssuedCertificate:
    certificate_path: Path
    key_path: Path
    fingerprint: str


def _verify_granted(
    certificate: x509.Certificate, authority: x509.Certificate, key: ec.EllipticCurvePrivateKey,
    names: list[x509.GeneralName], usage: x509.ObjectIdentifier, not_after: datetime.datetime,
) -> None:
    """Refuse a certificate that is anything other than what was requested."""
    def fail(what: str) -> None:
        raise ConfigurationError(f"Vault returned a certificate whose {what} differs from the request; nothing was written")

    public = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if certificate.public_key().public_bytes(*public) != key.public_key().public_bytes(*public):
        fail("public key")
    try:
        granted_names = list(certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
        granted_usage = list(certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
    except x509.ExtensionNotFound:
        fail("extensions")
    try:
        is_authority = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        # Vault omits basic constraints on a leaf unless the role asks for them; absent means not an authority.
        is_authority = False
    if sorted(map(repr, granted_names)) != sorted(map(repr, names)):
        fail("names or identity")
    if granted_usage != [usage] or is_authority:
        fail("usage")
    if abs(certificate.not_valid_after_utc - not_after) > VALIDITY_TOLERANCE:
        fail("validity")
    try:
        certificate.verify_directly_issued_by(authority)
    except (ValueError, TypeError, InvalidSignature):
        fail("issuer")


@dataclass(frozen=True)
class _Target:
    stem: str
    common_name: str
    role: str
    names: list[x509.GeneralName]
    usage: x509.ObjectIdentifier


def _target(platform: Platform, credential_id: str | None, server: bool, storage: bool) -> _Target:
    if (credential_id is not None) + server + storage != 1:
        raise ConfigurationError("choose exactly one of a credential, the gateway server, or the storage server")
    gateway = platform.gateway
    if server:
        # One certificate for both names served by the gateway listener.
        stem, common_name, role = "gateway-server", gateway.hostname, platform.vault.server_role
        names: list[x509.GeneralName] = [x509.DNSName(gateway.hostname), x509.DNSName(platform.grafana.hostname)]
        usage = ExtendedKeyUsageOID.SERVER_AUTH
    elif storage:
        hostnames = storage_hostnames(platform)
        stem, common_name, role = "storage-server", hostnames[0], platform.vault.server_role
        names = [x509.DNSName(name) for name in hostnames]
        usage = ExtendedKeyUsageOID.SERVER_AUTH
    else:
        credential = next((item for item in platform.credentials if item.id == credential_id), None)
        if credential is None or credential.certificate_identity is None:
            raise ConfigurationError(f"{credential_id!r} is not a credential that declares a certificate identity")
        stem, common_name = credential.id, credential.certificate_identity.rsplit("/", 1)[1]
        role = platform.vault.client_role
        names = [x509.UniformResourceIdentifier(credential.certificate_identity)]
        usage = ExtendedKeyUsageOID.CLIENT_AUTH
    return _Target(stem, common_name, role, names, usage)


def _check_validity(valid_days: int | None) -> None:
    if valid_days is None:
        raise ConfigurationError("an explicit validity period is required; there is no default")
    if isinstance(valid_days, bool) or not isinstance(valid_days, int) or valid_days < 1:
        raise ConfigurationError("an explicit validity of at least one day is required")


def _authority(client: Client) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(authority_certificate(client).encode("ascii"))
    except ValueError as error:
        raise ConfigurationError(f"the PKI mount's authority certificate is unusable: {error}") from error


def needs_issue(
    certificate_path: Path, key_path: Path, authority: x509.Certificate, renew_before: datetime.timedelta,
) -> bool:
    """Missing, unreadable, expiring within the period, or signed by an authority Vault no longer has."""
    if not certificate_path.exists() or not key_path.exists():
        return True
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        certificate.verify_directly_issued_by(authority)
    except (ValueError, TypeError, InvalidSignature):
        return True
    return certificate.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) < renew_before


def issue_certificate(
    platform: Platform, output_dir: Path, valid_days: int | None, client: Client, *,
    credential_id: str | None = None, server: bool = False, storage: bool = False,
) -> IssuedCertificate:
    """Obtain a collector client certificate, the gateway server certificate, or the storage server certificate."""
    target = _target(platform, credential_id, server, storage)
    _check_validity(valid_days)
    for path in (output_dir / f"{target.stem}.crt.pem", output_dir / f"{target.stem}.key.pem"):
        if path.exists():
            raise ConfigurationError(f"{path}: refusing to overwrite an existing certificate or key")
    return _issue(target, output_dir, valid_days, client, _authority(client))


def ensure_certificate(
    platform: Platform, output_dir: Path, valid_days: int | None, client: Client, renew_before_days: int | None, *,
    credential_id: str | None = None, server: bool = False, storage: bool = False,
) -> IssuedCertificate | None:
    """Issue only when the pair is missing, expiring within the period, or from another authority.

    Returns None when there was nothing to do. A replacement is requested and verified in a
    directory beside the pair and renamed into place afterwards, so a failed request leaves
    the existing certificate and key as they were.
    """
    target = _target(platform, credential_id, server, storage)
    _check_validity(valid_days)
    if isinstance(renew_before_days, bool) or not isinstance(renew_before_days, int) or renew_before_days < 0:
        raise ConfigurationError("a renewal period of zero or more days is required with --if-needed")
    if renew_before_days >= valid_days:
        raise ConfigurationError(
            f"a renewal period of {renew_before_days} day(s) is not shorter than the validity of {valid_days} day(s); "
            "every run would issue a new certificate"
        )
    certificate_path, key_path = output_dir / f"{target.stem}.crt.pem", output_dir / f"{target.stem}.key.pem"
    authority = _authority(client)
    if not needs_issue(certificate_path, key_path, authority, datetime.timedelta(days=renew_before_days)):
        return None
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.stem}.", dir=output_dir))
    try:
        issued = _issue(target, staging, valid_days, client, authority)
        # The key first: a certificate without its key is the state a later run repairs.
        os.replace(issued.key_path, key_path)
        os.replace(issued.certificate_path, certificate_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return IssuedCertificate(certificate_path, key_path, issued.fingerprint)


def _issue(
    target: _Target, output_dir: Path, valid_days: int, client: Client, authority: x509.Certificate,
) -> IssuedCertificate:
    stem, common_name, role, names, usage = target.stem, target.common_name, target.role, target.names, target.usage
    certificate_path, key_path = output_dir / f"{stem}.crt.pem", output_dir / f"{stem}.key.pem"
    not_after = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=valid_days)
    if not_after > authority.not_valid_after_utc:
        raise ConfigurationError("requested validity outlives the certificate authority")
    # Only the signing request leaves this machine; the private key is generated and kept here.
    key = ec.generate_private_key(ec.SECP256R1())
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    granted = client.pki_sign(
        role, request.public_bytes(serialization.Encoding.PEM).decode("ascii"), common_name, f"{valid_days * 24}h",
    )
    try:
        certificate = x509.load_pem_x509_certificate(str(granted["certificate"]).encode("ascii"))
    except (KeyError, ValueError) as error:
        raise ConfigurationError(f"Vault returned an unusable certificate ({type(error).__name__})") from None
    _verify_granted(certificate, authority, key, names, usage, not_after)
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(_private_pem(key))
    with certificate_path.open("x", encoding="ascii") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"))
    certificate_path.chmod(0o600)
    return IssuedCertificate(certificate_path, key_path, certificate.fingerprint(hashes.SHA256()).hex())


def revoke_certificate(client: Client, certificate_path: Path) -> str:
    """Revoke a certificate in Vault; return the fingerprint to list in the platform document."""
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"{certificate_path}: not a readable certificate ({type(error).__name__})") from None
    serial = f"{certificate.serial_number:x}"
    serial = "0" * (len(serial) % 2) + serial
    client.pki_revoke(":".join(serial[index:index + 2] for index in range(0, len(serial), 2)))
    return certificate.fingerprint(hashes.SHA256()).hex()
