from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nighthawk import quickstart
from nighthawk.config import ConfigurationError, load_platform
from nighthawk.vault import pki_roles
from tests.fakes import TOKEN, FakeCompletedProcess, FakeVault, example_document, write_document


class FakeHost:
    """Stands in for the container CLI and Compose."""

    def __init__(self, podman: bool = False) -> None:
        self.podman = podman
        self.missing: set[str] = set()
        self.running = False
        self.fail: str | None = None
        self.ps: list[dict] = []
        self.volumes: list[str] = []
        self.compose_calls: list[list[str]] = []
        self.environments: list[dict] = []

    def __call__(self, args, **kwargs):
        tool = args[0]
        self.environments.append(dict(kwargs.get("env") or {}))
        if tool in self.missing:
            raise FileNotFoundError(tool)
        if args[:2] == ["docker", "compose"]:
            return self.compose(args[2:])
        if args[:2] == ["docker", "version"]:
            return FakeCompletedProcess(0, stdout="Podman Engine Conmon " if self.podman else "Engine containerd ")
        if args[:2] == ["docker", "info"]:
            return FakeCompletedProcess(0, stdout="[name=seccomp name=rootless]" if self.podman else "[name=seccomp]")
        if args[:3] == ["docker", "volume", "ls"]:
            return FakeCompletedProcess(0, stdout="".join(f"{name}\n" for name in [*self.volumes, "unrelated_data"]))
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
            self.volumes = ["nighthawk_mimir-data", "nighthawk_seaweedfs-data"]
        if "down" in args:
            self.running = False
            if "--volumes" in args:
                self.volumes = []
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
        self.vault = FakeVault(load_platform(write_document(self.root, self.data)))
        self.environ = {"VAULT_TOKEN": TOKEN, "PATH": "/usr/bin"}
        self.lines: list[str] = []
        self.port_free = True

    def options(self) -> quickstart.Options:
        return quickstart.Options(
            config=write_document(self.root, self.data), root=self.root, rendered_dir=self.root / "rendered",
            secrets_dir=self.root / "materialized", environ=self.environ, vault_transport=self.vault,
        )

    def run_quickstart(self, options: quickstart.Options | None = None) -> quickstart.Options:
        options = options or self.options()
        quickstart.quickstart(
            options, runner=self.host, out=self.lines.append, port_is_free=lambda address, port: self.port_free,
        )
        return options

    def created_files(self) -> list[Path]:
        return [path for path in self.root.rglob("*") if path.is_file() and path.name != "platform.yaml"]

    def stored(self) -> dict[str, str]:
        return self.vault.values("nighthawk/local")

    def signed(self) -> int:
        return sum(1 for _, path, _ in self.vault.calls if "/sign/" in path)

    def assert_nothing_happened(self) -> None:
        self.assertEqual(self.created_files(), [])
        self.assertEqual(self.host.compose_calls, [])
        self.assertEqual(self.vault.kv, {})
        self.assertEqual(self.signed(), 0)


