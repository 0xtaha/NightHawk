from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from nighthawk.__main__ import main
from nighthawk.authz import RequestFacts, decide, load_policy
from nighthawk.collector import PROFILES
from nighthawk.config import ROOT, load_platform
from tests.fakes import basic, materialize_plain

EXAMPLE = ROOT / "config" / "tenants.example.yaml"


class CrossArtifactTests(unittest.TestCase):
    """Everything rendered from the example document must describe the same gateway and tenant."""

    def test_rendered_artifacts_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root = Path(temp)
            platform = load_platform(EXAMPLE)
            backend_id = platform.streams[0].backend_id
            hostname = platform.gateway.hostname
            self.assertEqual(main(["render-contracts", "--config", str(EXAMPLE), "--output", str(root / "contracts")]), 0)
            secrets = materialize_plain(platform, root / "materialized")
            self.assertEqual(main([
                "render-gateway-policy", "--config", str(EXAMPLE), "--secrets-dir", str(secrets),
                "--output", str(root / "policy.json"),
            ]), 0)

            static = yaml.safe_load((root / "contracts/gateway/traefik-static.yaml").read_text())
            ports = {
                name: int(entry["address"].lstrip(":"))
                for name, entry in static["entryPoints"].items() if name != "metrics"
            }
            policy = load_policy(root / "policy.json")
            self.assertEqual(set(policy.entry_points), set(ports))
            dynamic = yaml.safe_load((root / "contracts/gateway/traefik-dynamic.yaml").read_text())
            for router in dynamic["http"]["routers"].values():
                expected = platform.grafana.hostname if router["service"] == "grafana-ui" else hostname
                self.assertIn(f"Host(`{expected}`)", router["rule"])
                self.assertLessEqual(set(router["entryPoints"]), set(ports))

            # Collectors use the local entry point; every profile targets a listening port with a known credential.
            local = platform.gateway.entry_point("local-gateway")
            for profile in (name for name, item in PROFILES.items() if not item.privileged):
                output = root / f"collector-{profile}"
                self.assertEqual(main([
                    "render-collector", "--config", str(EXAMPLE), "--tenant", "example", "--datastream", "application",
                    "--profile", profile, "--entry-point", "local-gateway", "--output", str(output),
                ]), 0)
                generated = (output / "datastream.alloy").read_text()
                urls = set(re.findall(r'"(https://[^"/]+)', generated))
                self.assertEqual(urls, {f"https://{hostname}:{local.port}"})
                (username,) = set(re.findall(r'username\s+= "([^"]+)"', generated))
                decision = decide(policy, RequestFacts(local.name, "logs", "ingest", (basic(username),)))
                # The example collector is certificate-bound, so the credential alone is not enough.
                self.assertEqual(decision.status, 403)
                self.assertEqual(policy.credentials[username].backend_id, backend_id)

            for backend in ("mimir", "loki", "tempo", "pyroscope"):
                overrides = yaml.safe_load((root / f"contracts/{backend}-overrides.yaml").read_text())
                self.assertEqual(list(overrides["overrides"]), [backend_id])

            state = json.loads((root / "contracts/grafana/desired-state.json").read_text())
            grafana = platform.gateway.entry_point(platform.gateway.grafana_entry_point)
            for datasource in state["organizations"][0]["datasources"]:
                self.assertTrue(datasource["url"].startswith(f"https://{hostname}:{grafana.port}/"))
                signal = datasource["name"].split()[1]
                decision = decide(policy, RequestFacts(grafana.name, signal, "query", (basic(datasource["basicAuthUser"]),)))
                self.assertEqual((decision.status, decision.backend_id), (200, backend_id))
                # Each data source path is one the gateway routes for that signal's query permission.
                path = datasource["url"].split(f":{grafana.port}", 1)[1]
                rules = [
                    router["rule"] for name, router in dynamic["http"]["routers"].items()
                    if name.startswith(grafana.name) and name.endswith(f"{signal}-query")
                ]
                self.assertEqual(len(rules), 1)
                self.assertIn(path + "/", rules[0])


if __name__ == "__main__":
    unittest.main()
