"""Shared test doubles: an in-memory fake Vault and platform-document builders."""

from __future__ import annotations

import base64
import copy
import datetime
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nighthawk import vault
from nighthawk.config import ROOT, Platform
from nighthawk.secrets import materialized_path

TOKEN = "test-token-not-a-real-credential"
ROOT_TOKEN = "test-root-token-not-a-real-credential"


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeVault:
    """A Vault transport: versioned key-value storage with check-and-set, PKI signing, and token lookup.

    Call it as the `transport` of a `nighthawk.vault.Client`. `calls` records every request so
    tests can assert what was, and was not, sent.
    """

    def __init__(self, platform: Platform | None = None, *, configured: bool = True, version: str = "2.1.2") -> None:
        self.version, self.sealed, self.initialized, self.reachable = version, False, True, True
        self.tokens: dict[str, list[str]] = {TOKEN: ["default", vault.POLICY_NAME], ROOT_TOKEN: ["root"]}
        self.approles: dict[tuple[str, str], str] = {}
        self.mounts: dict[str, dict] = {}
        self.kv: dict[str, list[dict[str, str]]] = {}
        self.roles: dict[str, dict] = {}
        self.policies: dict[str, str] = {}
        self.auth_mounts: dict[str, dict] = {}
        self.auth_roles: dict[tuple[str, str], dict] = {}
        self.auth_configs: dict[str, dict] = {}
        self.revoked: list[str] = []
        self.denied: set[str] = set()
        self.conflict_next_write = False
        self.tamper: dict = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.headers: list[dict[str, str]] = []
        self.ca_key = None
        self.ca_certificate = None
        self.kv_mount, self.pki_mount = "nighthawk-kv", "nighthawk-pki"
        if platform is not None:
            self.kv_mount, self.pki_mount = platform.vault.kv_mount, platform.vault.pki_mount
            if configured:
                self.mounts = {self.kv_mount: {"type": "kv", "options": {"version": "2"}},
                               self.pki_mount: {"type": "pki", "options": {}}}
                self.roles = copy.deepcopy(vault.pki_roles(platform))
                self.policies[vault.POLICY_NAME] = vault.policy_hcl(platform)
                self.new_authority()

    # ---- helpers for tests ---------------------------------------------------------------

    def new_authority(self, days: int = 3650) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake Vault authority")])
        self.ca_certificate = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self.ca_key, hashes.SHA256())
        )

    def ca_pem(self) -> str:
        return self.ca_certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def values(self, path: str) -> dict[str, str]:
        return dict(self.kv[path][-1]) if self.kv.get(path) else {}

    def sent(self) -> str:
        """Everything that went into a URL, for asserting nothing secret travels there."""
        return " ".join(f"{method} {path}" for method, path, _ in self.calls)

    # ---- transport -------------------------------------------------------------------------

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        if not self.reachable:
            raise vault.VaultError(f"Vault at {urlsplit(url).scheme}://{urlsplit(url).netloc} is unreachable: refused")
        split = urlsplit(url)
        path = split.path.removeprefix("/v1/")
        payload = json.loads(body) if body else None
        self.calls.append((method, path + (f"?{split.query}" if split.query else ""), payload))
        self.headers.append(dict(headers))
        status, result = self._route(method, path, payload, headers.get("X-Vault-Token"))
        if isinstance(result, str):
            return status, result.encode("utf-8")
        return status, b"" if result is None else json.dumps(result).encode("utf-8")

    def _route(self, method: str, path: str, payload: dict | None, token: str | None):
        if path == "sys/health":
            return 200, {"initialized": self.initialized, "sealed": self.sealed, "version": self.version}
        if path == f"{self.pki_mount}/ca/pem":
            if self.ca_certificate is None:
                return 204, None
            return 200, self.ca_certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
        if path == f"auth/{vault.APPROLE_MOUNT}/login":
            issued = self.approles.get((payload["role_id"], payload["secret_id"]))
            if issued is None:
                return 400, {"errors": ["invalid role or secret ID"]}
            return 200, {"auth": {"client_token": issued}}
        if self.sealed:
            return 503, {"errors": ["Vault is sealed"]}
        if token not in self.tokens or path in self.denied:
            return 403, {"errors": ["permission denied"]}
        root = "root" in self.tokens[token]
        if path == "auth/token/lookup-self":
            return 200, {"data": {"policies": self.tokens[token]}}
        if path.startswith("sys/internal/ui/mounts/"):
            mount = self.mounts.get(path.removeprefix("sys/internal/ui/mounts/"))
            return (200, {"data": mount}) if mount else (403, {"errors": ["preflight capability check returned 403"]})
        if path.startswith("sys/") and not root:
            return 403, {"errors": ["permission denied"]}
        if path == "sys/mounts" and method == "GET":
            return 200, {"data": {f"{name}/": value for name, value in self.mounts.items()}}
        if path.startswith("sys/mounts/"):
            self.mounts[path.removeprefix("sys/mounts/")] = {"type": payload["type"], "options": payload.get("options") or {}}
            return 204, None
        if path == "sys/auth" and method == "GET":
            return 200, {"data": {f"{name}/": value for name, value in self.auth_mounts.items()}}
        if path.startswith("sys/auth/"):
            self.auth_mounts[path.removeprefix("sys/auth/")] = {"type": payload["type"]}
            return 204, None
        if path.startswith("auth/") and "/role/" in path:
            mount, _, name = path.removeprefix("auth/").partition("/role/")
            if mount not in self.auth_mounts:
                return 404, {"errors": ["no handler for route"]}
            if method == "GET":
                role = self.auth_roles.get((mount, name))
                return (200, {"data": role}) if role else (404, {"errors": []})
            self.auth_roles[(mount, name)] = dict(payload)
            return 204, None
        if path.startswith("auth/") and path.endswith("/config"):
            mount = path.removeprefix("auth/").removesuffix("/config")
            if method == "GET":
                config = self.auth_configs.get(mount)
                return (200, {"data": config}) if config else (404, {"errors": []})
            self.auth_configs[mount] = dict(payload)
            return 204, None
        if path.startswith("sys/policies/acl/"):
            name = path.removeprefix("sys/policies/acl/")
            if method == "GET":
                return (200, {"data": {"policy": self.policies[name]}}) if name in self.policies else (404, {"errors": []})
            self.policies[name] = payload["policy"]
            return 204, None
        if path.startswith(f"{self.kv_mount}/data/"):
            return self._kv(method, path.removeprefix(f"{self.kv_mount}/data/"), payload)
        if path.startswith(f"{self.pki_mount}/"):
            return self._pki(method, path.removeprefix(f"{self.pki_mount}/"), payload)
        return 404, {"errors": ["no handler for route"]}

    def _kv(self, method: str, path: str, payload: dict | None):
        if self.kv_mount not in self.mounts:
            return 404, {"errors": ["no handler for route"]}
        versions = self.kv.get(path, [])
        if method == "GET":
            if not versions:
                return 404, {"errors": []}
            return 200, {"data": {"data": versions[-1], "metadata": {"version": len(versions)}}}
        if self.conflict_next_write or payload["options"]["cas"] != len(versions):
            self.conflict_next_write = False
            return 400, {"errors": ["check-and-set parameter did not match the current version"]}
        self.kv.setdefault(path, []).append(dict(payload["data"]))
        return 200, {"data": {"version": len(self.kv[path])}}

    def _pki(self, method: str, path: str, payload: dict | None):
        if self.pki_mount not in self.mounts:
            return 404, {"errors": ["no handler for route"]}
        if path.startswith("roles/"):
            name = path.removeprefix("roles/")
            if method == "GET":
                if name not in self.roles:
                    return 404, {"errors": []}
                return 200, {"data": vault._comparable(self.roles[name])}
            self.roles[name] = dict(payload)
            return 204, None
        if path == "root/generate/internal":
            self.new_authority(int(payload["ttl"].rstrip("h")) // 24)
            return 200, {"data": {}}
        if path == "revoke":
            self.revoked.append(payload["serial_number"])
            return 200, {"data": {}}
        if path.startswith("sign/"):
            return self._sign(self.roles.get(path.removeprefix("sign/")), payload)
        return 404, {"errors": ["no handler for route"]}

    def _sign(self, role: dict | None, payload: dict):
        if role is None:
            return 404, {"errors": ["unknown role"]}
        request = x509.load_pem_x509_csr(payload["csr"].encode("ascii"))
        try:
            names = list(request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
        except x509.ExtensionNotFound:
            names = []
        for name in names:
            allowed = role["allowed_domains"] if isinstance(name, x509.DNSName) else role["allowed_uri_sans"]
            if not isinstance(name, (x509.DNSName, x509.UniformResourceIdentifier)) or name.value not in allowed:
                return 400, {"errors": [f"subject alternate name {name.value} not allowed by this role"]}
        names = names + list(self.tamper.get("extra_names", []))
        hours = self.tamper.get("hours", int(payload["ttl"].rstrip("h")))
        usage = self.tamper.get("usage") or (
            ExtendedKeyUsageOID.SERVER_AUTH if role["server_flag"] else ExtendedKeyUsageOID.CLIENT_AUTH
        )
        signer = self.tamper.get("signer", self.ca_key)
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (
            x509.CertificateBuilder().subject_name(request.subject).issuer_name(self.ca_certificate.subject)
            .public_key(self.tamper.get("public_key", request.public_key()))
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(seconds=30))
            .not_valid_after(now + datetime.timedelta(hours=hours))
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .sign(signer, hashes.SHA256())
        )
        return 200, {"data": {
            "certificate": certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            "serial_number": f"{certificate.serial_number:x}",
        }}


def fake_client(platform: Platform, fake: FakeVault | None = None, token: str = TOKEN) -> vault.Client:
    return vault.Client(platform.vault, token, fake if fake is not None else FakeVault(platform))


def example_document() -> dict:
    return yaml.safe_load((ROOT / "config" / "tenants.example.yaml").read_text(encoding="utf-8"))


def add_stream(data: dict, tenant_id: str, stream_id: str, backend_id: str, certificate: bool = False) -> None:
    stream = copy.deepcopy(data["tenants"][0]["datastreams"][0])
    stream.update(id=stream_id, backend_id=backend_id)
    tenant = next((item for item in data["tenants"] if item["id"] == tenant_id), None)
    if tenant is None:
        tenant = {"id": tenant_id, "datastreams": []}
        data["tenants"].append(tenant)
    tenant["datastreams"].append(stream)
    for permission in ("ingest", "query"):
        credential_id = f"{tenant_id}-{stream_id}-{permission}"
        data["secrets"][credential_id] = {"path": "nighthawk/local", "key": credential_id}
        credential = {
            "id": credential_id, "secret_ref": credential_id,
            "tenant": tenant_id, "datastream": stream_id, "permission": permission,
        }
        if certificate and permission == "ingest":
            credential["certificate_identity"] = f"spiffe://nighthawk/{tenant_id}/{stream_id}/collector"
        data["credentials"].append(credential)


def four_stream_document() -> dict:
    """Two customers with two datastreams each."""
    data = example_document()
    add_stream(data, "example", "infrastructure", "example-infrastructure")
    add_stream(data, "second", "application", "second-application")
    add_stream(data, "second", "infrastructure", "second-infrastructure")
    return data


def write_document(directory: Path, data: dict, name: str = "platform.yaml") -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def secret_for(credential_id: str) -> str:
    return f"{credential_id}-" + "s" * 40


def materialize_plain(platform: Platform, directory: Path, overrides: dict[str, str] | None = None) -> Path:
    """Write gateway credential secrets where `materialize-secrets` would put them."""
    for credential in platform.credentials:
        target = materialized_path(directory, platform.secrets[credential.secret_ref])
        target.parent.mkdir(parents=True, exist_ok=True)
        value = (overrides or {}).get(credential.id, secret_for(credential.id))
        target.write_text(value + "\n", encoding="utf-8")
    return directory


def basic(credential_id: str, secret: str | None = None) -> str:
    value = f"{credential_id}:{secret_for(credential_id) if secret is None else secret}"
    return "Basic " + base64.b64encode(value.encode("utf-8")).decode("ascii")


def forwarded_certificate(path: Path) -> str:
    """Encode a certificate as the pinned Traefik passTLSClientCert middleware does."""
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    return base64.b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode("ascii")


def run_cli(fake: FakeVault, argv: list[str], *, token: str | None = TOKEN, stdin: str = "") -> tuple[int, str, str]:
    """Run the command line against a fake Vault; returns (status, stdout, stderr)."""
    import io
    import os
    from contextlib import redirect_stderr, redirect_stdout
    from unittest import mock

    from nighthawk.__main__ import main

    environment = {key: value for key, value in os.environ.items() if key != "VAULT_TOKEN"}
    if token is not None:
        environment["VAULT_TOKEN"] = token
    output, errors = io.StringIO(), io.StringIO()
    with mock.patch("nighthawk.vault.http_transport", return_value=fake), \
            mock.patch.dict(os.environ, environment, clear=True), \
            mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(output), redirect_stderr(errors):
        status = main(argv)
    return status, output.getvalue(), errors.getvalue()