class PreflightTests(QuickstartFixture):
    def test_unreachable_vault_and_busy_port_are_named_and_nothing_is_created(self) -> None:
        self.vault.reachable = False
        with self.assertRaises(ConfigurationError) as raised:
            self.run_quickstart()
        message = str(raised.exception)
        self.assertIn("Vault at http://127.0.0.1:8200 is unreachable", message)
        self.assertIn("bootstrap-dev-vault", message)
        self.assertIn("docs/00-quickstart.md", message)
        self.assert_nothing_happened()

    def test_missing_credential_points_to_the_development_vault_steps(self) -> None:
        del self.environ["VAULT_TOKEN"]
        with self.assertRaises(ConfigurationError) as raised:
            self.run_quickstart()
        self.assertIn("no Vault credential", str(raised.exception))
        self.assertIn("bootstrap-dev-vault", str(raised.exception))
        self.assert_nothing_happened()

    def test_each_vault_prerequisite_is_named(self) -> None:
        cases = {
            "is sealed": lambda: setattr(self.vault, "sealed", True),
            "outside the supported range": lambda: setattr(self.vault, "version", "1.15.0"),
            "the credential was rejected": lambda: self.environ.update(VAULT_TOKEN="unknown-token"),
            "PKI role nighthawk-collector does not exist": lambda: self.vault.roles.pop("nighthawk-collector"),
            "key-value mount nighthawk-kv does not exist": lambda: self.vault.mounts.pop("nighthawk-kv"),
            "has no certificate authority": lambda: setattr(self.vault, "ca_certificate", None),
        }
        for expected, break_it in cases.items():
            with self.subTest(expected=expected):
                self.setUp()
                break_it()
                with self.assertRaisesRegex(ConfigurationError, expected):
                    self.run_quickstart()
                self.assert_nothing_happened()

    def test_role_that_would_refuse_a_declared_name_is_reported_before_anything_is_created(self) -> None:
        self.vault.roles["nighthawk-server"]["allowed_domains"] = ["gateway.nighthawk.internal"]
        with self.assertRaisesRegex(ConfigurationError, "does not allow grafana.nighthawk.internal, seaweedfs; apply the rendered"):
            self.run_quickstart()
        self.assert_nothing_happened()

    def test_missing_runtime_and_busy_port_are_named(self) -> None:
        self.host.missing = {"docker"}
        with self.assertRaisesRegex(ConfigurationError, "Compose command .* is not installed"):
            self.run_quickstart()
        self.host.missing = set()
        self.port_free = False
        with self.assertRaisesRegex(ConfigurationError, "port 127.0.0.1:8443 is already in use"):
            self.run_quickstart()
        self.assert_nothing_happened()

    def test_non_loopback_bind_address_is_refused_before_anything_else(self) -> None:
        for address in ("0.0.0.0", "192.168.1.10", "::", "localhost", ""):
            with self.subTest(address=address):
                options = self.options()
                options.bind_address = address
                with self.assertRaises(ConfigurationError) as raised:
                    self.run_quickstart(options)
                self.assertIn("is not a loopback address", str(raised.exception))
                self.assertIn("accepts ingestion without a client certificate", str(raised.exception))
                self.assert_nothing_happened()
        self.assertEqual(self.vault.calls, [])
        self.assertEqual(self.host.environments, [])

    def test_loopback_addresses_are_accepted(self) -> None:
        for address in ("127.0.0.1", "::1", "127.0.0.2"):
            with self.subTest(address=address):
                self.setUp()
                options = self.options()
                options.bind_address = address
                self.run_quickstart(options)
                environment = (options.rendered_dir / "compose.env").read_text(encoding="utf-8")
                self.assertIn(f"NIGHTHAWK_BIND_ADDRESS={address}\n", environment)
                self.assertIn(f"published on {address}:8443", "\n".join(self.lines))

    def test_published_port_is_the_local_entry_points_declared_port(self) -> None:
        import yaml
        from nighthawk.config import ROOT

        options = self.run_quickstart()
        environment = (options.rendered_dir / "compose.env").read_text(encoding="utf-8")
        self.assertIn("NIGHTHAWK_GATEWAY_PORT=8443\n", environment)
        # A network contract that declares another port for the local entry point.
        network = yaml.safe_load((ROOT / "config" / "network.yaml").read_text(encoding="utf-8"))
        next(rule for rule in network["rules"] if rule["id"] == "local-gateway")["port"] = 9443
        path = self.root / "network.yaml"
        path.write_text(yaml.safe_dump(network), encoding="utf-8")
        platform = load_platform(write_document(self.root, self.data), network=path)
        self.assertEqual(quickstart.local_entry_point(platform).port, 9443)

    def test_document_without_a_loopback_entry_point_is_refused(self) -> None:
        self.data["gateway"]["entry_points"] = ["remote-gateway"]
        self.data["gateway"]["grafana_entry_point"] = "remote-gateway"
        with self.assertRaisesRegex(ConfigurationError, "exactly one loopback-scoped gateway entry point"):
            self.run_quickstart()

    def test_only_a_docker_document_is_accepted(self) -> None:
        self.data["deployment"] = "self-hosted-k8s"
        with self.assertRaisesRegex(ConfigurationError, "needs a docker platform document"):
            self.run_quickstart()


