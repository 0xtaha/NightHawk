"""One-command local bring-up of the Docker Compose stack, and its teardown.

Nothing is replaced: secrets, identities, the CA, and certificates are created only
when missing. Rendered files are synchronized into stable directories file by file,
because running containers keep those directories mounted.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import re
import secrets as token_source
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable

from cryptography import x509

from nighthawk import collector, gateway, storage, trust
from nighthawk.config import ROOT, ConfigurationError, Platform, load_platform, load_versions
from nighthawk.secrets import Runner, doctor, generate_recipient, materialize
from nighthawk.tools import TOOLS_DIR

COMPOSE_FILE = ROOT / "docker-compose" / "docker-compose.yaml"
# Long-running services whose readiness the bring-up waits for; their dependencies follow.
WAITED_SERVICES = ("grafana", "alloy")
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
    age_key: Path = ROOT / "secrets" / "local.agekey"
    tools_dir: Path = TOOLS_DIR
    tenant: str | None = None
    datastream: str | None = None
    collector_entry_point: str = "local-gateway"
    bind_address: str = "127.0.0.1"
    ca_valid_days: int = 365
    certificate_valid_days: int = 90
    timeout: int = 600
    build: bool = True
    project: str = "nighthawk"
    created: list[str] = field(default_factory=list)


def _environment(options: Options) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PATH"] = f"{options.tools_dir}{os.pathsep}{environment.get('PATH', '')}"
    environment["SOPS_AGE_KEY_FILE"] = str(options.age_key)
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
    return [
        *options.compose, "--project-name", options.project, "--file", str(options.compose_file),
        "--env-file", str(options.rendered_dir / "compose.env"),
    ]


def _port_is_free(address: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
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
) -> tuple[int, int]:
    """Check every prerequisite before anything is written; return the UID and GID services run as."""
    problems: list[str] = []
    for args, name in (([*options.compose, "version"], "the Compose command"), ([options.container, "info"], "the container runtime")):
        try:
            if runner(args, capture_output=True, text=True).returncode != 0:
                problems.append(f"{name} ({' '.join(args)}) is not usable")
        except (OSError, subprocess.SubprocessError):
            problems.append(f"{name} ({' '.join(args)}) is not installed")
    for check in doctor(load_versions(), runner=runner):
        if not check.ok:
            problems.append(f"{check.detail}; run `python -m nighthawk fetch-tools`")
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
        if not _stack_is_running(options, runner):
            published = [entry.port for entry in platform.gateway.entry_points if entry.scope == "loopback"]
            for port in published:
                if not port_is_free(options.bind_address, port):
                    problems.append(f"port {options.bind_address}:{port} is already in use")
    if problems:
        raise ConfigurationError("prerequisites not met:\n  - " + "\n  - ".join(problems))
    return run_as


def _exists(options: Options, platform: Platform, reference: str) -> bool:
    item = platform.secrets[reference]
    return trust.secret_key_exists(options.root / item.file, item.key)


def _recipient(options: Options, runner: Runner) -> str:
    if not options.age_key.exists():
        options.age_key.parent.mkdir(parents=True, exist_ok=True)
        recipient = generate_recipient(options.age_key, runner=runner)
        options.created.append("age key")
        return recipient
    match = re.search(r"public key: (age1[0-9a-z]+)", options.age_key.read_text(encoding="utf-8"))
    if match is None:
        raise ConfigurationError(f"{options.age_key}: no public key comment found")
    return match.group(1)


def ensure_secrets(options: Options, platform: Platform, runner: Runner, out: Callable[[str], None]) -> None:
    """Create each referenced secret that does not exist yet. Existing values are never touched."""
    recipients = [_recipient(options, runner)]
    common = {"root": options.root, "runner": runner}
    for credential in platform.credentials:
        if not _exists(options, platform, credential.secret_ref):
            trust.generate_credential(platform, credential.id, recipients, **common)
            options.created.append(f"credential {credential.id}")
    for reference in trust.storage_identity_refs(platform):
        if not _exists(options, platform, reference):
            trust.generate_storage_identity(platform, reference, recipients, **common)
            options.created.append(f"storage identity {reference}")
    admin = platform.grafana.admin_secret_ref
    if not _exists(options, platform, admin):
        password = token_source.token_urlsafe(24)
        trust.store_new_secret(platform.secrets[admin], password, recipients, profile=platform.profile, **common)
        options.created.append("Grafana admin password")
        out(f"Grafana admin password (shown once): {password}")
    ca = platform.gateway.client_ca_secret_ref
    if not _exists(options, platform, ca):
        trust.init_ca(platform, recipients, options.ca_valid_days, **common)
        options.created.append("certificate authority")
    # The local CA also signs the storage certificate, so the storage trust references hold its certificate.
    storage_cas = {binding.tls.ca_secret_ref for binding in platform.bindings.values() if binding.tls.ca_secret_ref}
    for reference in sorted(storage_cas):
        if not _exists(options, platform, reference):
            certificate = trust._decrypt(platform.secrets[ca], options.root, runner)
            trust.store_new_secret(platform.secrets[reference], certificate, recipients, profile=platform.profile, **common)
            options.created.append(f"storage trust {reference}")
    missing = sorted(reference for reference in platform.secrets if not _exists(options, platform, reference))
    if missing:
        raise ConfigurationError(f"the quickstart cannot create these secrets: {', '.join(missing)}")


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


def _needs_issue(certificate_path: Path) -> bool:
    if not certificate_path.exists():
        return True
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    except ValueError:
        return True
    return certificate.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) < RENEW_BEFORE


def _ensure_certificate(options: Options, platform: Platform, runner: Runner, directory: Path, stem: str, **what) -> None:
    certificate_path, key_path = directory / f"{stem}.crt.pem", directory / f"{stem}.key.pem"
    if not _needs_issue(certificate_path) and key_path.exists():
        return
    for path in (certificate_path, key_path):
        path.unlink(missing_ok=True)
    trust.issue_certificate(
        platform, directory, options.certificate_valid_days, root=options.root, runner=runner, **what,
    )
    options.created.append(f"certificate {stem}")


def _select_stream(options: Options, platform: Platform):
    if options.tenant is None and options.datastream is None:
        return platform.streams[0]
    for stream in platform.streams:
        if (stream.tenant, stream.datastream) == (options.tenant, options.datastream):
            return stream
    raise ConfigurationError(f"unknown tenant/datastream {options.tenant}/{options.datastream}")


def render(options: Options, platform: Platform, run_as: tuple[int, int], runner: Runner) -> dict[str, bool]:
    """Materialize secrets and write every rendered and runtime file; report which parts changed."""
    # Imported here: __main__ imports this module.
    from nighthawk.__main__ import main

    materialize(options.config, root=options.root, output_dir=options.secrets_dir, runner=runner)
    runtime = options.secrets_dir / "runtime"

    def read(reference: str) -> str:
        item = platform.secrets[reference]
        return (options.secrets_dir / item.file / item.key).read_text(encoding="utf-8").rstrip("\r\n")

    for name in ("gateway", "storage", "collector-certificates"):
        (runtime / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    _ensure_certificate(options, platform, runner, runtime / "gateway", "gateway-server", server=True)
    if platform.storage_provider == "seaweedfs":
        _ensure_certificate(options, platform, runner, runtime / "storage", "storage-server", storage=True)
    stream = _select_stream(options, platform)
    credential = collector._select_credential(platform, stream, collector.PROFILES["docker"], None)
    if credential.certificate_identity is not None:
        _ensure_certificate(
            options, platform, runner, runtime / "collector-certificates", credential.id, credential_id=credential.id,
        )

    with tempfile.TemporaryDirectory() as temporary:
        contracts = Path(temporary) / "contracts"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            status = main(["render-contracts", "--config", str(options.config), "--output", str(contracts)])
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

    ca_certificate = read(platform.gateway.client_ca_secret_ref) + "\n"
    changed["gateway"] = any([
        _write(runtime / "gateway" / "client-ca.pem", ca_certificate, 0o600),
        _write(runtime / "gateway" / "traefik-static.yaml", rendered["gateway/traefik-static.yaml"], 0o600),
        _write(runtime / "gateway" / "traefik-dynamic.yaml", rendered["gateway/traefik-dynamic.yaml"], 0o600),
    ])
    storage_trust = ""
    if platform.storage_provider == "seaweedfs":
        storage_cas = sorted({b.tls.ca_secret_ref for b in platform.bindings.values() if b.tls.ca_secret_ref})
        storage_trust = "".join(read(reference) + "\n" for reference in storage_cas)
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

    _write(options.rendered_dir / "compose.env", "".join(f"{key}={value}\n" for key, value in {
        "COMPOSE_PROJECT_NAME": options.project,
        "NIGHTHAWK_RENDERED_DIR": options.rendered_dir.resolve(),
        "NIGHTHAWK_SECRETS_DIR": options.secrets_dir.resolve(),
        "NIGHTHAWK_UID": run_as[0],
        "NIGHTHAWK_GID": run_as[1],
        "NIGHTHAWK_BIND_ADDRESS": options.bind_address,
        "NIGHTHAWK_GATEWAY_HOSTNAME": platform.gateway.hostname,
        "NIGHTHAWK_GRAFANA_HOSTNAME": platform.grafana.hostname,
    }.items()), 0o644)
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
    run_as = preflight(options, platform, run, port_is_free)
    was_running = _stack_is_running(options, run)
    ensure_secrets(options, platform, run, out)
    changed = render(options, platform, run_as, run)

    compose = _compose(options)
    if options.build:
        _run(run, [*compose, "build", "authz"], "building the NightHawk image")
    try:
        _run(run, [*compose, "up", "--detach", "--wait", "--wait-timeout", str(options.timeout), *WAITED_SERVICES],
             "starting the stack", timeout=options.timeout + 60)
        _run(run, [*compose, "run", "--rm", "wait-alloy"], "waiting for the collector", timeout=options.timeout)
    except ConfigurationError as error:
        failing = _unhealthy(options, run)
        raise ConfigurationError(f"{error}" + (f"; not healthy: {', '.join(failing)}" if failing else "")) from None
    if was_running:
        # Running services keep their mounts; tell the ones that do not watch their files to reload.
        for part, service in (("authz", "authz"), ("collector", "alloy")):
            if changed.get(part):
                _run(run, [*compose, "kill", "--signal", "HUP", service], f"reloading {service}")
    _run(run, [*compose, "run", "--rm", "grafana-init"], "provisioning Grafana", timeout=options.timeout)

    stream = _select_stream(options, platform)
    local = [entry for entry in platform.gateway.entry_points if entry.scope == "loopback"]
    port = local[0].port if local else platform.gateway.entry_points[0].port
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
    command = [*_compose(options), "--profile", "sample", "--profile", "tools", "down", "--remove-orphans"]
    if purge:
        command.append("--volumes")
    _run(run, command, "tearing down the stack", timeout=options.timeout)
    out("Stopped and removed the NightHawk containers and networks.")
    out("Deleted all NightHawk volumes." if purge else "Volumes, secrets, and certificates were kept.")
