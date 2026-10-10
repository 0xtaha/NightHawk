"""One-command local bring-up of the Docker Compose stack, and its teardown.

Secrets live in the Vault the platform document declares, which must already be running:
the quickstart never starts, configures, or stores a credential for it. Nothing is replaced:
secrets, identities, and certificates are created only when missing. Rendered files are
synchronized into stable directories file by file, because running containers keep those
directories mounted.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import ipaddress
import json
import os
import secrets as token_source
import socket
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable, Mapping


from nighthawk import collector, gateway, grafana, storage, trust
from nighthawk.config import ROOT, ConfigurationError, Platform, load_platform, load_versions
from nighthawk.secrets import doctor, materialize, materialized_path, secret_exists, store_secret
from nighthawk.vault import Auth, Client, Transport

Runner = Callable[..., subprocess.CompletedProcess]

COMPOSE_FILE = ROOT / "docker-compose" / "docker-compose.yaml"
# Publishes the certificate-requiring external entry point. Never part of the local quickstart.
EXTERNAL_OVERRIDE = ROOT / "docker-compose" / "docker-compose.external.yaml"
# Long-running services whose readiness the bring-up waits for; their dependencies follow.
WAITED_SERVICES = ("grafana", "alloy")
DEPLOYMENT_MANIFEST = "deployment.json"
# What the host builds the platform's own image from and runs Compose with: the build context
# .dockerignore allows, plus the Compose directory.
SOURCE_ARCHIVE = "source.tar"
SOURCE_PATHS = ("requirements.txt", "config", "nighthawk", "docker-compose")
SINGLE_NODE_NOTICE = (
    "This is a single node without high availability: if that machine is lost, so is its telemetry."
)
HOST_COLLECTION_PROFILE, HOST_COLLECTION_SERVICE = "host-collection", "alloy-host"
RENEW_BEFORE = datetime.timedelta(days=7)


@dataclass
class Options:
    config: Path
    root: Path = ROOT
    compose_file: Path = COMPOSE_FILE
    container: str = "docker"
    compose: tuple[str, ...] = ("docker", "compose")
    rendered_dir: Path = ROOT / ".generated" / "docker"
    secrets_dir: Path = ROOT / ".materialized-secrets"
    vault_auth: Auth = Auth()
    # Injection points for tests; the defaults are the process environment and real HTTP.
    environ: Mapping[str, str] | None = None
    vault_transport: Transport | None = None
    tenant: str | None = None
    datastream: str | None = None
    collector_entry_point: str = "local-gateway"
    bind_address: str = "127.0.0.1"
    # Credential IDs to use where a datastream declares several of one permission.
    credentials: tuple[str, ...] = ()
    # Also run the host and container collector (Compose profile `host-collection`). Docker Engine only.
    host_collection: bool = False
    certificate_valid_days: int = 90
    timeout: int = 600
    build: bool = True
    project: str = "nighthawk"
    created: list[str] = field(default_factory=list)
    # Publishing the external entry point, for a deployment other machines reach. The address
    # is required for that; the published port defaults to the entry point's own port.
    external_bind_address: str | None = None
    external_published_port: int | None = None
    # Where the two trees will live on the host that runs the stack, and the account it runs
    # as there, when that is not this machine. They only change what compose.env says.
    remote_rendered_dir: Path | None = None
    remote_secrets_dir: Path | None = None
    remote_run_as: tuple[int, int] | None = None


VAULT_HELP = (
    "start a development Vault and run `python -m nighthawk bootstrap-dev-vault`, "
    "or supply a credential for your own; see docs/00-quickstart.md"
)


def _environment(options: Options) -> dict[str, str]:
    """The environment of every child process. The Vault credential is not passed on to Compose."""
    environment = dict(os.environ if options.environ is None else options.environ)
    environment.pop("VAULT_TOKEN", None)
    return environment


def _run(runner: Runner, args: list[str], what: str, **kwargs) -> subprocess.CompletedProcess:
    try:
        result = runner(args, capture_output=True, text=True, **kwargs)
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigurationError(f"{what}: {error}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ConfigurationError(f"{what} failed" + (f": {detail[-1]}" if detail else ""))
    return result


def _compose(options: Options) -> list[str]:
    files = ["--file", str(options.compose_file)]
    if options.external_bind_address is not None:
        files += ["--file", str(EXTERNAL_OVERRIDE)]
    return [
        *options.compose, "--project-name", options.project, *files,
        "--env-file", str(options.rendered_dir / "compose.env"),
    ]


def external_entry_point(platform: Platform):
    """The certificate-requiring entry point a deployment publishes for other machines, if selected."""
    external = [entry for entry in platform.gateway.entry_points if entry.scope == "restricted-external"]
    return external[0] if len(external) == 1 else None


def vault_client(options: Options, platform: Platform) -> tuple[Client | None, list[str]]:
    """The Vault prerequisite: a client, or what is wrong."""
    checks, client = doctor(load_versions(), platform, options.vault_auth, options.environ, options.vault_transport)
    problems = [check.detail for check in checks if not check.ok]
    return client, ([f"{'; '.join(problems)}; {VAULT_HELP}"] if problems else [])


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def local_entry_point(platform: Platform):
    """The loopback-scoped entry point the Compose stack publishes."""
    local = [entry for entry in platform.gateway.entry_points if entry.scope == "loopback"]
    if len(local) != 1:
        raise ConfigurationError(
            "the quickstart publishes exactly one loopback-scoped gateway entry point; "
            f"the platform document selects {len(local)}"
        )
    return local[0]


def _port_is_free(address: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((address, port))
        except OSError:
            return False
    return True


def _stack_is_running(options: Options, runner: Runner) -> bool:
    if not (options.rendered_dir / "compose.env").exists():
        return False
    result = runner([*_compose(options), "ps", "--quiet", "traefik"], capture_output=True, text=True)
    return result.returncode == 0 and bool((result.stdout or "").strip())


def preflight(
    options: Options, platform: Platform, runner: Runner,
    port_is_free: Callable[[str, int], bool] = _port_is_free,
) -> tuple[tuple[int, int], Client]:
    """Check every prerequisite before anything is written; return the UID and GID services run as, and a Vault client."""
    if not _is_loopback(options.bind_address):
        # The published entry point is scoped `loopback` in the gateway policy, which admits
        # ingestion without a client certificate. It must not be reachable from another machine.
        raise ConfigurationError(
            f"--bind-address {options.bind_address} is not a loopback address. The quickstart publishes the "
            "local gateway entry point, which accepts ingestion without a client certificate; "
            "publishing it beyond this machine is not supported."
        )
    problems: list[str] = []
    for args, name in (([*options.compose, "version"], "the Compose command"), ([options.container, "info"], "the container runtime")):
        try:
            if runner(args, capture_output=True, text=True).returncode != 0:
                problems.append(f"{name} ({' '.join(args)}) is not usable")
        except (OSError, subprocess.SubprocessError):
            problems.append(f"{name} ({' '.join(args)}) is not installed")
    client, vault_problems = vault_client(options, platform)
    problems += vault_problems
    run_as = (os.getuid(), os.getgid())
    if not problems:
        components = runner(
            [options.container, "version", "--format", "{{range .Server.Components}}{{.Name}} {{end}}"],
            capture_output=True, text=True,
        )
        security = runner(
            [options.container, "info", "--format", "{{.SecurityOptions}}"], capture_output=True, text=True,
        )
        if "Podman" in (components.stdout or "") and "rootless" in (security.stdout or ""):
            # Rootless Podman maps container root to the invoking user, who owns the secret files.
            run_as = (0, 0)
            if options.host_collection:
                problems.append(
                    "--host-collection needs Docker Engine: it reads the Docker socket and the host's "
                    "filesystems as root, which rootless Podman does not provide"
                )
        if not _stack_is_running(options, runner):
            port = local_entry_point(platform).port
            if not port_is_free(options.bind_address, port):
                problems.append(f"port {options.bind_address}:{port} is already in use")
    if problems or client is None:
        raise ConfigurationError("prerequisites not met:\n  - " + "\n  - ".join(problems))
    return run_as, client


def _project_volumes(options: Options, runner: Runner) -> list[str]:
    result = runner([options.container, "volume", "ls", "--format", "{{.Name}}"], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return sorted(name for name in (result.stdout or "").split() if name.startswith(f"{options.project}_"))


def ensure_secrets(
    options: Options, platform: Platform, client: Client, runner: Runner | None, out: Callable[[str], None],
) -> None:
    """Create each referenced secret that does not exist yet in Vault. Existing values are never touched.

    `runner` reaches the container runtime that holds the stack's volumes. It is None when the
    stack runs on another machine, whose volumes cannot be looked at from here.
    """
    def exists(reference: str) -> bool:
        return secret_exists(client, platform.secrets[reference])

    identities = [reference for reference in trust.storage_identity_refs(platform) if not exists(reference)]
    volumes = _project_volumes(options, runner) if identities and runner is not None else []
    if volumes:
        # New identities could not read what the lost ones wrote, so nothing is generated.
        raise ConfigurationError(
            f"Vault holds no value for the storage identities ({', '.join(identities)}) that protect the data in "
            f"existing volumes ({', '.join(volumes)}). A development Vault loses everything when it restarts. "
            "Clear the volumes with `python -m nighthawk teardown-docker --purge --yes`, then run the quickstart again."
        )
    for credential in platform.credentials:
        if not exists(credential.secret_ref):
            trust.generate_credential(platform, credential.id, client)
            options.created.append(f"credential {credential.id}")
    for reference in identities:
        trust.generate_storage_identity(platform, reference, client)
        options.created.append(f"storage identity {reference}")
    admin = platform.grafana.admin_secret_ref
    if not exists(admin):
        password = token_source.token_urlsafe(24)
        store_secret(client, platform, admin, password)
        options.created.append("Grafana admin password")
        out(f"Grafana admin password (shown once): {password}")
    missing = sorted(reference for reference in platform.secrets if not exists(reference))
    if missing:
        raise ConfigurationError(
            f"the quickstart cannot create these secrets; store them with store-secret: {', '.join(missing)}"
        )


def _write(path: Path, content: str | bytes, mode: int) -> bool:
    """Replace `path` atomically if its content differs; report whether it changed."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    if path.exists() and path.read_bytes() == data:
        path.chmod(mode)
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if mode == 0o600 else 0o755)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    return True


