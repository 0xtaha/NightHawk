"""Validate and render non-secret intermediate contracts, not deployments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from nighthawk.config import (
    ConfigurationError, ROOT, check_pins, load_network, load_platform, load_versions, validate_migration,
)
from nighthawk import authz, collector, gateway, grafana, trust
from nighthawk.overrides import render_overrides
from nighthawk.secrets import (
    MATERIALIZED_SECRETS_DIR, SECRETS_DIR, cleanup, doctor, encrypt_secret, ensure_doctor_ok,
    generate_recipient, materialize, rotate_secret,
)


def _add_contract_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
    parser.add_argument(
        "--previous", type=Path, action="append", default=[],
        help="Historical platform YAML for migration comparison; repeat for retained history",
    )
    parser.add_argument("--storage-output", type=Path, help="Storage bindings from 'terraform output -json storage'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    _add_contract_arguments(validate_parser)

    render_parser = subparsers.add_parser("render-contracts")
    _add_contract_arguments(render_parser)
    render_parser.add_argument("--output", type=Path, required=True, help="New directory for non-secret contract artifacts")

    check_pins_parser = subparsers.add_parser("check-pins")
    check_pins_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

    generate_recipient_parser = subparsers.add_parser("generate-recipient")
    generate_recipient_parser.add_argument("--output", type=Path, required=True)
    generate_recipient_parser.add_argument("--overwrite", action="store_true")
    generate_recipient_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

    encrypt_secret_parser = subparsers.add_parser("encrypt-secret")
    encrypt_secret_parser.add_argument("--file", required=True, help="Target filename under secrets/")
    encrypt_secret_parser.add_argument("--key", required=True)
    encrypt_secret_parser.add_argument("--value", help="Plaintext value; read from stdin if omitted")
    encrypt_secret_parser.add_argument("--recipient", action="append", default=[], help="Repeat for multiple age recipients")
    encrypt_secret_parser.add_argument("--secrets-dir", type=Path, default=SECRETS_DIR)
    encrypt_secret_parser.add_argument("--profile", default="development")
    encrypt_secret_parser.add_argument("--confirm-production-recipients", action="store_true")
    encrypt_secret_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

    rotate_secret_parser = subparsers.add_parser("rotate-secret")
    rotate_secret_parser.add_argument("--file", required=True)
    rotate_secret_parser.add_argument("--key", required=True)
    rotate_secret_parser.add_argument("--value", help="New plaintext value; read from stdin if omitted")
    rotate_secret_parser.add_argument("--secrets-dir", type=Path, default=SECRETS_DIR)
    rotate_secret_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

    materialize_parser = subparsers.add_parser("materialize-secrets")
    materialize_parser.add_argument("--config", type=Path, required=True)
    materialize_parser.add_argument("--root", type=Path, default=ROOT)
    materialize_parser.add_argument("--output-dir", type=Path, default=MATERIALIZED_SECRETS_DIR)
    materialize_parser.add_argument("--confirm-production-recipients", action="store_true")
    materialize_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

    clean_secrets_parser = subparsers.add_parser("clean-secrets")
    clean_secrets_parser.add_argument("--output-dir", type=Path, default=MATERIALIZED_SECRETS_DIR)
    clean_secrets_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

    collector_parser = subparsers.add_parser("render-collector")
    collector_parser.add_argument("--config", type=Path, required=True)
    collector_parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
    collector_parser.add_argument("--tenant", required=True)
    collector_parser.add_argument("--datastream", required=True)
    collector_parser.add_argument("--profile", required=True, help=f"One of: {', '.join(collector.PROFILES)}")
    collector_parser.add_argument("--entry-point", help="Gateway entry point (network rule ID) this collector connects to")
    collector_parser.add_argument("--credential", help="Ingestion credential ID, when several qualify")
    collector_parser.add_argument("--self-monitoring", action="store_true", help="Also scrape backends, gateway, and auth service")
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
    init_ca_parser = subparsers.add_parser("init-ca")
    init_ca_parser.add_argument("--valid-days", type=int, required=True)
    init_ca_parser.add_argument("--confirm-local-ca", action="store_true")
    for trust_parser in (generate_credential_parser, init_ca_parser):
        trust_parser.add_argument("--recipient", action="append", default=[], help="Repeat for multiple age recipients")
        trust_parser.add_argument("--confirm-production-recipients", action="store_true")
    issue_parser = subparsers.add_parser("issue-certificate")
    issue_target = issue_parser.add_mutually_exclusive_group(required=True)
    issue_target.add_argument("--credential", help="Credential that declares the certificate identity")
    issue_target.add_argument("--server", action="store_true", help="Issue the gateway server certificate")
    issue_parser.add_argument("--valid-days", type=int, help="Required; there is no default validity")
    issue_parser.add_argument("--output-dir", type=Path, required=True)
    for trust_parser in (generate_credential_parser, init_ca_parser, issue_parser):
        trust_parser.add_argument("--config", type=Path, required=True)
        trust_parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
        trust_parser.add_argument("--root", type=Path, default=ROOT)
        trust_parser.add_argument("--versions", type=Path, default=ROOT / "config" / "versions.yaml")

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

    args = parser.parse_args(argv)

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
                    grafana.desired_state(platform), grafana.http_client(args.url, args.admin_user, password),
                    grafana.secret_reader(platform, args.secrets_dir),
                    dry_run=args.dry_run, prune=args.prune, update_secrets=args.update_secrets,
                )
                for line in changes.lines(args.dry_run):
                    print(line)
            return 0
        except (ConfigurationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command in ("generate-credential", "init-ca", "issue-certificate"):
        try:
            ensure_doctor_ok(load_versions(args.versions))
            platform = load_platform(args.config, network=args.network)
            if args.command == "generate-credential":
                target = trust.generate_credential(
                    platform, args.credential, args.recipient, root=args.root,
                    confirm_production_recipients=args.confirm_production_recipients,
                )
                print(f"Generated and encrypted the secret for {args.credential} in {target}")
            elif args.command == "init-ca":
                fingerprint = trust.init_ca(
                    platform, args.recipient, args.valid_days, root=args.root,
                    confirm_local_ca=args.confirm_local_ca,
                    confirm_production_recipients=args.confirm_production_recipients,
                )
                print(f"Created the gateway client CA (SHA-256 fingerprint {fingerprint})")
            else:
                issued = trust.issue_certificate(
                    platform, args.output_dir, args.valid_days, credential_id=args.credential,
                    server=args.server, root=args.root,
                )
                print(f"Issued {issued.certificate_path} with key {issued.key_path}")
                print(f"SHA-256 fingerprint: {issued.fingerprint}")
            return 0
        except (ConfigurationError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.command == "doctor":
        try:
            matrix = load_versions(args.versions)
        except ConfigurationError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        checks = doctor(matrix)
        for check in checks:
            print(f"{'ok' if check.ok else 'FAIL'}: {check.detail}")
        return 0 if all(check.ok for check in checks) else 1

    if args.command in (
        "generate-recipient", "encrypt-secret", "rotate-secret", "materialize-secrets", "clean-secrets",
    ):
        try:
            ensure_doctor_ok(load_versions(args.versions))
            if args.command == "generate-recipient":
                recipient = generate_recipient(args.output, overwrite=args.overwrite)
                print(recipient)
            elif args.command == "encrypt-secret":
                value = args.value if args.value is not None else sys.stdin.read().rstrip("\n")
                target = encrypt_secret(
                    args.file, args.key, value, args.recipient, secrets_dir=args.secrets_dir,
                    profile=args.profile, confirm_production_recipients=args.confirm_production_recipients,
                )
                print(f"Encrypted {target}")
            elif args.command == "rotate-secret":
                value = args.value if args.value is not None else sys.stdin.read().rstrip("\n")
                target = rotate_secret(args.file, args.key, value, secrets_dir=args.secrets_dir)
                print(f"Rotated {target}")
            elif args.command == "materialize-secrets":
                platform = materialize(
                    args.config, root=args.root, output_dir=args.output_dir,
                    confirm_production_recipients=args.confirm_production_recipients,
                )
                print(f"Materialized {len(platform.secrets)} secret(s) into {args.output_dir}")
            else:
                cleanup(args.output_dir)
                print(f"Cleaned {args.output_dir}")
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
            artifacts["grafana/desired-state.json"] = (
                json.dumps(grafana.desired_state(platform), indent=2, sort_keys=True) + "\n"
            )
            for name, content in artifacts.items():
                (output / name).parent.mkdir(mode=0o700, exist_ok=True)
                with (output / name).open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
            print(f"Rendered non-secret contracts to {output}; no deployment was generated.")
        else:
            print(f"Valid contracts: {len(platform.streams)} datastream(s), {len(rules)} network rule(s).")
        return 0
    except (ConfigurationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
