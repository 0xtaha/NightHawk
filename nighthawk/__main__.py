"""Validate and render non-secret intermediate contracts, not deployments."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from functools import partial
from pathlib import Path

import yaml

from nighthawk.config import (
    ConfigurationError, ROOT, check_pins, load_network, load_platform, load_versions, validate_migration,
)
from nighthawk import authz, backends, collector, fixtures, gateway, grafana, quickstart, tools, trust, vault
from nighthawk.overrides import render_overrides
from nighthawk.secrets import (
    MATERIALIZED_SECRETS_DIR, cleanup, doctor, ensure_doctor_ok, materialize, rotate_secret, store_secret,
)


def _add_contract_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
    parser.add_argument(
        "--previous", type=Path, action="append", default=[],
        help="Historical platform YAML for migration comparison; repeat for retained history",
    )
    parser.add_argument("--storage-output", type=Path, help="Storage bindings from 'terraform output -json storage'")


def _add_vault_arguments(parser: argparse.ArgumentParser) -> None:
    """Where the Vault credential comes from. $VAULT_TOKEN takes precedence; none is ever an argument."""
    parser.add_argument("--vault-token-file", type=Path, help="Owner-only file holding a Vault token")
    parser.add_argument("--vault-role-id-file", type=Path, help="Owner-only file holding an AppRole role ID")
    parser.add_argument("--vault-secret-id-file", type=Path, help="Owner-only file holding an AppRole secret ID")


def _auth(args: argparse.Namespace) -> vault.Auth:
    return vault.Auth(args.vault_token_file, args.vault_role_id_file, args.vault_secret_id_file)


def _read_value(value_file: Path | None) -> str:
    """A secret value from a file or standard input; never from an argument."""
    text = value_file.read_text(encoding="utf-8") if value_file is not None else sys.stdin.read()
    return text.rstrip("\r\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    # No abbreviations: `--vault-token <value>` must not be read as `--vault-token-file`.
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=partial(argparse.ArgumentParser, allow_abbrev=False),
    )

    validate_parser = subparsers.add_parser("validate")
    _add_contract_arguments(validate_parser)

    render_parser = subparsers.add_parser("render-contracts")
    _add_contract_arguments(render_parser)
    render_parser.add_argument("--output", type=Path, required=True, help="New directory for non-secret contract artifacts")
    render_parser.add_argument(
        "--credential", action="append", default=[],
        help="Query credential ID for a datastream that declares several; repeat per datastream",
    )

    check_pins_parser = subparsers.add_parser("check-pins")
    check_pins_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

    doctor_parser = subparsers.add_parser("doctor", help="Check the declared Vault before anything uses it")

    store_secret_parser = subparsers.add_parser("store-secret", help="Store a value that does not exist yet")
    rotate_secret_parser = subparsers.add_parser("rotate-secret", help="Replace an existing value with a new version")
    for value_parser in (store_secret_parser, rotate_secret_parser):
        value_parser.add_argument("--secret", required=True, help="Secret reference name from the platform document")
        value_parser.add_argument("--value-file", type=Path, help="File holding the value; standard input if omitted")

    materialize_parser = subparsers.add_parser("materialize-secrets")
    materialize_parser.add_argument("--output-dir", type=Path, default=MATERIALIZED_SECRETS_DIR)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-dev-vault", help="Development only: configure a disposable Vault with what the platform needs",
    )
    bootstrap_parser.add_argument("--confirm-disposable-vault", action="store_true")
    bootstrap_parser.add_argument("--ca-valid-days", type=int, required=True)

    clean_secrets_parser = subparsers.add_parser("clean-secrets")
    clean_secrets_parser.add_argument("--output-dir", type=Path, default=MATERIALIZED_SECRETS_DIR)

    collector_parser = subparsers.add_parser("render-collector")
    collector_parser.add_argument("--config", type=Path, required=True)
    collector_parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
    collector_parser.add_argument("--tenant", required=True)
    collector_parser.add_argument("--datastream", required=True)
    collector_parser.add_argument("--profile", required=True, help=f"One of: {', '.join(collector.PROFILES)}")
    collector_parser.add_argument("--entry-point", help="Gateway entry point (network rule ID) this collector connects to")
    collector_parser.add_argument("--credential", help="Ingestion credential ID, when several qualify")
    collector_parser.add_argument("--self-monitoring", action="store_true", help="Also scrape backends, gateway, and auth service")
    collector_parser.add_argument("--otlp-only", action="store_true", help="Omit host sources; receive pushed telemetry only")
    collector_parser.add_argument("--output", type=Path, required=True, help="New directory for the collector configuration")

    policy_parser = subparsers.add_parser("render-gateway-policy")
    policy_parser.add_argument("--config", type=Path, required=True)
    policy_parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
    policy_parser.add_argument("--secrets-dir", type=Path, default=MATERIALIZED_SECRETS_DIR)
    policy_parser.add_argument("--output", type=Path, required=True, help="Policy bundle file; replaced atomically")

    serve_parser = subparsers.add_parser("serve-authz")
    serve_parser.add_argument("--policy", type=Path, required=True)
    serve_parser.add_argument("--listen", required=True, help="host:port; must be reachable only from the gateway proxy")

    generate_credential_parser = subparsers.add_parser("generate-credential")
    generate_credential_parser.add_argument("--credential", required=True)
    storage_identity_parser = subparsers.add_parser("generate-storage-identity")
    storage_identity_parser.add_argument("--identity", required=True, help="Secret reference of a storage binding identity")
    issue_parser = subparsers.add_parser("issue-certificate")
    issue_target = issue_parser.add_mutually_exclusive_group(required=True)
    issue_target.add_argument("--credential", help="Credential that declares the certificate identity")
    issue_target.add_argument("--server", action="store_true", help="Issue the gateway server certificate (gateway and Grafana host names)")
    issue_target.add_argument("--storage", action="store_true", help="Issue the local object storage server certificate")
    issue_parser.add_argument("--valid-days", type=int, help="Required; there is no default validity")
    issue_parser.add_argument("--output-dir", type=Path, required=True)
    revoke_parser = subparsers.add_parser("revoke-certificate", help="Revoke in Vault and print the fingerprint to list")
    revoke_parser.add_argument("--certificate", type=Path, required=True)
    for vault_parser in (
        doctor_parser, store_secret_parser, rotate_secret_parser, materialize_parser, bootstrap_parser,
        generate_credential_parser, storage_identity_parser, issue_parser, revoke_parser,
    ):
        vault_parser.add_argument("--config", type=Path, required=True)
        vault_parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
        vault_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")
        _add_vault_arguments(vault_parser)

    grafana_parser = subparsers.add_parser("provision-grafana")
    grafana_parser.add_argument("--config", type=Path, required=True)
    grafana_parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
    grafana_parser.add_argument("--url", required=True, help="Grafana base URL")
    grafana_parser.add_argument("--admin-user", required=True)
    grafana_parser.add_argument("--admin-password-file", type=Path, required=True)
    grafana_parser.add_argument("--secrets-dir", type=Path, default=MATERIALIZED_SECRETS_DIR)
    grafana_parser.add_argument("--dry-run", action="store_true", help="Report changes without sending a modifying request")
    grafana_parser.add_argument("--prune", action="store_true", help="Delete NightHawk data sources that are no longer declared")
    grafana_parser.add_argument("--update-secrets", action="store_true", help="Resend every data source password, e.g. after rotate-secret")
    grafana_parser.add_argument(
        "--credential", action="append", default=[],
        help="Query credential ID for a datastream that declares several; repeat per datastream",
    )

    fetch_parser = subparsers.add_parser("fetch-tools", help="Download the pinned Terraform, verified by checksum")
    fetch_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")
    fetch_parser.add_argument("--tools-dir", type=Path, default=tools.TOOLS_DIR)

    wait_parser = subparsers.add_parser("wait-http")
    wait_parser.add_argument("--url", action="append", required=True, help="Repeat; every URL must answer 200")
    wait_parser.add_argument("--timeout", type=float, required=True, help="Seconds before giving up")

    fixtures_parser = subparsers.add_parser("emit-fixtures")
    fixtures_parser.add_argument("--config", type=Path, required=True)
    fixtures_parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
    fixtures_parser.add_argument("--tenant", required=True)
    fixtures_parser.add_argument("--datastream", required=True)
    fixtures_parser.add_argument("--run-id", required=True, help="Alphanumeric identifier carried by every fixture item")
    fixtures_parser.add_argument("--otlp", required=True, help="Collector OTLP HTTP base URL")
    fixtures_parser.add_argument("--profiles", required=True, help="Collector profile push base URL")

    quickstart_parser = subparsers.add_parser("quickstart-docker")
    quickstart_parser.add_argument("--tenant", help="Tenant of the collector's datastream (default: the first)")
    quickstart_parser.add_argument("--datastream")
    quickstart_parser.add_argument("--collector-entry-point", default="local-gateway")
    quickstart_parser.add_argument("--bind-address", default="127.0.0.1", help="Loopback address the gateway is published on")
    quickstart_parser.add_argument(
        "--credential", action="append", default=[],
        help="Credential ID to use where a datastream declares several of one permission; repeat as needed",
    )
    quickstart_parser.add_argument("--certificate-valid-days", type=int, default=90)
    quickstart_parser.add_argument("--no-build", action="store_true", help="Reuse the existing NightHawk image")
    quickstart_parser.add_argument(
        "--host-collection", action="store_true",
        help="Also collect host and container telemetry (privileged; Docker Engine only)",
    )
    _add_vault_arguments(quickstart_parser)
    teardown_parser = subparsers.add_parser("teardown-docker")
    teardown_parser.add_argument("--purge", action="store_true", help="Also delete every volume, including stored telemetry")
    teardown_parser.add_argument("--yes", action="store_true", help="Confirm --purge without a prompt")
    for stack_parser in (quickstart_parser, teardown_parser):
        stack_parser.add_argument("--config", type=Path, default=ROOT / "config" / "tenants.example.yaml")
        stack_parser.add_argument(
            "--compose", default=os.environ.get("NIGHTHAWK_COMPOSE", "docker compose"),
            help="Compose command (default: $NIGHTHAWK_COMPOSE or 'docker compose')",
        )
        stack_parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for the stack")

    args = parser.parse_args(argv)

    if args.command in ("quickstart-docker", "teardown-docker"):
        compose = tuple(shlex.split(args.compose))
        try:
            if not compose:
                raise ConfigurationError("--compose must name a command")
            if args.command == "quickstart-docker":
                quickstart.quickstart(quickstart.Options(
                    config=args.config, compose=compose, container=compose[0], tenant=args.tenant,
                    datastream=args.datastream, collector_entry_point=args.collector_entry_point,
                    bind_address=args.bind_address, certificate_valid_days=args.certificate_valid_days,
                    timeout=args.timeout, build=not args.no_build, vault_auth=_auth(args),
                    credentials=tuple(args.credential), host_collection=args.host_collection,
                ))
            else:
                confirmed = args.yes
                if args.purge and not confirmed and sys.stdin.isatty():
                    confirmed = input("Delete all NightHawk volumes and stored telemetry? Type 'purge': ") == "purge"
                quickstart.teardown(
                    quickstart.Options(config=args.config, compose=compose, container=compose[0], timeout=args.timeout),
                    purge=args.purge, confirmed=confirmed,
                )
            return 0
        except (ConfigurationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command in ("fetch-tools", "wait-http", "emit-fixtures"):
        try:
            if args.command == "fetch-tools":
                installed = tools.fetch_tools(load_versions(args.versions), args.tools_dir)
                print(
                    f"Installed {', '.join(installed)} into {args.tools_dir}" if installed
                    else f"terraform is already present in {args.tools_dir}"
                )
            elif args.command == "wait-http":
                tools.wait_http(args.url, args.timeout)
                print(f"Ready: {', '.join(args.url)}")
            else:
                platform = load_platform(args.config, network=args.network)
                stream = next(
                    (item for item in platform.streams if (item.tenant, item.datastream) == (args.tenant, args.datastream)),
                    None,
                )
                if stream is None:
                    raise ConfigurationError(f"unknown tenant/datastream {args.tenant}/{args.datastream}")
                print(json.dumps(fixtures.emit(stream, args.run_id, args.otlp, args.profiles), sort_keys=True))
            return 0
        except (ConfigurationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command == "serve-authz":
        try:
            return authz.serve(args.policy, args.listen)
        except (ConfigurationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command in ("render-collector", "render-gateway-policy", "provision-grafana"):
        try:
            platform = load_platform(args.config, network=args.network)
            if args.command == "render-collector":
                files = collector.render_collector(
                    platform, args.tenant, args.datastream, args.profile, entry_point=args.entry_point,
                    credential_id=args.credential, self_monitoring=args.self_monitoring,
                    otlp_only=args.otlp_only,
                )
                collector.write_collector(files, args.output)
                print(f"Rendered {args.profile} collector for {args.tenant}/{args.datastream} to {args.output}.")
            elif args.command == "render-gateway-policy":
                bundle = gateway.policy_bundle(platform, args.secrets_dir)
                gateway.write_policy_bundle(bundle, args.output)
                print(f"Rendered gateway policy for {len(bundle['credentials'])} credential(s) to {args.output}.")
            else:
                password = args.admin_password_file.read_text(encoding="utf-8").rstrip("\r\n")
                changes = grafana.reconcile(
                    grafana.desired_state(platform, args.credential),
                    grafana.http_client(args.url, args.admin_user, password),
                    grafana.secret_reader(platform, args.secrets_dir),
                    dry_run=args.dry_run, prune=args.prune, update_secrets=args.update_secrets,
                )
                for line in changes.lines(args.dry_run):
                    print(line)
            return 0
        except (ConfigurationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command == "clean-secrets":
        try:
            cleanup(args.output_dir)
            print(f"Cleaned {args.output_dir}")
            return 0
        except OSError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command == "doctor":
        try:
            checks, _ = doctor(load_versions(args.versions), load_platform(args.config, network=args.network), _auth(args))
        except ConfigurationError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        for check in checks:
            print(f"{'ok' if check.ok else 'FAIL'}: {check.detail}")
        return 0 if all(check.ok for check in checks) else 1

    if args.command == "bootstrap-dev-vault":
        try:
            platform = load_platform(args.config, network=args.network)
            created = vault.bootstrap_dev(
                platform, vault.connect(platform, _auth(args)),
                confirmed=args.confirm_disposable_vault, ca_valid_days=args.ca_valid_days,
            )
            print("Configured the development Vault: " + (", ".join(created) if created else "nothing to change"))
            return 0
        except (ConfigurationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command in (
        "store-secret", "rotate-secret", "materialize-secrets", "generate-credential",
        "generate-storage-identity", "issue-certificate", "revoke-certificate",
    ):
        try:
            platform = load_platform(args.config, network=args.network)
            client = ensure_doctor_ok(load_versions(args.versions), platform, _auth(args))
            if args.command == "store-secret":
                reference = store_secret(client, platform, args.secret, _read_value(args.value_file))
                print(f"Stored {args.secret} in Vault at {reference.path} key {reference.key}")
            elif args.command == "rotate-secret":
                reference = rotate_secret(client, platform, args.secret, _read_value(args.value_file))
                print(f"Rotated {args.secret} in Vault at {reference.path} key {reference.key}")
            elif args.command == "materialize-secrets":
                materialize(platform, client, args.output_dir)
                print(f"Materialized {len(platform.secrets)} secret(s) into {args.output_dir}")
            elif args.command == "generate-credential":
                reference = trust.generate_credential(platform, args.credential, client)
                print(f"Generated the secret for {args.credential} in Vault at {reference.path} key {reference.key}")
            elif args.command == "generate-storage-identity":
                reference = trust.generate_storage_identity(platform, args.identity, client)
                print(f"Generated the storage keys for {args.identity} in Vault at {reference.path} key {reference.key}")
            elif args.command == "issue-certificate":
                issued = trust.issue_certificate(
                    platform, args.output_dir, args.valid_days, client, credential_id=args.credential,
                    server=args.server, storage=args.storage,
                )
                print(f"Issued {issued.certificate_path} with key {issued.key_path}")
                print(f"SHA-256 fingerprint: {issued.fingerprint}")
            else:
                fingerprint = trust.revoke_certificate(client, args.certificate)
                print(f"Revoked {args.certificate} in Vault.")
                print(f"Add this fingerprint to gateway.revoked_certificate_fingerprints: {fingerprint}")
            return 0
        except (ConfigurationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command == "check-pins":
        try:
            matrix = load_versions(args.versions)
            mismatches = check_pins(matrix)
        except ConfigurationError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if mismatches:
            for mismatch in mismatches:
                print(mismatch, file=sys.stderr)
            return 1
        print(f"All {len(matrix.get('tracked_consumers', []))} tracked consumer(s) match the compatibility matrix.")
        return 0

    try:
        platform = load_platform(args.config, args.storage_output, args.network)
        rules = load_network(args.network)
        for previous in args.previous:
            validate_migration(platform, load_platform(previous, network=args.network))
        overrides, unenforced = render_overrides(platform)
        if args.command == "render-contracts":
            output = args.output
            output.mkdir(parents=True, mode=0o700, exist_ok=False)
            artifacts = {
                "platform.json": json.dumps(platform.manifest(), indent=2, sort_keys=True) + "\n",
                "network.json": json.dumps({"schema_version": 1, "rules": rules}, indent=2, sort_keys=True) + "\n",
                **{
                    f"{backend}-overrides.yaml": yaml.safe_dump(document, sort_keys=True)
                    for backend, document in overrides.items()
                },
                "unenforced-limits.json": json.dumps(unenforced, indent=2, sort_keys=True) + "\n",
                "ports.md": (
                    "# Network contract\n\n"
                    "This is an initial contract, not a complete deployment firewall.\n\n"
                    "| ID | Source | Destination | Protocol | Port | Scope | Purpose |\n"
                    "| --- | --- | --- | --- | --- | --- | --- |\n"
                    + "".join(
                        "| " + " | ".join(str(rule[key]) for key in (
                            "id", "source", "destination", "protocol", "port", "scope", "purpose"
                        )) + " |\n" for rule in rules
                    )
                ),
            }
            artifacts.update(gateway.render_artifacts(platform, rules))
            artifacts.update(vault.access_requirements(platform))
            backend_documents = backends.render_backends(platform)
            for backend, document in (backend_documents or {}).items():
                artifacts[f"backends/{backend}.yaml"] = document
            artifacts["grafana/desired-state.json"] = (
                json.dumps(grafana.desired_state(platform, args.credential), indent=2, sort_keys=True) + "\n"
            )
            for name, content in artifacts.items():
                (output / name).parent.mkdir(mode=0o700, exist_ok=True)
                with (output / name).open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
            print(f"Rendered non-secret contracts to {output}; no deployment was generated.")
            if backend_documents is None:
                print(f"Backend configuration is not rendered for the {platform.deployment} deployment yet.")
        else:
            print(f"Valid contracts: {len(platform.streams)} datastream(s), {len(rules)} network rule(s).")
        return 0
    except (ConfigurationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