class BringUpTests(QuickstartFixture):
    def test_first_run_creates_each_missing_item_once_and_starts_the_stack(self) -> None:
        options = self.run_quickstart()
        stored = self.stored()
        self.assertEqual(sorted(stored), sorted(self.data["secrets"]))
        # One check-and-set write per secret: nothing was written twice or replaced.
        self.assertEqual(len(self.vault.kv["nighthawk/local"]), len(self.data["secrets"]))
        self.assertEqual(sorted(options.created), sorted([
            "credential example-ingest", "credential example-query", "Grafana admin password",
            "storage identity mimir-storage", "storage identity loki-storage", "storage identity tempo-storage",
            "storage identity pyroscope-storage",
            "certificate gateway-server", "certificate storage-server", "certificate example-ingest",
        ]))
        self.assertEqual(self.signed(), 3)
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
        for secret in ("example-ingest", "mimir-storage"):
            self.assertNotIn(stored[secret], text)

    def test_rendered_and_runtime_trees_are_complete_and_owner_only_where_secret(self) -> None:
        options = self.run_quickstart()
        rendered, runtime = options.rendered_dir, options.secrets_dir / "runtime"
        for path in (
            "contracts/backends/mimir.yaml", "contracts/gateway/routes.md", "overrides/tempo-overrides.yaml",
            "contracts/vault/policy.hcl", "contracts/vault/pki-roles.json",
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
        stored = self.stored()
        for path in rendered.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                for key in ("example-ingest", "example-query", "grafana-admin"):
                    self.assertNotIn(stored[key], content, path)

    def test_trust_is_the_public_certificate_of_the_authority_in_vault(self) -> None:
        options = self.run_quickstart()
        runtime = options.secrets_dir / "runtime"
        authority = self.vault.ca_pem()
        for path in ("gateway/client-ca.pem", "grafana/gateway-ca.pem", "collector/gateway-ca.pem", "storage-trust/ca.pem"):
            self.assertEqual((runtime / path).read_text(encoding="utf-8").strip(), authority.strip(), path)
        # The only private keys on disk are the three leaf keys issued by this run.
        keys = sorted(
            str(path.relative_to(runtime)) for path in self.created_files()
            if "PRIVATE KEY" in path.read_text(encoding="utf-8", errors="replace")
        )
        self.assertEqual(keys, [
            "collector-certificates/example-ingest.key.pem", "collector/client.key.pem",
            "gateway/gateway-server.key.pem", "storage/storage-server.key.pem",
        ])
        # Nothing asked Vault for a key, and no signing request carried one.
        for _, path, body in self.vault.calls:
            self.assertNotIn("PRIVATE KEY", json.dumps(body or {}), path)

    def test_vault_credential_is_never_written_or_passed_to_child_processes(self) -> None:
        self.run_quickstart()
        for path in self.created_files():
            self.assertNotIn(TOKEN, path.read_text(encoding="utf-8", errors="replace"), path)
        self.assertTrue(self.host.environments)
        for environment in self.host.environments:
            self.assertNotIn("VAULT_TOKEN", environment)
            self.assertNotIn(TOKEN, " ".join(map(str, environment.values())))
        for call in self.host.compose_calls:
            self.assertNotIn(TOKEN, " ".join(call))
        self.assertNotIn(TOKEN, self.vault.sent())
        self.assertNotIn(TOKEN, "\n".join(self.lines))

    def test_rootless_podman_runs_services_as_container_root(self) -> None:
        self.host = FakeHost(podman=True)
        options = self.run_quickstart()
        environment = (options.rendered_dir / "compose.env").read_text(encoding="utf-8")
        self.assertIn("NIGHTHAWK_UID=0\n", environment)
        self.assertIn("NIGHTHAWK_GID=0\n", environment)

    def test_second_run_creates_nothing_and_rewrites_nothing(self) -> None:
        self.run_quickstart()
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.created_files()}
        versions, signed = len(self.vault.kv["nighthawk/local"]), self.signed()
        self.lines.clear()
        self.host.compose_calls.clear()
        second = self.options()
        self.run_quickstart(second)
        self.assertEqual(second.created, [])
        self.assertEqual((len(self.vault.kv["nighthawk/local"]), self.signed()), (versions, signed))
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.created_files()}
        self.assertEqual(before, after)
        self.assertIn("Created this run: nothing new", "\n".join(self.lines))
        self.assertNotIn("shown once", "\n".join(self.lines))
        self.assertNotIn("certificate authority in Vault changed", "\n".join(self.lines))
        # The stack was already running and nothing changed, so nothing is told to reload.
        self.assertNotIn("kill", self.host.verbs())

    def test_changed_contract_reloads_what_does_not_watch_its_files(self) -> None:
        self.run_quickstart()
        self.host.compose_calls.clear()
        self.data["secrets"]["example-ingest-next"] = {"path": "nighthawk/local", "key": "example-ingest-next"}
        self.data["credentials"].append({
            "id": "example-ingest-next", "secret_ref": "example-ingest-next",
            "tenant": "example", "datastream": "application", "permission": "ingest",
            "certificate_identity": "spiffe://nighthawk/example/application/collector-next",
        })
        self.data["credentials"] = [item for item in self.data["credentials"] if item["id"] != "example-ingest"]
        # Until the operator applies the re-rendered role, the new identity cannot be signed.
        with self.assertRaisesRegex(ConfigurationError, "does not allow spiffe://nighthawk/example/application/collector-next"):
            self.run_quickstart()
        self.assertEqual(self.host.compose_calls, [])
        self.vault.roles = pki_roles(load_platform(write_document(self.root, self.data)))
        options = self.run_quickstart()
        self.assertIn("credential example-ingest-next", options.created)
        kills = [call[-1] for call in self.host.compose_calls if "kill" in call]
        self.assertEqual(kills, ["authz", "alloy"])
        policy = json.loads((options.secrets_dir / "runtime" / "authz" / "policy.json").read_text(encoding="utf-8"))
        self.assertNotIn("example-ingest", policy["credentials"])
        self.assertIn("example-ingest-next", (options.rendered_dir / "collector" / "datastream.alloy").read_text())

    def test_recreated_vault_with_surviving_volumes_stops_before_generating_anything(self) -> None:
        self.run_quickstart()
        self.assertTrue(self.host.volumes)
        # A development Vault that restarted: same configuration, nothing stored.
        self.vault.kv.clear()
        self.host.compose_calls.clear()
        with self.assertRaises(ConfigurationError) as raised:
            self.run_quickstart()
        message = str(raised.exception)
        self.assertIn("storage identities (loki-storage, mimir-storage, pyroscope-storage, tempo-storage)", message)
        self.assertIn("nighthawk_mimir-data", message)
        self.assertNotIn("unrelated_data", message)
        self.assertIn("teardown-docker --purge --yes", message)
        self.assertEqual(self.vault.kv, {})
        self.assertEqual([call for call in self.host.compose_calls if "ps" not in call], [])
        # After the purge the quickstart generates everything again.
        quickstart.teardown(self.options(), purge=True, confirmed=True, runner=self.host, out=self.lines.append)
        options = self.run_quickstart()
        self.assertIn("storage identity mimir-storage", options.created)

    def test_replaced_authority_updates_trust_and_issues_certificates_again(self) -> None:
        first = self.run_quickstart()
        runtime = first.secrets_dir / "runtime"
        old_trust = (runtime / "gateway" / "client-ca.pem").read_text(encoding="utf-8")
        old_certificate = (runtime / "gateway" / "gateway-server.crt.pem").read_bytes()
        self.vault.new_authority()
        self.lines.clear()
        second = self.options()
        self.run_quickstart(second)
        self.assertNotEqual((runtime / "gateway" / "client-ca.pem").read_text(encoding="utf-8"), old_trust)
        self.assertNotEqual((runtime / "gateway" / "gateway-server.crt.pem").read_bytes(), old_certificate)
        self.assertEqual(sorted(second.created), [
            "certificate example-ingest", "certificate gateway-server", "certificate storage-server",
        ])
        text = "\n".join(self.lines)
        self.assertIn("certificate authority in Vault changed", text)
        self.assertIn("must be issued again", text)

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

    def test_secret_the_quickstart_cannot_generate_is_reported(self) -> None:
        self.data["secrets"]["mystery"] = {"path": "nighthawk/local", "key": "mystery"}
        with self.assertRaisesRegex(ConfigurationError, "store them with store-secret: mystery"):
            self.run_quickstart()