def _sync(files: dict[str, str | bytes], target: Path, mode: int) -> bool:
    """Make `target` hold exactly `files`, touching only what differs."""
    changed = False
    for name, content in files.items():
        changed |= _write(target / name, content, mode)
    if target.exists():
        wanted = {target / name for name in files}
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_file() and path not in wanted and path.suffix != ".tmp":
                path.unlink()
                changed = True
    return changed


def _ensure_certificate(
    options: Options, platform: Platform, client: Client, directory: Path, stem: str, **what,
) -> None:
    # A validity shorter than the usual renewal period still has to outlive one run.
    renew_before = min(RENEW_BEFORE.days, options.certificate_valid_days - 1)
    if trust.ensure_certificate(platform, directory, options.certificate_valid_days, client, renew_before, **what):
        options.created.append(f"certificate {stem}")


def select_credentials(options: Options, platform: Platform) -> tuple[str | None, tuple[str, ...]]:
    """Split the named credentials into the collector's ingestion credential and Grafana's query credentials.

    Raises when a name is not declared, when an ingestion credential belongs to another
    datastream than the collector's, or when an overlap is left without a choice.
    """
    stream = _select_stream(options, platform)
    declared = {item.id: item for item in platform.credentials}
    ingest: list[str] = []
    query: list[str] = []
    for credential_id in options.credentials:
        credential = declared.get(credential_id)
        if credential is None:
            raise ConfigurationError(f"--credential {credential_id}: the platform document declares no such credential")
        if credential.permission == "ingest":
            if (credential.tenant, credential.datastream) != (stream.tenant, stream.datastream):
                raise ConfigurationError(
                    f"--credential {credential_id}: not an ingestion credential of the collector's datastream "
                    f"{stream.tenant}/{stream.datastream}"
                )
            ingest.append(credential_id)
        else:
            query.append(credential_id)
    if len(set(ingest)) > 1:
        raise ConfigurationError(f"--credential: both {' and '.join(sorted(set(ingest)))} were named for the collector; choose one")
    chosen = ingest[0] if ingest else None
    # Both raise, listing the candidates, when an overlap has no choice.
    collector._select_credential(platform, stream, collector.PROFILES["docker"], chosen)
    grafana.query_credentials(platform, query)
    return chosen, tuple(query)


