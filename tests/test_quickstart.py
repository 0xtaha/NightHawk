from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from nighthawk import quickstart
from nighthawk.config import ConfigurationError, load_versions
from tests.fakes import FakeCompletedProcess, example_document, fake_sops, write_document


class FakeHost:
    """Stands in for sops, age, the container CLI, and Compose."""

    def __init__(self, podman: bool = False) -> None:
        versions = load_versions()["secrets_tools"]
        self.versions = {"sops": versions["sops"]["version"], "age": versions["age"]["version"]}
        self.podman = podman
        self.missing: set[str] = set()
        self.running = False
        self.fail: str | None = None
        self.ps: list[dict] = []
        self.compose_calls: list[list[str]] = []
        self.sops_writes = 0
        self.keygens = 0

    def __call__(self, args, **kwargs):
        tool = args[0]
        if tool in self.missing:
            raise FileNotFoundError(tool)
        if tool in ("sops", "age") and args[1:] == ["--version"]:
            return FakeCompletedProcess(0, stdout=f"{tool} {self.versions[tool]}\n")
        if tool == "age-keygen":
            self.keygens += 1
            Path(args[2]).write_text("# public key: age1quickstartrecipient\nAGE-SECRET-KEY-1TEST\n", encoding="utf-8")
            return FakeCompletedProcess(0, stderr="Public key: age1quickstartrecipient\n")
        if tool == "sops":
            if "--decrypt" not in args:
                self.sops_writes += 1
            return fake_sops(args, **kwargs)
        if args[:2] == ["docker", "compose"]:
            return self.compose(args[2:])
        if args[:2] == ["docker", "version"]:
            return FakeCompletedProcess(0, stdout="Podman Engine Conmon " if self.podman else "Engine containerd ")
        if args[:2] == ["docker", "info"]:
            return FakeCompletedProcess(0, stdout="[name=seccomp name=rootless]" if self.podman else "[name=seccomp]")
        raise AssertionError(f"unexpected command: {args}")

    def compose(self, args: list[str]) -> FakeCompletedProcess:
        if args == ["version"]:
            return FakeCompletedProcess(0, stdout="Docker Compose version 5")
        self.compose_calls.append(args)
        action = [item for item in args if not item.startswith("-")]
        if "ps" in args and "--quiet" in args:
            return FakeCompletedProcess(0, stdout="abc123\n" if self.running else "")
        if "ps" in args:
            return FakeCompletedProcess(0, stdout="\n".join(json.dumps(item) for item in self.ps))
        if self.fail and self.fail in args:
            return FakeCompletedProcess(1, stderr=f"{self.fail} failed\n")
        if "up" in args:
            self.running = True
        if "down" in args:
            self.running = False
        return FakeCompletedProcess(0, stdout=" ".join(action))

    def verbs(self) -> list[str]:
        verbs = []
        for call in self.compose_calls:
            verbs.append(next(item for item in call if item in ("build", "up", "run", "kill", "down", "ps")))
        return verbs


class QuickstartFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = example_document()
        self.host = FakeHost()
        self.lines: list[str] = []
        self.port_free = True

    def options(self) -> quickstart.Options:
        return quickstart.Options(
            config=write_document(self.root, self.data), root=self.root, rendered_dir=self.root / "rendered",
            secrets_dir=self.root / "materialized", age_key=self.root / "secrets" / "local.agekey",
            tools_dir=self.root / "tools",
        )

    def run_quickstart(self, options: quickstart.Options | None = None) -> quickstart.Options:
        options = options or self.options()
        quickstart.quickstart(
            options, runner=self.host, out=self.lines.append, port_is_free=lambda address, port: self.port_free,
        )
        return options

    def created_files(self) -> list[Path]:
        return [path for path in self.root.rglob("*") if path.is_file() and path.name != "platform.yaml"]


class PreflightTests(QuickstartFixture):
    def test_missing_prerequisites_are_named_and_nothing_is_created(self) -> None:
        self.host.missing = {"sops"}
        self.host.versions["age"] = "0.0.1"
        self.port_free = False
        with self.assertRaises(ConfigurationError) as raised:
            self.run_quickstart()
        message = str(raised.exception)
        self.assertIn("sops is not available", message)
        self.assertIn("age", message)
        self.assertIn("fetch-tools", message)
        self.assertEqual(self.created_files(), [])
        self.assertEqual(self.host.compose_calls, [])

    def test_missing_runtime_and_busy_port_are_named(self) -> None:
        self.host.missing = {"docker"}
        with self.assertRaisesRegex(ConfigurationError, "Compose command .* is not installed"):
            self.run_quickstart()
        self.host.missing = set()
        self.port_free = False
        with self.assertRaisesRegex(ConfigurationError, "port 127.0.0.1:8443 is already in use"):
            self.run_quickstart()
        self.assertEqual(self.created_files(), [])

    def test_only_a_docker_document_is_accepted(self) -> None:
        self.data["deployment"] = "self-hosted-k8s"
        with self.assertRaisesRegex(ConfigurationError, "needs a docker platform document"):
            self.run_quickstart()