class CredentialChoiceTests(QuickstartFixture):
    """Overlapping credentials for the sample datastream, as during a rotation."""

    def declare(self, credential_id: str, permission: str, **extra) -> None:
        self.data["secrets"][credential_id] = {"path": "nighthawk/local", "key": credential_id}
        self.data["credentials"].append({
            "id": credential_id, "secret_ref": credential_id, "tenant": "example",
            "datastream": "application", "permission": permission, **extra,
        })
        self.vault.roles = pki_roles(load_platform(write_document(self.root, self.data)))

    def run_with(self, *credentials: str) -> quickstart.Options:
        options = self.options()
        options.credentials = credentials
        return self.run_quickstart(options)

    def provision_call(self) -> list[str]:
        return next(call for call in reversed(self.host.compose_calls) if "grafana-init" in call)

    def test_one_credential_per_permission_needs_no_choice(self) -> None:
        self.run_with()
        self.assertEqual(self.provision_call()[-3:], ["run", "--rm", "grafana-init"])

    def test_overlapping_ingestion_credentials_without_a_choice_stop_before_anything(self) -> None:
        self.declare("example-ingest-2", "ingest", certificate_identity="spiffe://nighthawk/example/application/collector-2")
        with self.assertRaisesRegex(ConfigurationError, "several ingestion credentials qualify \\(example-ingest, example-ingest-2\\); choose one explicitly"):
            self.run_with()
        self.assert_nothing_happened()
        self.assertEqual(self.vault.calls, [])

    def test_named_ingestion_credential_is_the_one_the_collector_uses(self) -> None:
        self.declare("example-ingest-2", "ingest", certificate_identity="spiffe://nighthawk/example/application/collector-2")
        options = self.run_with("example-ingest-2")
        self.assertIn("example-ingest-2", (options.rendered_dir / "collector" / "datastream.alloy").read_text())
        self.assertEqual(
            (options.secrets_dir / "runtime" / "collector" / "credential").read_text(), self.stored()["example-ingest-2"],
        )
        self.assertIn("certificate example-ingest-2", options.created)
        # Both credentials are accepted by the gateway during the overlap.
        policy = json.loads((options.secrets_dir / "runtime" / "authz" / "policy.json").read_text())
        self.assertLessEqual({"example-ingest", "example-ingest-2"}, set(policy["credentials"]))
        self.assertNotIn("--credential", self.provision_call())

    def test_overlapping_query_credentials_without_a_choice_stop_before_anything(self) -> None:
        self.declare("example-query-2", "query")
        with self.assertRaisesRegex(ConfigurationError, "several query credentials are declared \\(example-query, example-query-2\\)"):
            self.run_with()
        self.assert_nothing_happened()

    def test_named_query_credential_reaches_grafana_provisioning_and_the_rendered_state(self) -> None:
        self.declare("example-query-2", "query")
        options = self.run_with("example-query-2")
        self.assertEqual(self.provision_call()[-5:], ["run", "--rm", "grafana-init", "--credential", "example-query-2"])
        state = (options.rendered_dir / "contracts" / "grafana" / "desired-state.json").read_text()
        self.assertIn('"basicAuthUser": "example-query-2"', state)
        self.assertNotIn('"basicAuthUser": "example-query"', state)

    def test_one_choice_of_each_permission_together(self) -> None:
        self.declare("example-ingest-2", "ingest", certificate_identity="spiffe://nighthawk/example/application/collector-2")
        self.declare("example-query-2", "query")
        options = self.run_with("example-query-2", "example-ingest-2")
        self.assertIn("example-ingest-2", (options.rendered_dir / "collector" / "datastream.alloy").read_text())
        self.assertIn("example-query-2", self.provision_call())

    def test_wrong_names_are_refused_before_anything(self) -> None:
        from tests.fakes import add_stream
        add_stream(self.data, "example", "batch", "example-batch")
        self.vault.roles = pki_roles(load_platform(write_document(self.root, self.data)))
        cases = {
            ("missing",): "--credential missing: the platform document declares no such credential",
            ("example-batch-ingest",): "not an ingestion credential of the collector's datastream example/application",
        }
        for names, expected in cases.items():
            with self.subTest(names=names), self.assertRaisesRegex(ConfigurationError, expected):
                self.run_with(*names)
        self.assert_nothing_happened()
        self.declare("example-ingest-2", "ingest", certificate_identity="spiffe://nighthawk/example/application/collector-2")
        with self.assertRaisesRegex(ConfigurationError, "both example-ingest and example-ingest-2 were named for the collector"):
            self.run_with("example-ingest", "example-ingest-2")
        self.assert_nothing_happened()

    def test_query_credential_of_another_datastream_can_be_named(self) -> None:
        from tests.fakes import add_stream
        add_stream(self.data, "example", "batch", "example-batch")
        self.data["secrets"]["example-batch-query-2"] = {"path": "nighthawk/local", "key": "example-batch-query-2"}
        self.data["credentials"].append({
            "id": "example-batch-query-2", "secret_ref": "example-batch-query-2",
            "tenant": "example", "datastream": "batch", "permission": "query",
        })
        self.vault.roles = pki_roles(load_platform(write_document(self.root, self.data)))
        self.run_with("example-batch-query-2")
        self.assertEqual(self.provision_call()[-2:], ["--credential", "example-batch-query-2"])