def _digests(paths: list[Path]) -> dict[Path, str | None]:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths}


def _select_stream(options: Options, platform: Platform):
    if options.tenant is None and options.datastream is None:
        return platform.streams[0]
    for stream in platform.streams:
        if (stream.tenant, stream.datastream) == (options.tenant, options.datastream):
            return stream
    raise ConfigurationError(f"unknown tenant/datastream {options.tenant}/{options.datastream}")


def render(
    options: Options, platform: Platform, run_as: tuple[int, int], client: Client,
    out: Callable[[str], None] = print,
) -> dict[str, bool]:
    """Materialize secrets and write every rendered and runtime file; report which parts changed."""
    # Imported here: __main__ imports this module.
    from nighthawk.__main__ import main

    ingest_choice, query_choice = select_credentials(options, platform)
    query_files = [
        materialized_path(options.secrets_dir, platform.secrets[credential.secret_ref])
        for credential in grafana.query_credentials(platform, query_choice).values()
    ]
    before = _digests(query_files)
    materialize(platform, client, options.secrets_dir)
    after = _digests(query_files)
    # A value that existed and now differs was rotated in place; Grafana cannot detect that itself.
    rotated = any(before[path] is not None and before[path] != after[path] for path in query_files)
    runtime = options.secrets_dir / "runtime"

    def read(reference: str) -> str:
        return materialized_path(options.secrets_dir, platform.secrets[reference]).read_text(encoding="utf-8").rstrip("\r\n")

    for name in ("gateway", "storage", "collector-certificates"):
        (runtime / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    # The authority is the one in Vault's PKI mount; only its public certificate is ever read.
    ca_certificate = trust.authority_certificate(client)
    previous = runtime / "gateway" / "client-ca.pem"
    if previous.exists() and previous.read_text(encoding="utf-8") != ca_certificate:
        out(
            "The certificate authority in Vault changed. Certificates the quickstart manages are issued again; "
            "any certificate issued separately, such as a remote collector's, must be issued again too."
        )
    _ensure_certificate(options, platform, client, runtime / "gateway", "gateway-server", server=True)
    if platform.storage_provider == "seaweedfs":
        _ensure_certificate(options, platform, client, runtime / "storage", "storage-server", storage=True)
    stream = _select_stream(options, platform)
    credential = collector._select_credential(platform, stream, collector.PROFILES["docker"], ingest_choice)
    if credential.certificate_identity is not None:
        _ensure_certificate(
            options, platform, client, runtime / "collector-certificates", credential.id,
            credential_id=credential.id,
        )

    with tempfile.TemporaryDirectory() as temporary:
        contracts = Path(temporary) / "contracts"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main([
                "render-contracts", "--config", str(options.config), "--output", str(contracts),
                *(argument for credential_id in query_choice for argument in ("--credential", credential_id)),
            ])
        if status != 0:
            raise ConfigurationError(errors.getvalue().strip() or "render-contracts failed")
        rendered = {
            str(path.relative_to(contracts)): path.read_bytes() for path in contracts.rglob("*") if path.is_file()
        }
    changed = {
        "contracts": _sync(rendered, options.rendered_dir / "contracts", 0o644),
        "overrides": _sync(
            {name: content for name, content in rendered.items() if name.endswith("-overrides.yaml")},
            options.rendered_dir / "overrides", 0o644,
        ),
        "platform": _sync({"platform.yaml": options.config.read_bytes()}, options.rendered_dir / "platform", 0o644),
        "collector": _sync(
            collector.render_collector(
                platform, stream.tenant, stream.datastream, "docker", entry_point=options.collector_entry_point,
                credential_id=credential.id, self_monitoring="metrics" in stream.signals, otlp_only=True,
            ),
            options.rendered_dir / "collector", 0o644,
        ),
    }

    changed["gateway"] = any([
        _write(runtime / "gateway" / "client-ca.pem", ca_certificate, 0o600),
        _write(runtime / "gateway" / "traefik-static.yaml", rendered["gateway/traefik-static.yaml"], 0o600),
        _write(runtime / "gateway" / "traefik-dynamic.yaml", rendered["gateway/traefik-dynamic.yaml"], 0o600),
    ])
    storage_trust = ""
    if platform.storage_provider == "seaweedfs":
        storage_cas = sorted({b.tls.ca_secret_ref for b in platform.bindings.values() if b.tls.ca_secret_ref})
        storage_trust = "".join(read(reference) + "\n" for reference in storage_cas)
        if any(binding.tls.trust == "pki" for binding in platform.bindings.values()):
            storage_trust = ca_certificate + storage_trust
        changed["storage"] = any([
            _write(runtime / "storage" / "s3.json", json.dumps(storage.s3_identities(platform, read), indent=2, sort_keys=True) + "\n", 0o600),
            _write(runtime / "storage" / "buckets.txt", "".join(f"{bucket}\n" for bucket in storage.buckets(platform)), 0o600),
        ])
    _sync({"ca.pem": storage_trust} if storage_trust else {}, runtime / "storage-trust", 0o600)
    (runtime / "storage-trust").mkdir(parents=True, exist_ok=True, mode=0o700)
    changed["backends"] = _sync(
        {f"{name}.env": content for name, content in storage.backend_environment(platform, read).items()},
        runtime / "backends", 0o600,
    )
    policy = json.dumps(gateway.policy_bundle(platform, options.secrets_dir), indent=2, sort_keys=True) + "\n"
    changed["authz"] = _write(runtime / "authz" / "policy.json", policy, 0o600)
    changed["grafana"] = any([
        _write(runtime / "grafana" / "admin-password", read(platform.grafana.admin_secret_ref), 0o600),
        _write(runtime / "grafana" / "gateway-ca.pem", ca_certificate, 0o600),
    ])
    collector_files: dict[str, str | bytes] = {
        "credential": read(credential.secret_ref), "gateway-ca.pem": ca_certificate,
    }
    if credential.certificate_identity is not None:
        issued = runtime / "collector-certificates"
        collector_files["client.crt.pem"] = (issued / f"{credential.id}.crt.pem").read_bytes()
        collector_files["client.key.pem"] = (issued / f"{credential.id}.key.pem").read_bytes()
    changed["collector"] |= _sync(collector_files, runtime / "collector", 0o600)

    # The host collector gets the full docker profile; without the option nothing is left for it to mount.
    host_files = collector.render_collector(
        platform, stream.tenant, stream.datastream, "docker", entry_point=options.collector_entry_point,
        credential_id=credential.id,
    ) if options.host_collection else {}
    changed["collector-host"] = _sync(host_files, options.rendered_dir / "collector-host", 0o644)
    changed["query-secrets"] = rotated
    environment: dict[str, object] = {
        "COMPOSE_PROJECT_NAME": options.project,
        "NIGHTHAWK_RENDERED_DIR": options.remote_rendered_dir or options.rendered_dir.resolve(),
        "NIGHTHAWK_SECRETS_DIR": options.remote_secrets_dir or options.secrets_dir.resolve(),
        "NIGHTHAWK_UID": run_as[0],
        "NIGHTHAWK_GID": run_as[1],
        "NIGHTHAWK_BIND_ADDRESS": options.bind_address,
        "NIGHTHAWK_GATEWAY_PORT": local_entry_point(platform).port,
        "NIGHTHAWK_GATEWAY_HOSTNAME": platform.gateway.hostname,
        "NIGHTHAWK_GRAFANA_HOSTNAME": platform.grafana.hostname,
    }
    if options.external_bind_address is not None:
        external = external_entry_point(platform)
        if external is None:
            raise ConfigurationError(
                "publishing for other machines needs exactly one restricted-external gateway entry point "
                "selected in the platform document (the remote-gateway rule)"
            )
        published = options.external_published_port or external.port
        environment.update({
            "NIGHTHAWK_EXTERNAL_BIND_ADDRESS": options.external_bind_address,
            "NIGHTHAWK_EXTERNAL_PORT": external.port,
            "NIGHTHAWK_EXTERNAL_PUBLISHED_PORT": published,
            # Other machines reach the Grafana UI through the external entry point.
            "NIGHTHAWK_GRAFANA_PORT": published,
        })
    _write(options.rendered_dir / "compose.env", "".join(f"{key}={value}\n" for key, value in environment.items()), 0o644)
    return changed


def source_archive(root: Path = ROOT) -> bytes:
    """The same bytes for the same files: sorted, without owners or times, so a re-render rewrites nothing."""
    files: list[Path] = []
    for name in SOURCE_PATHS:
        path = root / name
        if not path.exists():
            raise ConfigurationError(f"{path}: needed to build the platform's image, not found")
        files += [path] if path.is_file() else [
            item for item in path.rglob("*") if item.is_file() and "__pycache__" not in item.parts
        ]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files):
            data = path.read_bytes()
            entry = tarfile.TarInfo(path.relative_to(root).as_posix())
            entry.size, entry.mtime = len(data), 0
            entry.mode = 0o755 if path.stat().st_mode & 0o100 else 0o644
            archive.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def deployment_manifest(options: Options, platform: Platform) -> dict:
    """What the host needs to know to start what was rendered, so no playbook repeats a service name."""
    _, query_choice = select_credentials(options, platform)
    reloads = [
        {"service": "authz", "rendered": [], "secrets": ["runtime/authz"]},
        {"service": "alloy", "rendered": ["collector"], "secrets": ["runtime/collector"]},
    ]
    if options.host_collection:
        reloads.append({"service": HOST_COLLECTION_SERVICE, "rendered": ["collector-host"], "secrets": ["runtime/collector"]})
    return {
        "project": options.project,
        "source_archive": SOURCE_ARCHIVE,
        "compose_files": [
            path.relative_to(ROOT).as_posix() for path in (ROOT / "docker-compose" / "docker-compose.yaml", EXTERNAL_OVERRIDE)
        ],
        "build_service": "authz",
        "profiles": [HOST_COLLECTION_PROFILE] if options.host_collection else [],
        "teardown_profiles": ["sample", "tools", HOST_COLLECTION_PROFILE],
        "waited_services": [*WAITED_SERVICES, *([HOST_COLLECTION_SERVICE] if options.host_collection else [])],
        "wait_service": "wait-alloy",
        "wait_timeout": options.timeout,
        # Services that do not watch their files, and the directories whose change they must be told about.
        "reload_signal": "HUP",
        "reloads": reloads,
        "provision": ["grafana-init", *(part for item in query_choice for part in ("--credential", item))],
        "notice": SINGLE_NODE_NOTICE,
    }


