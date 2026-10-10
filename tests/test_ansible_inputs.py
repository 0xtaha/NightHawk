from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from nighthawk.__main__ import main
from nighthawk.ansible_inputs import ansible_inputs, firewall_rules, render_ansible_inputs
from nighthawk.config import ROOT, ConfigurationError, load_network, load_platform, load_versions
from tests.ansible_checks import contract_values, inventory_secrets, literal_contract_values
from tests.fakes import add_stream, example_document, write_document

ANSIBLE = ROOT / "ansible"


class InputsFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = example_document()
        self.rules = load_network(ROOT / "config" / "network.yaml")
        self.matrix = load_versions()

    def inputs(self) -> dict:
        platform = load_platform(write_document(self.root, self.data))
        return ansible_inputs(platform, self.rules, self.matrix)["nighthawk"]


class FirewallRuleTests(InputsFixture):
    def ports(self, role: str) -> list[tuple[str, int, str]]:
        return [(rule["protocol"], rule["port"], rule["source"]) for rule in self.inputs()["firewall"][role]]

    def test_docker_host_opens_ssh_and_the_external_entry_point_only(self) -> None:
        self.assertEqual(sorted(self.ports("docker_host")), [
            ("tcp", 22, "admin_allowlist"), ("tcp", 443, "collector_allowlist"),
        ])

    def test_loopback_and_container_internal_rules_are_never_opened_on_a_host(self) -> None:
        for role, rules in self.inputs()["firewall"].items():
            for rule in rules:
                self.assertNotEqual(rule["scope"], "loopback", (role, rule["id"]))
                self.assertNotIn(rule["port"], (8443, 9180, 8080, 3100, 3200, 4040, 3000, 8333), (role, rule["id"]))

    def test_cluster_server_and_agent_rules_come_from_the_other_nodes(self) -> None:
        self.assertEqual(sorted(self.ports("cluster_server")), [
            ("tcp", 22, "admin_allowlist"), ("tcp", 2379, "cluster_servers"), ("tcp", 2380, "cluster_servers"),
            ("tcp", 4240, "cluster_nodes"), ("tcp", 6443, "admin_allowlist"), ("tcp", 6443, "cluster_nodes"),
            ("tcp", 10250, "cluster_nodes"), ("udp", 8472, "cluster_nodes"),
        ])
        self.assertEqual(sorted(self.ports("cluster_agent")), [
            ("tcp", 22, "admin_allowlist"), ("tcp", 4240, "cluster_nodes"), ("tcp", 10250, "cluster_nodes"),
            ("udp", 8472, "cluster_nodes"),
        ])

    def test_collector_host_opens_ssh_only(self) -> None:
        self.assertEqual(self.ports("collector_host"), [("tcp", 22, "admin_allowlist")])

    def test_every_rule_is_a_contract_rule_with_its_protocol_and_port(self) -> None:
        contract = {rule["id"]: rule for rule in self.rules}
        for role, rules in self.inputs()["firewall"].items():
            for rule in rules:
                source = contract[rule["id"]]
                self.assertEqual((rule["protocol"], rule["port"], rule["scope"]), (source["protocol"], source["port"], source["scope"]))

    def test_restricted_rules_use_an_operator_allowlist(self) -> None:
        for rules in self.inputs()["firewall"].values():
            for rule in rules:
                if rule["scope"] == "restricted-external":
                    self.assertIn(rule["source"], ("admin_allowlist", "collector_allowlist"), rule["id"])

    def test_rule_from_an_unknown_source_is_an_error_not_an_open_port(self) -> None:
        rules = [*self.rules, {
            "id": "mystery", "source": "somebody", "destination": "host-ssh", "protocol": "tcp", "port": 2222,
            "purpose": "x", "scope": "private", "direction": "source-to-destination",
        }]
        with self.assertRaisesRegex(ConfigurationError, "network rule mystery: no inventory source is defined for 'somebody'"):
            firewall_rules(rules)


