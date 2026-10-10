"""A small client for the parts of Vault's HTTP API the platform uses, and what it needs from Vault.

Only the command-line tool talks to Vault. No error raised here contains a token or a secret value.
"""

from __future__ import annotations

import json
import os
import ssl
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from nighthawk.config import ConfigurationError, Platform, Vault

# (method, url, headers, body, timeout) -> (status, body)
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]

APPROLE_MOUNT = "approle"
POLICY_NAME = "nighthawk"
# Upper bound the rendered PKI roles allow; a request states its own validity.
MAX_CERTIFICATE_TTL = "9528h"
CREDENTIAL_HELP = (
    "set VAULT_TOKEN, or pass --vault-token-file, or pass --vault-role-id-file with --vault-secret-id-file"
)


class VaultError(ConfigurationError):
    """Vault refused or could not serve a request."""


class VaultConflict(VaultError):
    """A check-and-set write lost to a concurrent change; nothing was overwritten."""


def http_transport(ca_file: str | None = None) -> Transport:
    context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()

    def send(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        handlers = [urllib.request.HTTPSHandler(context=context)] if urlsplit(url).scheme == "https" else []
        try:
            with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()
        except (urllib.error.URLError, OSError) as error:
            origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
            raise VaultError(f"Vault at {origin} is unreachable: {getattr(error, 'reason', error)}") from None

    return send


@dataclass(frozen=True)
class Auth:
    """Where a Vault credential comes from besides the environment. Never the credential itself."""

    token_file: Path | None = None
    role_id_file: Path | None = None
    secret_id_file: Path | None = None


def _read_credential_file(path: Path) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise VaultError(f"{path}: credential file is readable by group or others ({oct(mode)}); restrict it to its owner")
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise VaultError(f"{path}: cannot read credential file ({type(error).__name__})") from None
    if not value:
        raise VaultError(f"{path}: credential file is empty")
    return value


class Client:
    def __init__(self, settings: Vault, token: str | None = None, transport: Transport | None = None) -> None:
        self.settings = settings
        self._token = token
        self._transport = transport if transport is not None else http_transport(settings.ca_file)

    def __repr__(self) -> str:  # never show the token
        return f"Client({self.settings.address!r})"

    def call(
        self, method: str, path: str, body: dict | None = None, *, operation: str,
        missing_ok: bool = False, denied_ok: bool = False, raw: bool = False, authenticated: bool = True,
    ):
        """Send one request. Returns the decoded body, `None` for a tolerated 404 or 403, or text when `raw`."""
        headers = {"Accept": "application/json"}
        if authenticated:
            if self._token is None:
                raise VaultError(f"no Vault credential: {CREDENTIAL_HELP}")
            headers["X-Vault-Token"] = self._token
        if self.settings.namespace:
            headers["X-Vault-Namespace"] = self.settings.namespace
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, content = self._transport(method, f"{self.settings.address}/v1/{path}", headers, data, 30.0)
        if status in (200, 204):
            if raw:
                return content.decode("utf-8", "replace")
            return json.loads(content) if content.strip() else {}
        try:
            errors = [str(item) for item in json.loads(content).get("errors", [])]
        except (ValueError, AttributeError):
            errors = []
        if status == 404:
            if missing_ok:
                try:
                    return {"missing": json.loads(content)} if content.strip() else None
                except ValueError:
                    return None
            raise VaultError(f"Vault has nothing at {path} ({operation})")
        if status == 403:
            if denied_ok:
                return None
            raise VaultError(f"Vault denied {operation} on {path}; the credential was rejected or lacks access")
        if status == 400 and any("check-and-set" in item for item in errors):
            raise VaultConflict(f"{path} changed in Vault during {operation}; nothing was overwritten, retry")
        if status == 503:
            raise VaultError(f"Vault is sealed or unavailable ({operation} on {path})")
        detail = f": {'; '.join(errors)}" if errors else ""
        raise VaultError(f"Vault could not {operation} on {path} (HTTP {status}){detail}")

    # ---- status and identity -------------------------------------------------------------

    def health(self) -> dict:
        return self.call(
            "GET", "sys/health?standbyok=true&perfstandbyok=true&sealedcode=200&uninitcode=200&drsecondarycode=200",
            operation="read health", authenticated=False,
        )

    def lookup_self(self) -> dict:
        return self.call("GET", "auth/token/lookup-self", operation="look up the credential")["data"]

    def mount_type(self, mount: str) -> tuple[str, dict] | None:
        """Type and options of a mount, or `None` when it is absent or not visible to this credential."""
        result = self.call(
            "GET", f"sys/internal/ui/mounts/{mount}", operation="read mount", missing_ok=True, denied_ok=True,
        )
        if not result or "data" not in result:
            return None
        data = result["data"]
        return str(data.get("type")), dict(data.get("options") or {})

    def pki_role(self, role: str) -> dict | None:
        result = self.call(
            "GET", f"{self.settings.pki_mount}/roles/{role}", operation="read PKI role",
            missing_ok=True, denied_ok=True,
        )
        return result["data"] if result and "data" in result else None

    # ---- key-value version 2 ---------------------------------------------------------------

    def kv_read(self, path: str) -> tuple[dict[str, str], int]:
        """Keys at `path` and the version to pass as check-and-set; `({}, 0)` when the path is new."""
        result = self.call("GET", f"{self.settings.kv_mount}/data/{path}", operation="read secret", missing_ok=True)
        if result is None:
            return {}, 0
        if "missing" in result:
            # A deleted latest version answers 404 but still counts for check-and-set.
            metadata = ((result["missing"] or {}).get("data") or {}).get("metadata") or {}
            return {}, int(metadata.get("version", 0))
        data = result["data"]
        return dict(data.get("data") or {}), int(data["metadata"]["version"])

    def kv_write(self, path: str, data: dict[str, str], cas: int) -> None:
        self.call(
            "POST", f"{self.settings.kv_mount}/data/{path}", {"options": {"cas": cas}, "data": data},
            operation="write secret",
        )

    # ---- PKI ---------------------------------------------------------------------------------

    def pki_sign(self, role: str, csr: str, common_name: str, ttl: str) -> dict:
        return self.call(
            "POST", f"{self.settings.pki_mount}/sign/{role}",
            # The request's own alternative names are authoritative; the subject name is not added to them.
            {"csr": csr, "common_name": common_name, "ttl": ttl, "format": "pem", "exclude_cn_from_sans": True},
            operation="sign certificate",
        )["data"]

    def pki_revoke(self, serial_number: str) -> None:
        self.call(
            "POST", f"{self.settings.pki_mount}/revoke", {"serial_number": serial_number},
            operation="revoke certificate",
        )

    def pki_ca_pem(self) -> str:
        """The authority's public certificate; empty when the mount has none."""
        pem = self.call(
            "GET", f"{self.settings.pki_mount}/ca/pem", operation="read CA certificate",
            missing_ok=True, raw=True, authenticated=False,
        )
        return pem.strip() + "\n" if isinstance(pem, str) and "BEGIN CERTIFICATE" in pem else ""


def resolve_token(
    settings: Vault, auth: Auth, environ: Mapping[str, str] | None = None, transport: Transport | None = None,
) -> str:
    """Environment token, else a token file, else an AppRole login. Nothing is written or printed."""
    environ = os.environ if environ is None else environ
    token = environ.get("VAULT_TOKEN", "").strip()
    if token:
        return token
    if auth.token_file is not None:
        return _read_credential_file(auth.token_file)
    if auth.role_id_file is not None or auth.secret_id_file is not None:
        if auth.role_id_file is None or auth.secret_id_file is None:
            raise VaultError("an AppRole login needs both --vault-role-id-file and --vault-secret-id-file")
        body = {"role_id": _read_credential_file(auth.role_id_file), "secret_id": _read_credential_file(auth.secret_id_file)}
        result = Client(settings, None, transport).call(
            "POST", f"auth/{APPROLE_MOUNT}/login", body, operation="log in with AppRole", authenticated=False,
        )
        return str(result["auth"]["client_token"])
    raise VaultError(f"no Vault credential: {CREDENTIAL_HELP}")


def connect(
    platform: Platform, auth: Auth = Auth(), environ: Mapping[str, str] | None = None,
    transport: Transport | None = None,
) -> Client:
    """An authenticated client. A production document refuses a credential carrying the root policy."""
    client = Client(platform.vault, resolve_token(platform.vault, auth, environ, transport), transport)
    if platform.profile == "production" and "root" in client.lookup_self().get("policies", []):
        raise VaultError("profile: production refuses a Vault credential that carries the root policy")
    return client


# ---- what the platform needs from Vault ---------------------------------------------------------


def server_hostnames(platform: Platform) -> list[str]:
    names = {platform.gateway.hostname, platform.grafana.hostname}
    if platform.storage_provider == "seaweedfs":
        names |= {urlsplit(binding.endpoint).hostname or "" for binding in platform.bindings.values()}
    return sorted(names)


def pki_roles(platform: Platform) -> dict[str, dict]:
    """The two PKI roles, limited to the hostnames and collector identities the document declares."""
    common = {
        "allow_any_name": False, "allow_ip_sans": False, "allow_localhost": False, "allow_subdomains": False,
        "allow_glob_domains": False, "allow_wildcard_certificates": False, "code_signing_flag": False,
        "email_protection_flag": False, "key_type": "ec", "key_bits": 256, "key_usage": ["DigitalSignature"],
        "max_ttl": MAX_CERTIFICATE_TTL, "use_csr_common_name": True, "use_csr_sans": True,
    }
    identities = sorted(item.certificate_identity for item in platform.credentials if item.certificate_identity)
    return {
        platform.vault.server_role: {
            **common, "allowed_domains": server_hostnames(platform), "allow_bare_domains": True,
            "enforce_hostnames": True, "allowed_uri_sans": [], "server_flag": True, "client_flag": False,
        },
        platform.vault.client_role: {
            **common, "allowed_domains": [], "allow_bare_domains": False, "enforce_hostnames": False,
            # The subject name of a collector certificate is a label; its URI name is the identity.
            "cn_validations": ["disabled"], "allowed_uri_sans": identities,
            "server_flag": False, "client_flag": True,
        },
    }


def policy_paths(platform: Platform) -> dict[str, list[str]]:
    vault = platform.vault
    paths = {
        f"{vault.kv_mount}/data/{path}": ["create", "read", "update"]
        for path in sorted({reference.path for reference in platform.secrets.values()})
    }
    for role in (vault.server_role, vault.client_role):
        paths[f"{vault.pki_mount}/sign/{role}"] = ["create", "update"]
        paths[f"{vault.pki_mount}/roles/{role}"] = ["read"]
    paths[f"{vault.pki_mount}/revoke"] = ["create", "update"]
    # Self-inspection only; lets the prerequisite check work for a token without the default policy.
    paths["auth/token/lookup-self"] = ["read"]
    return paths


def policy_hcl(platform: Platform) -> str:
    lines = ["# Rendered from the platform document; apply it as a Vault ACL policy. Do not edit."]
    for path, capabilities in sorted(policy_paths(platform).items()):
        lines += ["", f'path "{path}" {{', f"  capabilities = {json.dumps(capabilities)}", "}"]
    return "\n".join(lines) + "\n"


def _hcl(paths: dict[str, list[str]], title: str) -> str:
    lines = [f"# {title} Rendered from the platform document; do not edit."]
    for path, capabilities in sorted(paths.items()):
        lines += ["", f'path "{path}" {{', f"  capabilities = {json.dumps(capabilities)}", "}"]
    return "\n".join(lines) + "\n"


# How long a cluster identity's Vault token lives before the operator logs in again.
CLUSTER_TOKEN_TTL = "1h"


def cluster_access(
    platform: Platform, workloads: list, namespace: str, issuer: str, issuer_namespaces: tuple[str, ...] = (),
) -> dict:
    """What Vault must allow so a cluster can read its workloads' secrets and have certificates signed.

    One role and one policy per workload identity. A workload's policy reads exactly the paths
    of the secrets it uses; the issuer's policy signs with the two PKI roles. Nothing can write
    a secret, and nothing here is a secret.
    """
    vault = platform.vault
    if vault.kubernetes_auth_mount is None:
        raise ConfigurationError("vault: kubernetes_auth.mount is not declared")
    policies = {
        workload.vault_role: _hcl(
            {f"{vault.kv_mount}/data/{platform.secrets[ref].path}": ["read"] for ref in workload.secrets},
            f"Read-only access for the {workload.name} workload.",
        )
        for workload in workloads if workload.secrets
    }
    issuer_role = f"nighthawk-{issuer}"
    policies[issuer_role] = _hcl(
        {f"{vault.pki_mount}/sign/{role}": ["create", "update"] for role in (vault.server_role, vault.client_role)},
        "Certificate signing for the cluster's certificate manager.",
    )
    accounts = {workload.vault_role: workload.service_account for workload in workloads if workload.secrets}
    accounts[issuer_role] = issuer
    return {
        "mount": vault.kubernetes_auth_mount,
        "policies": policies,
        "roles": {
            role: {
                "bound_service_account_names": [account],
                "bound_service_account_namespaces": (
                    sorted({namespace, *issuer_namespaces}) if role == issuer_role else [namespace]
                ),
                "token_policies": [role],
                "token_ttl": CLUSTER_TOKEN_TTL,
                "token_max_ttl": CLUSTER_TOKEN_TTL,
            }
            for role, account in sorted(accounts.items())
        },
    }


def access_requirements(platform: Platform, cluster: dict | None = None) -> dict[str, str]:
    """Non-secret, deterministic files an operator applies to their own Vault."""
    files = {
        "vault/policy.hcl": policy_hcl(platform),
        "vault/pki-roles.json": json.dumps(pki_roles(platform), indent=2, sort_keys=True) + "\n",
    }
    if cluster is not None:
        files["vault/kubernetes-auth.json"] = json.dumps(cluster, indent=2, sort_keys=True) + "\n"
    return files


def _role_matches(existing: dict, wanted: dict) -> bool:
    return (
        sorted(existing.get("bound_service_account_names") or []) == wanted["bound_service_account_names"]
        and sorted(existing.get("bound_service_account_namespaces") or []) == wanted["bound_service_account_namespaces"]
        and sorted(existing.get("token_policies") or []) == wanted["token_policies"]
    )


def cluster_access_problems(client: Client, cluster: dict) -> list[str]:
    """Where a Vault differs from the rendered cluster requirements: missing, or allowing something else."""
    mount, problems = cluster["mount"], []
    auth = (client.call("GET", "sys/auth", operation="list authentication mounts").get("data") or {}).get(f"{mount}/")
    if auth is None:
        return [f"authentication mount {mount} does not exist; enable Kubernetes authentication there"]
    if auth.get("type") != "kubernetes":
        return [f"authentication mount {mount} is a {auth.get('type')} mount, not kubernetes"]
    for name, hcl in sorted(cluster["policies"].items()):
        current = client.call("GET", f"sys/policies/acl/{name}", operation="read policy", missing_ok=True)
        if not current or "data" not in current:
            problems.append(f"policy {name} does not exist; apply the rendered vault/kubernetes-auth.json")
        elif (current.get("data") or {}).get("policy") != hcl:
            problems.append(f"policy {name} differs from the rendered one; it may allow more than its workload needs")
    for name, wanted in sorted(cluster["roles"].items()):
        current = client.call("GET", f"auth/{mount}/role/{name}", operation="read role", missing_ok=True)
        if not current or "data" not in current:
            problems.append(f"role {name} does not exist at {mount}; the cluster cannot authenticate as it")
        elif not _role_matches(current.get("data") or {}, wanted):
            problems.append(f"role {name} is bound to other service accounts or policies than the rendered ones")
    return problems


def apply_cluster_access(client: Client, cluster: dict, kubernetes_host: str | None = None) -> list[str]:
    """Development only: make a disposable Vault match the rendered cluster requirements."""
    mount, created = cluster["mount"], []
    auths = client.call("GET", "sys/auth", operation="list authentication mounts").get("data") or {}
    existing = auths.get(f"{mount}/")
    if existing is None:
        client.call("POST", f"sys/auth/{mount}", {"type": "kubernetes"}, operation="enable authentication mount")
        created.append(f"authentication mount {mount}")
    elif existing.get("type") != "kubernetes":
        raise VaultError(f"{mount} already exists in Vault as a {existing.get('type')} authentication mount")
    if kubernetes_host is not None:
        # Vault validates service account tokens by asking this API server.
        current = client.call("GET", f"auth/{mount}/config", operation="read authentication configuration", missing_ok=True)
        if not current or (current.get("data") or {}).get("kubernetes_host") != kubernetes_host:
            client.call("POST", f"auth/{mount}/config", {"kubernetes_host": kubernetes_host}, operation="configure authentication")
            created.append(f"authentication configuration {mount}")
    for name, hcl in sorted(cluster["policies"].items()):
        current = client.call("GET", f"sys/policies/acl/{name}", operation="read policy", missing_ok=True)
        if not current or (current.get("data") or {}).get("policy") != hcl:
            client.call("PUT", f"sys/policies/acl/{name}", {"policy": hcl}, operation="write policy")
            created.append(f"policy {name}")
    for name, wanted in sorted(cluster["roles"].items()):
        current = client.call("GET", f"auth/{mount}/role/{name}", operation="read role", missing_ok=True)
        if not current or "data" not in current or not _role_matches(current["data"] or {}, wanted):
            client.call("POST", f"auth/{mount}/role/{name}", wanted, operation="write role")
            created.append(f"role {name}")
    return created


def bootstrap_dev(
    platform: Platform, client: Client, *, confirmed: bool, ca_valid_days: int, policy_name: str = POLICY_NAME,
) -> list[str]:
    """Configure a disposable Vault with what `access_requirements` describes. Returns what was created."""
    if platform.profile == "production":
        raise VaultError("bootstrap-dev-vault refuses a production platform document; apply the rendered policy and roles")
    if not confirmed:
        raise VaultError("bootstrap-dev-vault changes Vault's mounts and policies; confirm that this Vault is disposable")
    if isinstance(ca_valid_days, bool) or not isinstance(ca_valid_days, int) or ca_valid_days < 1:
        raise VaultError("an explicit authority validity of at least one day is required")
    vault, created = platform.vault, []
    mounts = client.call("GET", "sys/mounts", operation="list mounts").get("data") or {}
    wanted = {
        vault.kv_mount: {"type": "kv", "options": {"version": "2"}},
        vault.pki_mount: {"type": "pki", "config": {"max_lease_ttl": f"{ca_valid_days * 24}h"}},
    }
    for mount, definition in wanted.items():
        existing = mounts.get(f"{mount}/")
        if existing is None:
            client.call("POST", f"sys/mounts/{mount}", definition, operation="enable mount")
            created.append(f"mount {mount}")
        elif existing.get("type") != definition["type"]:
            raise VaultError(f"{mount} already exists in Vault as a {existing.get('type')} mount")
    if not client.pki_ca_pem():
        client.call(
            "POST", f"{vault.pki_mount}/root/generate/internal",
            {"common_name": f"NightHawk development CA ({platform.gateway.hostname})",
             "ttl": f"{ca_valid_days * 24}h", "key_type": "ec", "key_bits": 256},
            operation="create the development authority",
        )
        created.append("certificate authority")
    for role, definition in pki_roles(platform).items():
        existing = client.pki_role(role)
        if existing is None or any(existing.get(key) != value for key, value in _comparable(definition).items()):
            client.call("POST", f"{vault.pki_mount}/roles/{role}", definition, operation="write PKI role")
            created.append(f"PKI role {role}")
    hcl = policy_hcl(platform)
    current = client.call("GET", f"sys/policies/acl/{policy_name}", operation="read policy", missing_ok=True)
    if not current or (current.get("data") or {}).get("policy") != hcl:
        client.call("PUT", f"sys/policies/acl/{policy_name}", {"policy": hcl}, operation="write policy")
        created.append(f"policy {policy_name}")
    return created


def _comparable(definition: dict) -> dict:
    """A role as Vault reports it back: durations become seconds."""
    comparable = dict(definition)
    comparable["max_ttl"] = int(str(definition["max_ttl"]).rstrip("h")) * 3600
    return comparable