def render_deployment(options: Options, *, out: Callable[[str], None] = print) -> dict[str, bool]:
    """Everything the quickstart does before starting containers, for a stack that runs on another machine.

    Checks Vault, creates missing secrets there, obtains certificates, and writes the rendered
    and secret trees on this machine with a compose.env that names the remote paths. Nothing is
    started and the remote machine needs no access to Vault.
    """
    platform = load_platform(options.config)
    if platform.deployment != "docker":
        raise ConfigurationError(f"a Docker deployment needs a docker platform document, not {platform.deployment}")
    if platform.profile != "production":
        raise ConfigurationError(
            "a deployment for another machine needs a platform document with profile: production; "
            f"this one is {platform.profile}. Nothing was written."
        )
    if external_entry_point(platform) is None:
        raise ConfigurationError(
            "the platform document does not select the certificate-requiring external entry point "
            "(remote-gateway) in gateway.entry_points. Nothing was written."
        )
    if not options.external_bind_address:
        raise ConfigurationError(
            "state the host address the external entry point is published on (--external-bind-address). "
            "Nothing was written."
        )
    try:
        ipaddress.ip_address(options.external_bind_address)
    except ValueError:
        raise ConfigurationError(
            f"--external-bind-address {options.external_bind_address} is not an IP address. Nothing was written."
        ) from None
    if options.remote_rendered_dir is None or options.remote_secrets_dir is None or options.remote_run_as is None:
        raise ConfigurationError("the remote directories and the remote account's UID and GID are required")
    for directory in (options.remote_rendered_dir, options.remote_secrets_dir):
        if not directory.is_absolute():
            raise ConfigurationError(f"{directory}: the remote directory must be an absolute path")
    select_credentials(options, platform)
    client, problems = vault_client(options, platform)
    if problems or client is None:
        raise ConfigurationError("prerequisites not met:\n  - " + "\n  - ".join(problems))
    ensure_secrets(options, platform, client, None, out)
    # The output directory is usually new; the secret tree itself is created owner-only by materialize.
    options.secrets_dir.parent.mkdir(parents=True, exist_ok=True)
    changed = render(options, platform, options.remote_run_as, client, out)
    _write(
        options.rendered_dir / DEPLOYMENT_MANIFEST,
        json.dumps(deployment_manifest(options, platform), indent=2, sort_keys=True) + "\n", 0o644,
    )
    _write(options.rendered_dir / SOURCE_ARCHIVE, source_archive(), 0o644)
    out(f"Rendered a deployment of {platform.gateway.hostname} for another machine.")
    out(f"  Rendered tree: {options.rendered_dir}  ->  {options.remote_rendered_dir}")
    out(f"  Secret tree:   {options.secrets_dir}  ->  {options.remote_secrets_dir}")
    out(f"  {SINGLE_NODE_NOTICE}")
    out("  Created this run: " + (", ".join(options.created) if options.created else "nothing new"))
    return changed