class HostCollectionTests(QuickstartFixture):
    def run_with(self, requested: bool) -> quickstart.Options:
        options = self.options()
        options.host_collection = requested
        return self.run_quickstart(options)

    def up_call(self) -> list[str]:
        return next(call for call in self.host.compose_calls if "up" in call)

    def test_requested_renders_the_full_docker_profile_and_starts_the_service(self) -> None:
        options = self.run_with(True)
        directory = options.rendered_dir / "collector-host"
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["datastream.alloy", "logs.alloy", "metrics.alloy"])
        metrics = (directory / "metrics.alloy").read_text(encoding="utf-8")
        for setting in ('rootfs_path = "/rootfs"', 'procfs_path = "/rootfs/proc"', 'sysfs_path  = "/sys"'):
            self.assertIn(setting, metrics)
        generated = (directory / "datastream.alloy").read_text(encoding="utf-8")
        self.assertIn("example-ingest", generated)
        self.assertIn('prometheus.relabel "redact"', generated)
        up = self.up_call()
        self.assertEqual(up[up.index("--profile") + 1], "host-collection")
        self.assertEqual(up[-3:], ["grafana", "alloy", "alloy-host"])
        # The ordinary collector stays push-only.
        self.assertEqual(sorted(path.name for path in (options.rendered_dir / "collector").iterdir()), ["datastream.alloy"])

    def test_host_paths_match_the_compose_service_mounts(self) -> None:
        import yaml
        from nighthawk.quickstart import COMPOSE_FILE

        service = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]["alloy-host"]
        mounts = [volume for volume in service["volumes"] if isinstance(volume, str)]
        self.assertIn("/:/rootfs:ro", mounts)
        self.assertIn("/sys:/sys:ro", mounts)
        self.assertTrue(any("/collector-host:/etc/nighthawk/collector" in mount for mount in mounts))
        self.assertEqual(service["profiles"], ["host-collection"])

    def test_not_requested_renders_and_starts_nothing_for_it(self) -> None:
        options = self.run_with(False)
        directory = options.rendered_dir / "collector-host"
        self.assertFalse(directory.exists() and any(directory.iterdir()))
        self.assertNotIn("host-collection", self.up_call())
        self.assertNotIn("alloy-host", self.up_call())

    def test_requested_then_not_requested_removes_the_rendered_configuration(self) -> None:
        options = self.run_with(True)
        self.run_with(False)
        self.assertEqual(list((options.rendered_dir / "collector-host").glob("*")), [])

    def test_refused_under_rootless_podman_before_any_change(self) -> None:
        self.host = FakeHost(podman=True)
        with self.assertRaisesRegex(ConfigurationError, "--host-collection needs Docker Engine"):
            self.run_with(True)
        self.assert_nothing_happened()

    def test_teardown_also_removes_the_host_collector(self) -> None:
        options = self.run_with(True)
        self.host.compose_calls.clear()
        quickstart.teardown(options, runner=self.host, out=self.lines.append)
        (down,) = self.host.compose_calls
        self.assertIn("host-collection", down)


