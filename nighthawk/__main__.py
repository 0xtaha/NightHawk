"""Validate and render non-secret intermediate contracts, not deployments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from nighthawk.config import (
    ConfigurationError, ROOT, load_network, load_platform, validate_migration,
)
from nighthawk.retention import pyroscope_overrides


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "render-contracts"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--network", type=Path, default=ROOT / "config" / "network.yaml")
    parser.add_argument(
        "--previous", type=Path, action="append", default=[],
        help="Historical platform YAML for migration comparison; repeat for retained history",
    )
    parser.add_argument("--storage-output", type=Path, help="Storage bindings from 'terraform output -json storage'")
    parser.add_argument("--output", type=Path, help="New directory for non-secret contract artifacts")
    args = parser.parse_args(argv)
    if args.command == "render-contracts" and args.output is None:
        parser.error("render-contracts requires --output")
    if args.command == "validate" and args.output is not None:
        parser.error("--output is only valid with render-contracts")
    try:
        platform = load_platform(args.config, args.storage_output)
        rules = load_network(args.network)
        for previous in args.previous:
            validate_migration(platform, load_platform(previous))
        profile_overrides = pyroscope_overrides(platform)
        if args.command == "render-contracts":
            output = args.output
            output.mkdir(parents=True, mode=0o700, exist_ok=False)
            artifacts = {
                "platform.json": json.dumps(platform.manifest(), indent=2, sort_keys=True) + "\n",
                "network.json": json.dumps({"schema_version": 1, "rules": rules}, indent=2, sort_keys=True) + "\n",
                "pyroscope-overrides.yaml": yaml.safe_dump(profile_overrides, sort_keys=True),
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
            for name, content in artifacts.items():
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