def _unhealthy(options: Options, runner: Runner) -> list[str]:
    result = runner([*_compose(options), "ps", "--all", "--format", "json"], capture_output=True, text=True)
    services: list[str] = []
    for line in (result.stdout or "").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        for entry in item if isinstance(item, list) else [item]:
            state, health, code = entry.get("State"), entry.get("Health"), entry.get("ExitCode", 0)
            failed_exit = state in ("exited", "stopped") and code not in (0, None)
            if failed_exit or health in ("unhealthy", "starting") or state in ("restarting", "dead", "created"):
                services.append(f"{entry.get('Service')} ({health or state})")
    return sorted(set(services))


def quickstart(
    options: Options, *, runner: Runner = subprocess.run, out: Callable[[str], None] = print,
    port_is_free: Callable[[str, int], bool] = _port_is_free,
) -> None:
    run = partial(runner, env=_environment(options))
    platform = load_platform(options.config)
    if platform.deployment != "docker":
        raise ConfigurationError(f"the quickstart needs a docker platform document, not {platform.deployment}")
    # Fails before anything is checked, created, or started when a credential choice is missing or wrong.
    _, query_choice = select_credentials(options, platform)
    run_as, client = preflight(options, platform, run, port_is_free)
    was_running = _stack_is_running(options, run)
    ensure_secrets(options, platform, client, run, out)
    changed = render(options, platform, run_as, client, out)

    compose = _compose(options)
    if options.build:
        _run(run, [*compose, "build", "authz"], "building the NightHawk image")
    up = [*compose]
    services = list(WAITED_SERVICES)
    if options.host_collection:
        up += ["--profile", HOST_COLLECTION_PROFILE]
        services.append(HOST_COLLECTION_SERVICE)
    try:
        _run(run, [*up, "up", "--detach", "--wait", "--wait-timeout", str(options.timeout), *services],
             "starting the stack", timeout=options.timeout + 60)
        _run(run, [*compose, "run", "--rm", "wait-alloy"], "waiting for the collector", timeout=options.timeout)
    except ConfigurationError as error:
        failing = _unhealthy(options, run)
        raise ConfigurationError(f"{error}" + (f"; not healthy: {', '.join(failing)}" if failing else "")) from None
    if was_running:
        # Running services keep their mounts; tell the ones that do not watch their files to reload.
        reloads = [("authz", "authz"), ("collector", "alloy")]
        if options.host_collection:
            reloads.append(("collector-host", HOST_COLLECTION_SERVICE))
        for part, service in reloads:
            if changed.get(part):
                _run(run, [*compose, "kill", "--signal", "HUP", service], f"reloading {service}")
    provision = [*compose, "run", "--rm", "grafana-init"]
    for credential_id in query_choice:
        provision += ["--credential", credential_id]
    if changed.get("query-secrets"):
        provision.append("--update-secrets")
    _run(run, provision, "provisioning Grafana", timeout=options.timeout)

    stream = _select_stream(options, platform)
    port = local_entry_point(platform).port
    out("NightHawk is running.")
    out(f"  Gateway:  https://{platform.gateway.hostname}:{port}  (published on {options.bind_address}:{port})")
    out(f"  Grafana:  https://{platform.grafana.hostname}:{port}  (user admin)")
    out(f"  Collector datastream: {stream.tenant}/{stream.datastream}")
    out("  Retention values in the platform document are examples, not production defaults.")
    out("  Created this run: " + (", ".join(options.created) if options.created else "nothing new"))


def teardown(
    options: Options, *, purge: bool = False, confirmed: bool = False, runner: Runner = subprocess.run,
    out: Callable[[str], None] = print,
) -> None:
    """Stop and remove containers and networks. Volumes are removed only on a confirmed purge."""
    if purge and not confirmed:
        raise ConfigurationError("purging deletes all stored telemetry; confirm it explicitly with --yes")
    run = partial(runner, env=_environment(options))
    if not (options.rendered_dir / "compose.env").exists():
        raise ConfigurationError(f"{options.rendered_dir / 'compose.env'} not found; nothing to tear down")
    command = [
        *_compose(options), "--profile", "sample", "--profile", "tools", "--profile", HOST_COLLECTION_PROFILE,
        "down", "--remove-orphans",
    ]
    if purge:
        command.append("--volumes")
    _run(run, command, "tearing down the stack", timeout=options.timeout)
    out("Stopped and removed the NightHawk containers and networks.")
    out("Deleted all NightHawk volumes." if purge else "Volumes, secrets, and certificates were kept.")