class RotatedSecretTests(QuickstartFixture):
    def provision_calls(self) -> list[list[str]]:
        return [call for call in self.host.compose_calls if "grafana-init" in call]

    def rotate(self, key: str) -> None:
        versions = self.vault.kv["nighthawk/local"]
        versions.append({**versions[-1], key: f"rotated-{key}-" + "r" * 40})

    def test_first_and_unchanged_runs_do_not_resend_passwords(self) -> None:
        self.run_quickstart()
        self.run_quickstart()
        self.assertEqual(len(self.provision_calls()), 2)
        for call in self.provision_calls():
            self.assertNotIn("--update-secrets", call)

    def test_query_secret_rotated_in_place_is_resent_once(self) -> None:
        options = self.run_quickstart()
        self.rotate("example-query")
        self.run_quickstart()
        self.assertEqual(self.provision_calls()[-1][-1], "--update-secrets")
        from nighthawk.secrets import materialized_path
        materialized = materialized_path(options.secrets_dir, load_platform(options.config).secrets["example-query"])
        self.assertTrue(materialized.read_text().startswith("rotated-example-query-"))
        # Nothing changed since: the next run does not resend.
        self.run_quickstart()
        self.assertNotIn("--update-secrets", self.provision_calls()[-1])

    def test_rotating_another_kind_of_secret_does_not_resend_passwords(self) -> None:
        self.run_quickstart()
        self.rotate("example-ingest")
        self.run_quickstart()
        self.assertNotIn("--update-secrets", self.provision_calls()[-1])


