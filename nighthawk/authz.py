"""Tenant gateway forward-auth policy: a pure decision function and a thin HTTP adapter.

The service trusts the forwarded client-certificate header, so it must be reachable
only from the gateway proxy. Every failure path denies.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import signal
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from nighthawk.config import ConfigurationError, SCOPE_EXPOSURE


SIGNALS = ("metrics", "logs", "traces", "profiles")
PERMISSIONS = ("ingest", "query")
TENANT_HEADER = "X-Scope-OrgID"
# Header the pinned Traefik passTLSClientCert middleware sets from the verified TLS peer certificate.
CERTIFICATE_HEADER = "X-Forwarded-Tls-Client-Cert"
_DUMMY_DIGEST = hashlib.sha256(b"nighthawk-unknown-credential").hexdigest()


@dataclass(frozen=True)
class PolicyCredential:
    secret_sha256: str
    backend_id: str
    permission: str
    signals: frozenset[str]
    certificate_identity: str | None


@dataclass(frozen=True)
class Policy:
    entry_points: dict[str, str]
    revoked_certificate_fingerprints: frozenset[str]
    credentials: dict[str, PolicyCredential]


@dataclass(frozen=True)
class RequestFacts:
    """What the proxy reports about one request. Header fields hold every value received."""

    entry: str
    signal: str
    permission: str
    authorization: tuple[str, ...] = ()
    tenant_headers: tuple[str, ...] = ()
    client_certificates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    status: int
    backend_id: str | None
    reason: str

    @property
    def allowed(self) -> bool:
        return self.status == 200


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def parse_policy(document: object) -> Policy:
    """Validate a policy bundle strictly; anything unexpected is rejected rather than ignored."""
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "entry_points", "revoked_certificate_fingerprints", "credentials",
    } or document["schema_version"] != 1:
        raise ConfigurationError("policy: unsupported or malformed bundle")
    entry_points = document["entry_points"]
    if not isinstance(entry_points, dict) or not entry_points or any(
        not isinstance(name, str) or scope not in SCOPE_EXPOSURE for name, scope in entry_points.items()
    ):
        raise ConfigurationError("policy: malformed entry points")
    revoked = document["revoked_certificate_fingerprints"]
    if not isinstance(revoked, list) or not all(_is_hex_digest(item) for item in revoked):
        raise ConfigurationError("policy: malformed revoked certificate fingerprints")
    raw_credentials = document["credentials"]
    if not isinstance(raw_credentials, dict) or not raw_credentials:
        raise ConfigurationError("policy: no credentials")
    credentials: dict[str, PolicyCredential] = {}
    for credential_id, raw in raw_credentials.items():
        if not isinstance(raw, dict) or set(raw) != {
            "secret_sha256", "backend_id", "permission", "signals", "certificate_identity",
        }:
            raise ConfigurationError(f"policy: malformed credential {credential_id!r}")
        signals = raw["signals"]
        identity = raw["certificate_identity"]
        if (
            not isinstance(credential_id, str) or not credential_id or ":" in credential_id
            or not _is_hex_digest(raw["secret_sha256"])
            or not isinstance(raw["backend_id"], str) or not raw["backend_id"]
            or raw["permission"] not in PERMISSIONS
            or not isinstance(signals, list) or not signals or not set(signals) <= set(SIGNALS)
            or not (identity is None or (isinstance(identity, str) and identity))
            or (identity is not None and raw["permission"] != "ingest")
        ):
            raise ConfigurationError(f"policy: malformed credential {credential_id!r}")
        credentials[credential_id] = PolicyCredential(
            raw["secret_sha256"], raw["backend_id"], raw["permission"], frozenset(signals), identity,
        )
    return Policy(dict(entry_points), frozenset(revoked), credentials)


def load_policy(path: Path) -> Policy:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ConfigurationError(f"policy: cannot load {path}: {error}") from error
    return parse_policy(document)


def secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _basic_credentials(values: tuple[str, ...]) -> tuple[str, str] | None:
    if len(values) != 1:
        return None
    scheme, _, encoded = values[0].partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator or not username:
        return None
    return username, password


def certificate_from_header(value: str) -> x509.Certificate:
    """Decode the proxy's forwarded certificate: base64 DER without PEM armor, leaf first, comma-joined chain."""
    return x509.load_der_x509_certificate(base64.b64decode(value.split(",")[0], validate=True))


def certificate_fingerprint(certificate: x509.Certificate) -> str:
    return certificate.fingerprint(hashes.SHA256()).hex()


def certificate_uri_identities(certificate: x509.Certificate) -> list[str]:
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return names.get_values_for_type(x509.UniformResourceIdentifier)