class RenderedInputsTests(InputsFixture):
    def test_pins_and_checksums_come_from_the_matrix(self) -> None:
        pins = self.inputs()["pins"]
        artifacts = self.matrix["host_artifacts"]
        self.assertEqual(pins["docker_engine"]["version"], artifacts["docker_engine"]["version"])
        self.assertEqual(pins["docker_engine"]["signing_key_fingerprints"], artifacts["docker_engine"]["signing_key_fingerprints"])
        self.assertEqual(pins["k3s"], {
            "version": self.matrix["kubernetes_platform"]["k3s"]["version"], "sha256": artifacts["k3s"]["sha256"],
        })
        self.assertEqual(pins["cilium"]["version"], self.matrix["kubernetes_platform"]["cilium"]["version"])
        self.assertEqual(pins["alloy"], {
            "version": self.matrix["collectors"]["alloy"]["version"], "sha256": artifacts["alloy"]["sha256"],
        })

    def test_a_changed_pin_changes_the_inputs_and_nothing_else_is_needed(self) -> None:
        self.matrix["kubernetes_platform"]["k3s"]["version"] = "9.9.9+k3s1"
        self.assertEqual(self.inputs()["pins"]["k3s"]["version"], "9.9.9+k3s1")

    def test_supported_systems_per_role(self) -> None:
        support = self.inputs()["os_support"]
        self.assertEqual(sorted(support), ["cluster_agent", "cluster_server", "collector_host", "docker_host"])
        self.assertEqual([entry["distribution"] for entry in support["docker_host"]], ["ubuntu", "debian", "rocky", "almalinux"])
        self.assertEqual([entry["distribution"] for entry in support["cluster_server"]], ["ubuntu"])
        self.assertEqual(support["collector_host"], support["docker_host"])
        self.assertNotIn("evidence", support["docker_host"][0])

    def test_cluster_values_come_from_the_contract_and_the_layout_check(self) -> None:
        from nighthawk import cluster_layout
        cluster = self.inputs()["cluster"]
        rules = {rule["id"]: rule["port"] for rule in self.rules}
        self.assertEqual(cluster["api_port"], rules["cluster-api-server"])
        self.assertEqual(cluster["overlay_port"], rules["cluster-network-overlay"])
        self.assertEqual(cluster["checked_modules"], [*cluster_layout.REQUIRED_MODULES, *cluster_layout.STORAGE_MODULES])
        self.assertEqual(cluster["checked_packages"], list(cluster_layout.STORAGE_PACKAGES))

    def test_gateway_values(self) -> None:
        self.data["gateway"]["entry_points"] = ["local-gateway", "remote-gateway"]
        self.data["gateway"]["grafana_entry_point"] = "remote-gateway"
        self.assertEqual(self.inputs()["gateway"], {
            "hostname": "gateway.nighthawk.internal", "grafana_hostname": "grafana.nighthawk.internal",
            "loopback_port": 8443, "external_port": 443,
        })

    def test_kernel_settings_keep_forwarding_and_a_compatible_filter_on_cluster_nodes(self) -> None:
        sysctl = self.inputs()["sysctl"]
        for role in ("cluster_server", "cluster_agent"):
            self.assertEqual(sysctl[role]["net.ipv4.ip_forward"], 1)
            self.assertEqual(sysctl[role]["net.ipv4.conf.all.rp_filter"], 0)
        self.assertEqual(sysctl["docker_host"]["net.ipv4.ip_forward"], 1)
        self.assertNotEqual(sysctl["docker_host"]["net.ipv4.conf.all.rp_filter"], 1)
        self.assertEqual(sysctl["collector_host"]["net.ipv4.ip_forward"], 0)
        for role in sysctl:
            self.assertEqual(sysctl[role]["net.ipv4.conf.all.accept_redirects"], 0)

    def test_output_is_deterministic_and_holds_no_secret_reference_values(self) -> None:
        add_stream(self.data, "second", "edge", "second-edge", certificate=True)
        platform = load_platform(write_document(self.root, self.data))
        first = render_ansible_inputs(platform, self.rules, self.matrix)
        self.data["credentials"].reverse()
        self.data["tenants"].reverse()
        platform = load_platform(write_document(self.root, self.data))
        self.assertEqual(render_ansible_inputs(platform, list(reversed(self.rules)), self.matrix), first)
        (text,) = first.values()
        self.assertEqual(list(first), ["ansible/nighthawk.yml"])
        self.assertNotIn("secret", text)
        self.assertEqual(yaml.safe_load(text)["nighthawk"]["deployment"], "docker")

    def test_render_contracts_writes_the_file_without_contacting_vault(self) -> None:
        config, output = write_document(self.root, self.data), self.root / "contracts"
        environment = {key: value for key, value in os.environ.items() if key != "VAULT_TOKEN"}
        with mock.patch("nighthawk.vault.http_transport", side_effect=AssertionError("Vault was contacted")), \
                mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["render-contracts", "--config", str(config), "--output", str(output)]), 0)
        rendered = yaml.safe_load((output / "ansible" / "nighthawk.yml").read_text(encoding="utf-8"))
        self.assertEqual(rendered["nighthawk"], self.inputs())