class RemoteDeploymentTests(QuickstartFixture):
    """Rendering a deployment for another machine: no container runtime is involved here."""

    def setUp(self) -> None:
        super().setUp()
        self.data["profile"] = "production"
        self.data["vault"]["address"] = "https://vault.example.com:8200"
        self.data["gateway"]["entry_points"] = ["local-gateway", "grafana-gateway", "remote-gateway"]
        self.vault = FakeVault(load_platform(write_document(self.root, self.data)))

    def options(self, **overrides) -> quickstart.Options:
        values = {
            "config": write_document(self.root, self.data), "root": self.root,
            "rendered_dir": self.root / "out" / "rendered", "secrets_dir": self.root / "out" / "secrets",
            "environ": self.environ, "vault_transport": self.vault,
            "remote_rendered_dir": Path("/opt/nighthawk/rendered"), "remote_secrets_dir": Path("/opt/nighthawk/secrets"),
            "remote_run_as": (2001, 2002), "external_bind_address": "192.0.2.10",
        }
        values.update(overrides)
        return quickstart.Options(**values)

    def render(self, **overrides) -> quickstart.Options:
        options = self.options(**overrides)
        quickstart.render_deployment(options, out=self.lines.append)
        return options

    def environment(self, options: quickstart.Options) -> dict[str, str]:
        text = (options.rendered_dir / "compose.env").read_text(encoding="utf-8")
        return dict(line.split("=", 1) for line in text.splitlines())

    def test_renders_both_trees_addressed_for_the_remote_host_and_starts_nothing(self) -> None:
        options = self.render()
        environment = self.environment(options)
        self.assertEqual(environment["NIGHTHAWK_RENDERED_DIR"], "/opt/nighthawk/rendered")
        self.assertEqual(environment["NIGHTHAWK_SECRETS_DIR"], "/opt/nighthawk/secrets")
        self.assertEqual((environment["NIGHTHAWK_UID"], environment["NIGHTHAWK_GID"]), ("2001", "2002"))
        self.assertEqual(environment["NIGHTHAWK_BIND_ADDRESS"], "127.0.0.1")
        self.assertEqual(environment["NIGHTHAWK_EXTERNAL_BIND_ADDRESS"], "192.0.2.10")
        self.assertEqual(environment["NIGHTHAWK_EXTERNAL_PORT"], "443")
        self.assertEqual(environment["NIGHTHAWK_EXTERNAL_PUBLISHED_PORT"], "443")
        self.assertEqual(environment["NIGHTHAWK_GRAFANA_PORT"], "443")
        for path in ("runtime/authz/policy.json", "runtime/gateway/gateway-server.key.pem", "runtime/collector/credential"):
            self.assertTrue((options.secrets_dir / path).is_file(), path)
        self.assertTrue((options.rendered_dir / "contracts" / "ansible" / "nighthawk.yml").is_file())
        self.assertEqual(sorted(self.stored()), sorted(self.data["secrets"]))
        # The container runtime of this machine was never asked anything.
        self.assertEqual(self.host.compose_calls, [])
        self.assertEqual(self.host.environments, [])
        text = "\n".join(self.lines)
        self.assertIn("single node without high availability", text)
        self.assertIn("/opt/nighthawk/secrets", text)
        manifest = json.loads((options.rendered_dir / "deployment.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["compose_files"], ["docker-compose/docker-compose.yaml", "docker-compose/docker-compose.external.yaml"],
        )
        import tarfile
        with tarfile.open(options.rendered_dir / manifest["source_archive"]) as archive:
            names = archive.getnames()
            self.assertEqual({entry.mtime for entry in archive.getmembers()}, {0})
        for name in ("requirements.txt", "nighthawk/__main__.py", "config/versions.yaml", "docker-compose/nighthawk.Dockerfile", *manifest["compose_files"]):
            self.assertIn(name, names)
        # Only what the image and Compose need: no secret, test, or rendered output can travel in it.
        self.assertEqual({name.split("/")[0] for name in names}, {"requirements.txt", "nighthawk", "config", "docker-compose"})
        self.assertFalse([name for name in names if "__pycache__" in name])
        self.assertEqual(manifest["waited_services"], ["grafana", "alloy"])
        self.assertEqual(manifest["profiles"], [])
        self.assertEqual([item["service"] for item in manifest["reloads"]], ["authz", "alloy"])
        self.assertEqual(manifest["provision"], ["grafana-init"])
        self.assertIn("single node without high availability", manifest["notice"])
        hosted = self.render(host_collection=True, rendered_dir=self.root / "other" / "rendered", secrets_dir=self.root / "other" / "secrets")
        manifest = json.loads((hosted.rendered_dir / "deployment.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["profiles"], ["host-collection"])
        self.assertEqual(manifest["waited_services"][-1], "alloy-host")
        self.assertEqual(manifest["reloads"][-1]["rendered"], ["collector-host"])

    def test_second_render_creates_and_rewrites_nothing(self) -> None:
        self.render()
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.created_files()}
        versions, signed = len(self.vault.kv["nighthawk/local"]), self.signed()
        second = self.render()
        self.assertEqual(second.created, [])
        self.assertEqual((len(self.vault.kv["nighthawk/local"]), self.signed()), (versions, signed))
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.created_files()}, before)

    def test_no_written_file_holds_the_vault_credential(self) -> None:
        self.render()
        self.assertTrue(self.created_files())
        for path in self.created_files():
            self.assertNotIn(TOKEN, path.read_text(encoding="utf-8", errors="replace"), path)
        self.assertNotIn(TOKEN, "\n".join(self.lines))

    def test_development_document_is_refused_before_anything_is_written(self) -> None:
        self.data["profile"] = "development"
        with self.assertRaisesRegex(ConfigurationError, "needs a platform document with profile: production"):
            self.render()
        self.assert_nothing_happened()
        self.assertEqual(self.vault.calls, [])

    def test_document_without_the_external_entry_point_is_refused(self) -> None:
        self.data["gateway"]["entry_points"] = ["local-gateway", "grafana-gateway"]
        with self.assertRaisesRegex(ConfigurationError, "does not select the certificate-requiring external entry point"):
            self.render()
        self.assert_nothing_happened()

    def test_missing_or_malformed_external_address_is_refused(self) -> None:
        for address, expected in ((None, "state the host address"), ("", "state the host address"), ("gateway.example.com", "is not an IP address")):
            with self.subTest(address=address), self.assertRaisesRegex(ConfigurationError, expected):
                self.render(external_bind_address=address)
        self.assert_nothing_happened()

    def test_remote_paths_must_be_absolute_and_the_account_stated(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must be an absolute path"):
            self.render(remote_rendered_dir=Path("relative/rendered"))
        with self.assertRaisesRegex(ConfigurationError, "UID and GID are required"):
            self.render(remote_run_as=None)
        self.assert_nothing_happened()

    def test_production_refuses_a_root_credential_and_names_vault_problems(self) -> None:
        from tests.fakes import ROOT_TOKEN
        self.environ["VAULT_TOKEN"] = ROOT_TOKEN
        with self.assertRaisesRegex(ConfigurationError, "production refuses a Vault credential that carries the root policy"):
            self.render()
        self.environ["VAULT_TOKEN"] = TOKEN
        self.vault.sealed = True
        with self.assertRaisesRegex(ConfigurationError, "is sealed"):
            self.render()
        self.assert_nothing_happened()

    def test_command_line_wires_every_option(self) -> None:
        from unittest import mock
        from nighthawk.__main__ import main

        with mock.patch("nighthawk.quickstart.render_deployment") as rendered:
            status = main([
                "render-docker-deployment", "--config", str(write_document(self.root, self.data)),
                "--output", str(self.root / "out"), "--remote-dir", "/opt/nighthawk", "--uid", "2001", "--gid", "2002",
                "--external-bind-address", "192.0.2.10", "--vault-token-file", str(self.root / "token"),
            ])
        self.assertEqual(status, 0)
        (options,), _ = rendered.call_args
        self.assertEqual(options.rendered_dir, self.root / "out" / "rendered")
        self.assertEqual(options.secrets_dir, self.root / "out" / "secrets")
        self.assertEqual(options.remote_rendered_dir, Path("/opt/nighthawk/rendered"))
        self.assertEqual(options.remote_secrets_dir, Path("/opt/nighthawk/secrets"))
        self.assertEqual(options.remote_run_as, (2001, 2002))
        self.assertEqual(options.external_bind_address, "192.0.2.10")

    def test_the_local_quickstart_cannot_publish_the_external_entry_point(self) -> None:
        import io
        from contextlib import redirect_stderr
        from nighthawk.__main__ import main

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["quickstart-docker", "--external-bind-address", "192.0.2.10"])