def decide(policy: Policy, facts: RequestFacts) -> Decision:
    """Bind one request to its credential's backend tenant, or deny it."""
    scope = policy.entry_points.get(facts.entry)
    if scope is None or facts.signal not in SIGNALS or facts.permission not in PERMISSIONS:
        return Decision(403, None, "unknown route class")
    basic = _basic_credentials(facts.authorization)
    if basic is None:
        return Decision(401, None, "missing or malformed credential")
    credential = policy.credentials.get(basic[0])
    # Compare against a dummy digest for unknown identifiers so both failures take the same path.
    expected = credential.secret_sha256 if credential is not None else _DUMMY_DIGEST
    matches = hmac.compare_digest(secret_digest(basic[1]), expected)
    if credential is None or not matches:
        return Decision(401, None, "invalid credential")
    if credential.permission != facts.permission:
        return Decision(403, None, "permission does not allow this route")
    if facts.signal not in credential.signals:
        return Decision(403, None, "signal is not enabled for this datastream")
    if credential.certificate_identity is None:
        if facts.permission == "ingest" and scope == "restricted-external":
            return Decision(403, None, "external ingestion requires a certificate-bound credential")
    else:
        if len(facts.client_certificates) != 1:
            return Decision(403, None, "client certificate required")
        try:
            certificate = certificate_from_header(facts.client_certificates[0])
        except (ValueError, binascii.Error):
            return Decision(403, None, "unreadable client certificate")
        if certificate_fingerprint(certificate) in policy.revoked_certificate_fingerprints:
            return Decision(403, None, "client certificate is revoked")
        if certificate_uri_identities(certificate) != [credential.certificate_identity]:
            return Decision(403, None, "client certificate identity does not match the credential")
    if facts.tenant_headers and facts.tenant_headers != (credential.backend_id,):
        return Decision(403, None, "tenant header does not match the credential")
    return Decision(200, credential.backend_id, "allowed")


class AuthService:
    """Holds the current policy; a failed reload keeps the last valid one."""

    def __init__(self, policy_path: Path) -> None:
        self.policy_path = policy_path
        self._lock = threading.Lock()
        self._policy = load_policy(policy_path)
        self._reload_failed = False
        self._decisions = {200: 0, 401: 0, 403: 0}

    def reload(self) -> bool:
        try:
            policy = load_policy(self.policy_path)
        except ConfigurationError as error:
            with self._lock:
                self._reload_failed = True
            print(f"error: policy reload failed, keeping previous policy: {error}", file=sys.stderr)
            return False
        with self._lock:
            self._policy, self._reload_failed = policy, False
        return True

    def verify(self, facts: RequestFacts) -> Decision:
        with self._lock:
            policy = self._policy
        decision = decide(policy, facts)
        with self._lock:
            self._decisions[decision.status] += 1
        return decision

    def health(self) -> dict:
        with self._lock:
            return {
                "status": "degraded" if self._reload_failed else "ok",
                "policy_reload_failed": self._reload_failed,
                "credentials": len(self._policy.credentials),
            }

    def metrics(self) -> str:
        with self._lock:
            decisions, failed, count = dict(self._decisions), self._reload_failed, len(self._policy.credentials)
        lines = [
            "# TYPE nighthawk_authz_decisions_total counter",
            *(f'nighthawk_authz_decisions_total{{status="{status}"}} {total}' for status, total in sorted(decisions.items())),
            "# TYPE nighthawk_authz_policy_reload_failed gauge",
            f"nighthawk_authz_policy_reload_failed {int(failed)}",
            "# TYPE nighthawk_authz_policy_credentials gauge",
            f"nighthawk_authz_policy_credentials {count}",
        ]
        return "\n".join(lines) + "\n"


def _handler(service: AuthService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _handle(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/healthz":
                self._send(200, json.dumps(service.health(), sort_keys=True) + "\n")
            elif url.path == "/metrics":
                self._send(200, service.metrics())
            elif url.path == "/verify":
                query = parse_qs(url.query)

                def single(name: str) -> str:
                    values = query.get(name, [])
                    return values[0] if len(values) == 1 else ""

                decision = service.verify(RequestFacts(
                    single("entry"), single("signal"), single("permission"),
                    tuple(self.headers.get_all("Authorization", [])),
                    # A header repeated or folded into a list is never a single tenant.
                    tuple(self.headers.get_all(TENANT_HEADER, [])),
                    tuple(self.headers.get_all(CERTIFICATE_HEADER, [])),
                ))
                if decision.allowed:
                    self._send(200, "ok\n", {TENANT_HEADER: decision.backend_id or ""})
                elif decision.status == 401:
                    self._send(401, "authentication required\n", {"WWW-Authenticate": 'Basic realm="nighthawk"'})
                else:
                    self._send(403, "forbidden\n")
            else:
                self._send(404, "not found\n")

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _handle

        def log_message(self, format: str, *args: object) -> None:
            # Request lines would repeat tenant routing on every ingest call; decisions are counted instead.
            return

    return Handler


def make_server(service: AuthService, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _handler(service))
    server.daemon_threads = True
    return server


def serve(policy_path: Path, listen: str) -> int:
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise ConfigurationError(f"--listen must be host:port, not {listen!r}")
    service = AuthService(policy_path)
    server = make_server(service, host, int(port))
    signal.signal(signal.SIGHUP, lambda *_: service.reload())
    print(f"Serving tenant gateway policy on {listen} with {service.health()['credentials']} credential(s).", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