class BringUpTests(QuickstartFixture):
    def test_first_run_creates_each_missing_item_once_and_starts_the_stack(self) -> None:
        options = self.run_quickstart()
        stored = yaml.safe_load((self.root / "secrets" / "local.sops.yaml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(key for key in stored if key != "sops"), sorted(self.data["secrets"]))
        self.assertEqual(self.host.keygens, 1)
        self.assertEqual(self.host.sops_writes, len(self.data["secrets"]))
        self.assertEqual(sorted(options.created), sorted([
            "age key", "credential example-ingest", "credential example-query", "Grafana admin password",
            "certificate authority", "storage trust storage-ca", "storage identity mimir-storage",
            "storage identity loki-storage", "storage identity tempo-storage", "storage identity pyroscope-storage",
            "certificate gateway-server", "certificate storage-server", "certificate example-ingest",
        ]))
        self.assertEqual(stored["storage-ca"], stored["gateway-client-ca"])
        self.assertEqual(self.host.verbs(), ["build", "up", "run", "run"])
        up = self.host.compose_calls[1]
        self.assertEqual(up[-2:], ["grafana", "alloy"])
        self.assertIn("--wait", up)
        self.assertEqual(self.host.compose_calls[2][-1], "wait-alloy")
        self.assertEqual(self.host.compose_calls[3][-1], "grafana-init")
        text = "\n".join(self.lines)
        self.assertIn("https://gateway.nighthawk.internal:8443", text)
        self.assertIn("https://grafana.nighthawk.internal:8443", text)
        self.assertIn("example/application", text)
        self.assertIn("examples, not production defaults", text)
        self.assertEqual(text.count("Grafana admin password (shown once)"), 1)
        self.assertIn(stored["grafana-admin"], text)
        for secret in ("example-ingest", "mimir-storage", "gateway-client-ca-key"):
            self.assertNotIn(stored[secret], text)

    def test_rendered_and_runtime_trees_are_complete_and_owner_only_where_secret(self) -> None:
        options = self.run_quickstart()
        rendered, runtime = options.rendered_dir, options.secrets_dir / "runtime"
        for path in (
            "contracts/backends/mimir.yaml", "contracts/gateway/routes.md", "overrides/tempo-overrides.yaml",
            "platform/platform.yaml", "collector/datastream.alloy", "compose.env",
        ):
            self.assertTrue((rendered / path).is_file(), path)
        self.assertEqual(sorted(path.name for path in (rendered / "collector").iterdir()), ["datastream.alloy"])
        for path in (
            "gateway/client-ca.pem", "gateway/gateway-server.crt.pem", "gateway/gateway-server.key.pem",
            "gateway/traefik-static.yaml", "gateway/traefik-dynamic.yaml", "storage/s3.json", "storage/buckets.txt",
            "storage/storage-server.crt.pem", "storage/storage-server.key.pem", "storage-trust/ca.pem",
            "backends/mimir.env", "backends/loki.env", "backends/tempo.env", "backends/pyroscope.env",
            "authz/policy.json", "grafana/admin-password", "grafana/gateway-ca.pem", "collector/credential",
            "collector/gateway-ca.pem", "collector/client.crt.pem", "collector/client.key.pem",
        ):
            self.assertTrue((runtime / path).is_file(), path)
            self.assertEqual((runtime / path).stat().st_mode & 0o077, 0, path)
        # Backends see the storage CA but neither the storage key nor the identity file.
        self.assertEqual(sorted(path.name for path in (runtime / "storage-trust").iterdir()), ["ca.pem"])
        environment = dict(
            line.split("=", 1) for line in (rendered / "compose.env").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(environment["NIGHTHAWK_BIND_ADDRESS"], "127.0.0.1")
        self.assertEqual(environment["NIGHTHAWK_SECRETS_DIR"], str(options.secrets_dir.resolve()))
        stored = yaml.safe_load((self.root / "secrets" / "local.sops.yaml").read_text(encoding="utf-8"))
        for path in rendered.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                for key in ("example-ingest", "example-query", "grafana-admin"):
                    self.assertNotIn(stored[key], content, path)

    def test_rootless_podman_runs_services_as_container_root(self) -> None:
        self.host = FakeHost(podman=True)
        options = self.run_quickstart()
        environment = (options.rendered_dir / "compose.env").read_text(encoding="utf-8")
        self.assertIn("NIGHTHAWK_UID=0\n", environment)
        self.assertIn("NIGHTHAWK_GID=0\n", environment)

    def test_second_run_creates_nothing_and_rewrites_nothing(self) -> None:
        first = self.run_quickstart()
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.created_files()}
        writes, keygens = self.host.sops_writes, self.host.keygens
        self.lines.clear()
        self.host.compose_calls.clear()
        second = self.options()
        self.run_quickstart(second)
        self.assertEqual(second.created, [])
        self.assertEqual((self.host.sops_writes, self.host.keygens), (writes, keygens))
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.created_files()}
        materialized = first.secrets_dir / "secrets"
        self.assertEqual(
            {path: value for path, value in before.items() if materialized not in path.parents},
            {path: value for path, value in after.items() if materialized not in path.parents},
        )
        self.assertIn("Created this run: nothing new", "\n".join(self.lines))
        self.assertNotIn("shown once", "\n".join(self.lines))
        # The stack was already running and nothing changed, so nothing is told to reload.
        self.assertNotIn("kill", self.host.verbs())

    def test_changed_contract_reloads_what_does_not_watch_its_files(self) -> None:
        self.run_quickstart()
        self.host.compose_calls.clear()
        self.data["secrets"]["example-ingest-next"] = {"file": "secrets/local.sops.yaml", "key": "example-ingest-next"}
        self.data["credentials"].append({
            "id": "example-ingest-next", "secret_ref": "example-ingest-next",
            "tenant": "example", "datastream": "application", "permission": "ingest",
            "certificate_identity": "spiffe://nighthawk/example/application/collector-next",
        })
        self.data["credentials"][0]["id"] = "example-ingest"
        self.data["credentials"] = [item for item in self.data["credentials"] if item["id"] != "example-ingest"]
        options = self.run_quickstart()
        self.assertIn("credential example-ingest-next", options.created)
        kills = [call[-1] for call in self.host.compose_calls if "kill" in call]
        self.assertEqual(kills, ["authz", "alloy"])
        policy = json.loads((options.secrets_dir / "runtime" / "authz" / "policy.json").read_text(encoding="utf-8"))
        self.assertNotIn("example-ingest", policy["credentials"])
        self.assertIn("example-ingest-next", (options.rendered_dir / "collector" / "datastream.alloy").read_text())

    def test_unhealthy_service_is_named(self) -> None:
        self.host.fail = "up"
        self.host.ps = [
            {"Service": "mimir", "State": "running", "Health": ""},
            {"Service": "tempo", "State": "exited", "ExitCode": 1},
            {"Service": "grafana", "State": "running", "Health": "unhealthy"},
            {"Service": "storage-init", "State": "exited", "ExitCode": 0},
        ]
        with self.assertRaises(ConfigurationError) as raised:
            self.run_quickstart()
        self.assertIn("starting the stack failed", str(raised.exception))
        self.assertIn("not healthy: grafana (unhealthy), tempo (exited)", str(raised.exception))

    def test_unknown_secret_reference_is_reported(self) -> None:
        self.data["secrets"]["mystery"] = {"file": "secrets/local.sops.yaml", "key": "mystery"}
        with self.assertRaisesRegex(ConfigurationError, "cannot create these secrets: mystery"):
            self.run_quickstart()


class TeardownTests(QuickstartFixture):
    def test_ordinary_teardown_keeps_volumes(self) -> None:
        options = self.run_quickstart()
        self.host.compose_calls.clear()
        quickstart.teardown(options, runner=self.host, out=self.lines.append)
        (down,) = self.host.compose_calls
        self.assertIn("down", down)
        self.assertNotIn("--volumes", down)
        self.assertNotIn("-v", down)
        self.assertIn("Volumes, secrets, and certificates were kept.", self.lines)
        self.assertTrue((self.root / "secrets" / "local.sops.yaml").exists())

    def test_purge_requires_confirmation(self) -> None:
        options = self.run_quickstart()
        self.host.compose_calls.clear()
        with self.assertRaisesRegex(ConfigurationError, "confirm it explicitly"):
            quickstart.teardown(options, purge=True, runner=self.host, out=self.lines.append)
        self.assertEqual(self.host.compose_calls, [])
        quickstart.teardown(options, purge=True, confirmed=True, runner=self.host, out=self.lines.append)
        self.assertIn("--volumes", self.host.compose_calls[0])
        self.assertIn("Deleted all NightHawk volumes.", self.lines)

    def test_teardown_without_a_stack_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "nothing to tear down"):
            quickstart.teardown(self.options(), runner=self.host, out=self.lines.append)


if __name__ == "__main__":
    unittest.main()