class CommandLineTests(QuickstartFixture):
    def test_vault_credential_options_reach_the_quickstart_and_the_authority_option_is_gone(self) -> None:
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        from nighthawk.__main__ import main

        token_file = self.root / "token"
        with mock.patch("nighthawk.quickstart.quickstart") as started:
            status = main([
                "quickstart-docker", "--config", str(write_document(self.root, self.data)),
                "--vault-token-file", str(token_file), "--no-build",
                "--credential", "example-query", "--credential", "example-ingest",
            ])
        self.assertEqual(status, 0)
        (options,), _ = started.call_args
        self.assertEqual(options.credentials, ("example-query", "example-ingest"))
        self.assertEqual(options.vault_auth.token_file, token_file)
        self.assertIsNone(options.vault_auth.role_id_file)
        self.assertFalse(options.build)
        self.assertFalse(hasattr(options, "age_key"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["quickstart-docker", "--ca-valid-days", "365"])


class TeardownTests(QuickstartFixture):
    def test_ordinary_teardown_keeps_volumes(self) -> None:
        options = self.run_quickstart()
        self.host.compose_calls.clear()
        stored = self.stored()
        quickstart.teardown(options, runner=self.host, out=self.lines.append)
        (down,) = self.host.compose_calls
        self.assertIn("down", down)
        self.assertNotIn("--volumes", down)
        self.assertNotIn("-v", down)
        self.assertIn("Volumes, secrets, and certificates were kept.", self.lines)
        self.assertEqual(self.stored(), stored)

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