class RepositoryCheckTests(InputsFixture):
    """The checks themselves, on planted examples, and on the real tree."""

    def rendered(self) -> dict:
        return {"nighthawk": self.inputs()}

    def test_contract_values_cover_ports_versions_checksums_and_fingerprints(self) -> None:
        values = contract_values(self.rendered())
        for expected in ("22", "6443", "8472", self.matrix["host_artifacts"]["docker_engine"]["version"],
                         self.matrix["kubernetes_platform"]["k3s"]["version"],
                         self.matrix["host_artifacts"]["k3s"]["sha256"]["linux_amd64"],
                         self.matrix["host_artifacts"]["docker_engine"]["signing_key_fingerprints"]["rpm"]):
            self.assertIn(expected, values)

    def test_a_planted_literal_is_reported_with_its_file_and_meaning(self) -> None:
        role = self.root / "roles" / "example" / "tasks"
        role.mkdir(parents=True)
        (role / "main.yml").write_text("- name: Open the API\n  port: 6443\n", encoding="utf-8")
        (role / "clean.yml").write_text("- name: Uses the input\n  port: '{{ rule.port }}'\n  mode: '0644'\n  version: 22.04\n", encoding="utf-8")
        problems = literal_contract_values(self.root, self.rendered())
        self.assertEqual(len(problems), 1)
        self.assertIn("roles/example/tasks/main.yml: literal 6443 (port of network rule cluster-api", problems[0])

    def test_a_planted_version_or_checksum_is_reported(self) -> None:
        role = self.root / "roles" / "example" / "defaults"
        role.mkdir(parents=True)
        version = self.matrix["kubernetes_platform"]["k3s"]["version"]
        checksum = self.matrix["host_artifacts"]["alloy"]["sha256"]["linux_arm64"]
        (role / "main.yml").write_text(f"k3s_version: {version}\nalloy_sum: {checksum}\n", encoding="utf-8")
        self.assertEqual(len(literal_contract_values(self.root, self.rendered())), 2)

    def test_a_planted_inventory_secret_is_reported_and_locations_are_not(self) -> None:
        inventory = self.root / "inventories" / "example" / "group_vars"
        inventory.mkdir(parents=True)
        (inventory / "all.yml").write_text(
            "vault_token_file: /run/secrets/token\njoin_token_secret_ref: k3s-token\nadmin_password: hunter2\n"
            "nested:\n  api_token: abc123\nempty_secret: ''\n", encoding="utf-8",
        )
        self.assertEqual(inventory_secrets(self.root), [
            "inventories/example/group_vars/all.yml: admin_password holds a literal value",
            "inventories/example/group_vars/all.yml: nested.api_token holds a literal value",
        ])

    def test_both_checks_pass_on_an_empty_tree(self) -> None:
        self.assertEqual(literal_contract_values(self.root, self.rendered()), [])
        self.assertEqual(inventory_secrets(self.root), [])

    def test_the_real_roles_and_playbooks_hard_code_no_contract_value(self) -> None:
        for name in ("roles", "playbooks"):
            if (ANSIBLE / name).exists():
                self.assertEqual(literal_contract_values(ANSIBLE / name, self.rendered()), [], name)

    def test_the_real_inventories_hold_no_secret(self) -> None:
        if (ANSIBLE / "inventories").exists():
            self.assertEqual(inventory_secrets(ANSIBLE / "inventories"), [])


if __name__ == "__main__":
    unittest.main()
