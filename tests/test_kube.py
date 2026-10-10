from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from nighthawk import kube, vault
from nighthawk.config import ROOT, ConfigurationError, load_platform
from tests.fakes import ROOT_TOKEN, FakeVault, fake_client, run_cli, write_document

EXAMPLE = ROOT / "config" / "self-hosted.example.yaml"


def self_hosted_document() -> dict:
    """The self-hosted example as a development document, so a test can bootstrap a fake Vault for it."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["profile"] = "development"
    data["vault"]["address"] = "http://127.0.0.1:8200"
    return data


class KubeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self_hosted_document()

    def platform(self):
        return load_platform(write_document(self.root, self.data))

    def workloads(self, **options) -> dict[str, kube.Workload]:
        return {item.name: item for item in kube.workloads(self.platform(), **options)}

    def access(self) -> dict:
        platform = self.platform()
        return vault.cluster_access(platform, kube.workloads(platform), kube.NAMESPACE, kube.ISSUER, (kube.GATEWAY_NAMESPACE,))


class WorkloadTests(KubeFixture):
    def test_each_workload_reads_exactly_its_own_secrets(self) -> None:
        workloads = self.workloads()
        self.assertEqual(
            sorted(workloads),
            ["authz", "collector", "grafana", "grafana-provisioner", "loki", "mimir", "object-storage", "pyroscope", "tempo"],
        )
        self.assertEqual(workloads["authz"].secrets, ("example-ingest", "example-query"))
        self.assertEqual(workloads["collector"].secrets, ("example-ingest",))
        self.assertEqual(workloads["grafana"].secrets, ("grafana-admin",))
        self.assertEqual(workloads["grafana-provisioner"].secrets, ("example-query", "grafana-admin"))
        for backend in ("mimir", "loki", "tempo", "pyroscope"):
            self.assertEqual(workloads[backend].secrets, (f"{backend}-storage",))
        self.assertEqual(
            workloads["object-storage"].secrets, ("loki-storage", "mimir-storage", "pyroscope-storage", "tempo-storage"),
        )
        self.assertEqual(workloads["mimir"].service_account, "mimir")
        self.assertEqual(workloads["mimir"].vault_role, "nighthawk-mimir")

    def test_a_disabled_signal_has_no_backend_workload(self) -> None:
        for tenant in self.data["tenants"]:
            for stream in tenant["datastreams"]:
                stream["signals"].pop("profiles", None)
        self.assertNotIn("pyroscope", self.workloads())

    def test_secrets_with_different_readers_cannot_share_a_vault_path(self) -> None:
        for name in ("mimir-storage", "grafana-admin"):
            self.data["secrets"][name] = {"path": "nighthawk/self-hosted/shared", "key": name}
        with self.assertRaisesRegex(ConfigurationError, "Vault path nighthawk/self-hosted/shared holds secrets with different readers") as raised:
            self.workloads()
        self.assertIn("mimir-storage is read by mimir, object-storage", str(raised.exception))
        self.assertIn("give secrets with different readers different paths", str(raised.exception))

    def test_secrets_with_the_same_readers_may_share_a_path(self) -> None:
        # A second ingestion credential that only the auth service reads, beside the query credential's readers.
        self.data["secrets"]["example-ingest"] = {"path": "nighthawk/self-hosted/collector", "key": "example-ingest"}
        self.assertIn("collector", self.workloads())

    def test_collector_datastream_and_credential_are_chosen_explicitly_when_ambiguous(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown tenant/datastream nobody/nothing"):
            self.workloads(tenant="nobody", datastream="nothing")
        self.assertEqual(self.workloads(tenant="example", datastream="application")["collector"].secrets, ("example-ingest",))

    def test_docker_documents_and_documents_without_the_mount_are_refused(self) -> None:
        del self.data["vault"]["kubernetes_auth"]
        with self.assertRaisesRegex(ConfigurationError, "needs kubernetes_auth.mount"):
            self.workloads()
        docker = load_platform(ROOT / "config" / "tenants.example.yaml")
        with self.assertRaisesRegex(ConfigurationError, "rendered for a self-hosted-k8s document, not docker"):
            kube.workloads(docker)


class ClusterAccessTests(KubeFixture):
    def test_one_role_and_policy_per_identity_bound_to_its_service_account(self) -> None:
        access = self.access()
        self.assertEqual(access["mount"], "nighthawk-k8s")
        self.assertEqual(sorted(access["roles"]), sorted(access["policies"]))
        self.assertIn("nighthawk-certificate-issuer", access["roles"])
        for name, role in access["roles"].items():
            self.assertEqual(role["token_policies"], [name])
            # Certificates are requested beside the workloads and beside the gateway; secrets only beside the workloads.
            expected = ["nighthawk", "nighthawk-gateway"] if name == "nighthawk-certificate-issuer" else ["nighthawk"]
            self.assertEqual(role["bound_service_account_namespaces"], expected)
            self.assertEqual(role["bound_service_account_names"], [name.removeprefix("nighthawk-")])

    def test_no_identity_can_write_a_secret(self) -> None:
        for name, policy in self.access()["policies"].items():
            for block in policy.split("path ")[1:]:
                path, capabilities = block.split('"')[1], json.loads(block.split("capabilities = ")[1].split("\n")[0])
                if path.startswith("nighthawk-kv/"):
                    self.assertEqual(capabilities, ["read"], (name, path))
                else:
                    # Only the issuer leaves the key-value mount, and only to have a request signed.
                    self.assertEqual(name, "nighthawk-certificate-issuer")
                    self.assertRegex(path, r"^nighthawk-pki/sign/(nighthawk-server|nighthawk-collector)$")

    def test_a_workloads_policy_excludes_every_other_workloads_secrets(self) -> None:
        policies = self.access()["policies"]
        self.assertIn('"nighthawk-kv/data/nighthawk/self-hosted/mimir-storage"', policies["nighthawk-mimir"])
        for other in ("loki-storage", "grafana-admin", "example-ingest", "example-query"):
            self.assertNotIn(other, policies["nighthawk-mimir"])
        self.assertNotIn("grafana-admin", policies["nighthawk-authz"])
        self.assertNotIn("example-query", policies["nighthawk-collector"])
        # The cluster's join token is for the hosts, never for a workload.
        self.assertFalse([name for name, policy in policies.items() if "nighthawk/cluster" in policy])

    def test_requirements_are_deterministic_and_hold_no_secret_value(self) -> None:
        first = vault.access_requirements(self.platform(), self.access())["vault/kubernetes-auth.json"]
        self.assertEqual(first, vault.access_requirements(self.platform(), self.access())["vault/kubernetes-auth.json"])
        self.assertNotIn("vault/kubernetes-auth.json", vault.access_requirements(self.platform()))

    def test_render_contracts_writes_them_for_self_hosted_and_not_for_docker(self) -> None:
        fake = FakeVault()
        output = self.root / "contracts"
        status, _, errors = run_cli(fake, ["render-contracts", "--config", str(write_document(self.root, self.data)), "--output", str(output)], token=None)
        self.assertEqual((status, errors), (0, ""))
        rendered = json.loads((output / "vault" / "kubernetes-auth.json").read_text(encoding="utf-8"))
        self.assertEqual(rendered, self.access())
        self.assertEqual(fake.calls, [])
        docker = self.root / "docker"
        status, _, _ = run_cli(fake, ["render-contracts", "--config", str(ROOT / "config" / "tenants.example.yaml"), "--output", str(docker)], token=None)
        self.assertEqual(status, 0)
        self.assertFalse((docker / "vault" / "kubernetes-auth.json").exists())


class DevelopmentVaultTests(KubeFixture):
    def setUp(self) -> None:
        super().setUp()
        self.config = write_document(self.root, self.data)
        self.fake = FakeVault(self.platform())
        self.requirements = self.root / "kubernetes-auth.json"
        self.requirements.write_text(json.dumps(self.access()), encoding="utf-8")

    def bootstrap(self, *extra: str) -> tuple[int, str, str]:
        return run_cli(self.fake, [
            "bootstrap-dev-vault", "--config", str(self.config), "--confirm-disposable-vault", "--ca-valid-days", "30",
            "--cluster-requirements", str(self.requirements), *extra,
        ], token=ROOT_TOKEN)

    def doctor(self) -> tuple[int, str, str]:
        return run_cli(self.fake, ["doctor", "--config", str(self.config), "--cluster", str(self.requirements)], token=ROOT_TOKEN)

    def test_bootstrap_applies_the_requirements_and_a_second_run_changes_nothing(self) -> None:
        status, output, errors = self.bootstrap("--kubernetes-host", "https://192.0.2.31:6443")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("authentication mount nighthawk-k8s", output)
        self.assertEqual(self.fake.auth_mounts["nighthawk-k8s"], {"type": "kubernetes"})
        self.assertEqual(self.fake.auth_configs["nighthawk-k8s"], {"kubernetes_host": "https://192.0.2.31:6443"})
        access = self.access()
        self.assertEqual({name for _, name in self.fake.auth_roles}, set(access["roles"]))
        for name, policy in access["policies"].items():
            self.assertEqual(self.fake.policies[name], policy)
        writes = [call for call in self.fake.calls if call[0] in ("POST", "PUT")]
        status, output, _ = self.bootstrap("--kubernetes-host", "https://192.0.2.31:6443")
        self.assertEqual(status, 0)
        self.assertIn("nothing to change", output)
        self.assertEqual([call for call in self.fake.calls if call[0] in ("POST", "PUT")], writes)
        self.assertEqual(self.doctor()[0], 0)

    def test_doctor_names_a_missing_mount_role_and_policy(self) -> None:
        status, output, _ = self.doctor()
        self.assertEqual(status, 1)
        self.assertIn("FAIL: authentication mount nighthawk-k8s does not exist", output)
        self.bootstrap()
        del self.fake.auth_roles[("nighthawk-k8s", "nighthawk-mimir")]
        del self.fake.policies["nighthawk-loki"]
        status, output, _ = self.doctor()
        self.assertEqual(status, 1)
        self.assertIn("FAIL: role nighthawk-mimir does not exist at nighthawk-k8s", output)
        self.assertIn("FAIL: policy nighthawk-loki does not exist", output)

    def test_doctor_reports_a_wider_policy_or_role(self) -> None:
        self.bootstrap()
        self.fake.policies["nighthawk-mimir"] += '\npath "nighthawk-kv/data/*" {\n  capabilities = ["read"]\n}\n'
        self.fake.auth_roles[("nighthawk-k8s", "nighthawk-authz")]["bound_service_account_names"] = ["authz", "default"]
        status, output, _ = self.doctor()
        self.assertEqual(status, 1)
        self.assertIn("FAIL: policy nighthawk-mimir differs from the rendered one; it may allow more", output)
        self.assertIn("FAIL: role nighthawk-authz is bound to other service accounts or policies", output)

    def test_existing_mount_of_another_type_is_not_taken_over(self) -> None:
        self.fake.auth_mounts["nighthawk-k8s"] = {"type": "approle"}
        status, _, errors = self.bootstrap()
        self.assertEqual(status, 1)
        self.assertIn("already exists in Vault as a approle authentication mount", errors)
        self.assertEqual(self.fake.auth_roles, {})

    def test_host_option_needs_the_requirements(self) -> None:
        status, _, errors = run_cli(self.fake, [
            "bootstrap-dev-vault", "--config", str(self.config), "--confirm-disposable-vault", "--ca-valid-days", "30",
            "--kubernetes-host", "https://192.0.2.31:6443",
        ], token=ROOT_TOKEN)
        self.assertEqual(status, 1)
        self.assertIn("--kubernetes-host only applies with --cluster-requirements", errors)


class ExampleDocumentTests(unittest.TestCase):
    def test_the_production_example_renders_cluster_requirements(self) -> None:
        platform = load_platform(EXAMPLE)
        access = vault.cluster_access(platform, kube.workloads(platform), kube.NAMESPACE, kube.ISSUER, (kube.GATEWAY_NAMESPACE,))
        self.assertEqual(len(access["roles"]), 10)


class RenderingTests(KubeFixture):
    """What `render-contracts` writes for a self-hosted development document."""

    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeVault()

    def render(self, name: str = "contracts", data: dict | None = None, *extra: str) -> Path:
        output = self.root / name
        config = write_document(self.root, data or self.data, f"{name}.yaml")
        status, _, errors = run_cli(self.fake, ["render-contracts", "--config", str(config), "--output", str(output), *extra], token=None)
        self.assertEqual((status, errors), (0, ""))
        return output / "kubernetes"

    def files(self, directory: Path) -> dict[str, str]:
        return {str(path.relative_to(directory)): path.read_text(encoding="utf-8") for path in sorted(directory.rglob("*")) if path.is_file()}

    def test_one_values_file_per_release_in_dependency_order(self) -> None:
        directory = self.render()
        releases = yaml.safe_load((directory / "releases.yaml").read_text(encoding="utf-8"))
        self.assertEqual(releases["profile"], "development")
        self.assertIn("single node without high availability", releases["notice"])
        names = [item["name"] for item in releases["releases"]]
        self.assertEqual(names[:3], ["nighthawk-network", "nighthawk-gateway-network", "nighthawk-foundation"])
        order = {name: index for index, name in enumerate(names)}
        for earlier, later in (
            ("nighthawk-foundation", "nighthawk-storage"), ("nighthawk-storage", "nighthawk-storage-init"),
            ("nighthawk-storage-init", "nighthawk-backends"), ("nighthawk-backends", "nighthawk-authz"),
            ("nighthawk-authz", "traefik"), ("traefik", "grafana"), ("grafana", "grafana-provision"),
            ("grafana-provision", "nighthawk-collectors"),
        ):
            self.assertLess(order[earlier], order[later], (earlier, later))
        for item in releases["releases"] + releases["addons"]:
            for values in item["values"]:
                self.assertTrue((directory / "values" / values).is_file(), values)
            self.assertIn(item["namespace"], ("nighthawk", "nighthawk-gateway", "metallb-system", "cert-manager", "vault-secrets-operator"))
            if item["chart"] != releases["platform_chart"]:
                self.assertRegex(releases["charts"][item["chart"]]["sha256"], "^[0-9a-f]{64}$")
        # One node gets no replicated storage, ingest log, or replicated database.
        self.assertEqual([item["name"] for item in releases["addons"]], ["metallb", "cert-manager", "vault-secrets-operator"])

    def test_rendering_is_deterministic_contacts_no_vault_and_holds_no_secret(self) -> None:
        first, second = self.files(self.render("one")), self.files(self.render("two"))
        self.assertEqual(first, second)
        self.assertEqual(self.fake.calls, [])
        text = "".join(first.values())
        self.assertNotIn("PRIVATE KEY", text)
        # Secrets appear only as the place they live in Vault.
        self.assertIn("nighthawk/self-hosted/mimir-storage", text)

    def test_every_image_is_pinned_by_a_matrix_digest(self) -> None:
        from nighthawk.config import load_versions
        matrix = load_versions()
        digests = {image["digest"] for section in ("container_images", "kubernetes_images") for image in matrix[section].values()}
        files = self.files(self.render())
        found = set()
        for name, content in files.items():
            for digest in __import__("re").findall(r"sha256:[0-9a-f]{64}", content):
                self.assertIn(digest, digests, name)
                found.add(digest)
        platform_values = yaml.safe_load(files["values/platform.yaml"])
        for name in ("seaweedfs", "mimir", "tempo", "alloy"):
            self.assertIn(matrix["container_images"][name]["digest"], platform_values["images"][name])
        # The platform's own image is the operator's; the deployment supplies it.
        self.assertEqual(platform_values["images"]["platform"], "")
        self.assertGreaterEqual(len(found), 14)

    def test_a_new_datastream_needs_no_edit_beyond_the_document(self) -> None:
        before = self.files(self.render("before"))
        data = copy.deepcopy(self.data)
        stream = copy.deepcopy(data["tenants"][0]["datastreams"][0])
        stream.update(id="second", backend_id="example-second")
        data["tenants"][0]["datastreams"].append(stream)
        for permission in ("ingest", "query"):
            name = f"example-second-{permission}"
            data["secrets"][name] = {"path": f"nighthawk/self-hosted/{name}", "key": "value"}
            data["credentials"].append({"id": name, "secret_ref": name, "tenant": "example", "datastream": "second", "permission": permission})
        after = self.files(self.render("after", data, "--collector-tenant", "example", "--collector-datastream", "application"))
        self.assertEqual(sorted(before), sorted(after))
        self.assertIn("example-second", after["values/platform.yaml"])
        self.assertIn("example-second", after["values/traefik.yaml"] + after["values/platform.yaml"])
        self.assertEqual(before["values/addon-metallb.yaml"], after["values/addon-metallb.yaml"])

    def test_docker_and_production_documents_get_no_kubernetes_workloads(self) -> None:
        output = self.root / "docker"
        status, _, _ = run_cli(self.fake, ["render-contracts", "--config", str(ROOT / "config" / "tenants.example.yaml"), "--output", str(output)], token=None)
        self.assertEqual(status, 0)
        self.assertFalse((output / "kubernetes").exists())
        production = self.root / "production"
        status, text, _ = run_cli(self.fake, ["render-contracts", "--config", str(EXAMPLE), "--output", str(production)], token=None)
        self.assertEqual(status, 0)
        self.assertIn("rendered for the development profile only", text)
        self.assertFalse((production / "kubernetes").exists())
        # What Vault must allow does not depend on the profile.
        self.assertTrue((production / "vault" / "kubernetes-auth.json").is_file())

    def test_gateway_addresses_must_name_the_services_of_the_platform_namespace(self) -> None:
        self.data["gateway"]["upstreams"]["metrics"] = "http://mimir:8080"
        config = write_document(self.root, self.data)
        status, _, errors = run_cli(self.fake, ["render-contracts", "--config", str(config), "--output", str(self.root / "bad")], token=None)
        self.assertEqual(status, 1)
        self.assertIn("gateway.upstreams.metrics: http://mimir:8080 must name the service the deployment creates, mimir.nighthawk.svc", errors)

    def test_the_in_cluster_entry_point_is_required(self) -> None:
        self.data["gateway"]["entry_points"] = ["grafana-gateway", "remote-gateway"]
        config = write_document(self.root, self.data)
        status, _, errors = run_cli(self.fake, ["render-contracts", "--config", str(config), "--output", str(self.root / "bad")], token=None)
        self.assertEqual(status, 1)
        self.assertIn("cluster-gateway", errors)

    def test_development_backends_are_single_replicas_without_an_ingest_log(self) -> None:
        files = self.files(self.render())
        values = yaml.safe_load(files["values/platform.yaml"])
        self.assertEqual(sorted(values["backends"]), ["mimir", "tempo"])
        self.assertEqual(yaml.safe_load(files["values/loki.yaml"])["singleBinary"]["replicas"], 1)
        self.assertEqual(yaml.safe_load(files["values/pyroscope.yaml"])["pyroscope"]["replicaCount"], 1)
        self.assertNotIn("kafka", "".join(files.values()).lower())

    def test_only_the_gateway_gets_an_external_address_and_only_on_external_entry_points(self) -> None:
        traefik = yaml.safe_load(self.files(self.render())["values/traefik.yaml"])
        exposed = {name: port["expose"]["default"] for name, port in traefik["ports"].items() if port}
        self.assertEqual(exposed, {"metrics": False, "port-443": True, "port-8444": False})
        self.assertEqual(traefik["service"]["type"], "LoadBalancer")
        self.assertEqual(traefik["providers"]["file"]["watch"], False)
        self.assertNotIn("tls", traefik["providers"]["file"]["content"])


class NetworkPolicyTests(KubeFixture):
    def setUp(self) -> None:
        super().setUp()
        from nighthawk.config import load_network
        self.rules = load_network(ROOT / "config" / "network.yaml")
        self.policies = {(item["namespace"], item["component"]): item for item in kube.network_policies(self.platform(), self.rules)}

    def allowances(self) -> list[tuple[str, str, str, dict, dict]]:
        return [
            (namespace, component, direction, group["peer"], port)
            for (namespace, component), policy in self.policies.items()
            for direction in ("ingress", "egress") for group in policy[direction] for port in group["ports"]
        ]

    def test_every_allowance_is_exactly_one_contract_rule_with_its_protocol_and_port(self) -> None:
        by_id = {rule["id"]: rule for rule in self.rules}
        self.assertTrue(self.allowances())
        for namespace, component, direction, peer, port in self.allowances():
            rule = by_id[port["rule"]]
            self.assertEqual(port["protocol"], rule["protocol"].upper(), port)
            expected = kube.gateway_container_port(rule["port"]) if rule["destination"] == "gateway" else rule["port"]
            self.assertEqual(port["port"], expected, port)

    def test_every_contract_rule_with_an_end_in_a_platform_namespace_is_covered(self) -> None:
        covered = {port["rule"] for *_, port in self.allowances()}
        placed = set(kube._placement(self.platform()))
        selected = {rule for entry in self.platform().gateway.entry_points for rule in entry.rules}
        for rule in self.rules:
            if rule["destination"] == "gateway" and rule["id"] not in selected:
                continue
            reachable = lambda name: name in placed or name in kube.SPECIAL_PEERS
            if (rule["source"] in placed and reachable(rule["destination"])) or (rule["destination"] in placed and reachable(rule["source"])):
                self.assertIn(rule["id"], covered, rule["id"])

    def sources(self, component: str, port: int) -> set[str]:
        return {
            group["peer"].get("component", group["peer"]["kind"])
            for group in self.policies[("nighthawk", component)]["ingress"] for entry in group["ports"] if entry["port"] == port
        }

    def test_backends_accept_only_the_gateway_the_collector_and_themselves(self) -> None:
        self.assertEqual(self.sources("mimir", 8080), {"gateway", "collector"})
        self.assertEqual(self.sources("loki", 3100), {"gateway", "collector"})
        self.assertEqual(self.sources("tempo", 4317), {"gateway"})
        self.assertEqual(self.sources("mimir", 9095), {"mimir"})
        for backend in ("mimir", "loki", "tempo", "pyroscope"):
            peers = {group["peer"].get("component", group["peer"]["kind"]) for group in self.policies[("nighthawk", backend)]["ingress"]}
            self.assertLessEqual(peers, {"gateway", "collector", backend}, backend)

    def test_object_storage_accepts_only_the_backends_and_itself(self) -> None:
        self.assertEqual(self.sources("object-storage", 8333), {"mimir", "loki", "tempo", "pyroscope"})
        peers = {group["peer"].get("component") for group in self.policies[("nighthawk", "object-storage")]["ingress"]}
        self.assertEqual(peers, {"mimir", "loki", "tempo", "pyroscope", "object-storage"})

    def test_nothing_but_the_collector_gateway_and_pyroscope_reaches_the_cluster_api(self) -> None:
        with_api = {
            component for (namespace, component), policy in self.policies.items()
            if any(group["peer"]["kind"] == "cluster-api" for group in policy["egress"])
        }
        self.assertEqual(with_api, {"collector", "gateway", "pyroscope"})

    def test_egress_leaves_the_cluster_only_for_the_kubelet(self) -> None:
        outside = {
            (component, port["rule"]) for namespace, component, direction, peer, port in self.allowances()
            if direction == "egress" and peer["kind"] in ("anywhere", "nodes")
        }
        self.assertEqual(outside, {("collector", "collector-kubelet")})

    def test_every_component_may_resolve_names_and_the_gateway_is_in_its_own_namespace(self) -> None:
        for policy in self.policies.values():
            self.assertTrue(any(group["peer"]["kind"] == "dns" for group in policy["egress"]), policy["component"])
        self.assertEqual([key for key in self.policies if key[0] == "nighthawk-gateway"], [("nighthawk-gateway", "gateway")])
