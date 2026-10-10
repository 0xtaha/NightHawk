from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from nighthawk.config import (
    ROOT, ConfigurationError, artifact_checksum_problems, chart_version_problems, check_pins, is_os_supported, load_versions, validate_pin_changes,
    supported_architectures, vault_supports,
)


class VersionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.matrix = yaml.safe_load((ROOT / "config" / "versions.yaml").read_text())

    def write(self, data: dict) -> Path:
        path = Path(self.temp.name) / "versions.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_load_versions_accepts_the_real_matrix(self) -> None:
        matrix = load_versions()
        self.assertEqual(matrix["schema_version"], 1)
        self.assertIn("loki", matrix["backends"])

    def test_every_chart_is_locked_by_digest_and_agrees_with_the_pin_it_names(self) -> None:
        charts = load_versions()["helm_charts"]
        self.assertGreaterEqual(len(charts), 13)
        for name, chart in charts.items():
            self.assertRegex(chart["sha256"], r"^[0-9a-f]{64}$", name)
            self.assertTrue(chart["repository"].startswith("https://"), name)
        document = copy.deepcopy(self.matrix)
        del document["helm_charts"]["loki"]["sha256"]
        with self.assertRaisesRegex(ConfigurationError, "'sha256' is a required property"):
            load_versions(self.write(document))

    def test_chart_version_that_differs_from_its_pin_is_named(self) -> None:
        document = copy.deepcopy(self.matrix)
        document["helm_charts"]["metallb"]["version"] = "0.16.0"
        with self.assertRaisesRegex(ConfigurationError, "helm_charts.metallb.version is 0.16.0 but kubernetes_platform.metallb.version pins 0.16.1"):
            load_versions(self.write(document))
        document = copy.deepcopy(self.matrix)
        document["kubernetes_platform"]["traefik"]["version"] = "3.7.14"
        with self.assertRaisesRegex(ConfigurationError, "helm_charts.traefik.app_version is 3.7.13 but kubernetes_platform.traefik.version pins 3.7.14"):
            load_versions(self.write(document))
        document = copy.deepcopy(self.matrix)
        document["helm_charts"]["loki"]["version_of"] = "grafana_charts.nothing.version"
        with self.assertRaisesRegex(ConfigurationError, "names grafana_charts.nothing.version, which the matrix does not have"):
            load_versions(self.write(document))

    def test_only_listed_components_are_horizontally_scalable(self) -> None:
        scalable = load_versions()["kubernetes_scaling"]["horizontally_scalable"]
        self.assertIn("gateway", scalable)
        for stateful in ("mimir-ingester", "loki-ingester", "tempo-ingester", "kafka", "object-storage", "database"):
            self.assertNotIn(stateful, scalable)

    def test_load_versions_rejects_unknown_field(self) -> None:
        document = copy.deepcopy(self.matrix)
        document["backends"]["loki"]["unexpected_field"] = "oops"
        with self.assertRaises(ConfigurationError):
            load_versions(self.write(document))

    def test_load_versions_rejects_malformed_version_string(self) -> None:
        document = copy.deepcopy(self.matrix)
        document["backends"]["loki"]["version"] = "latest"
        with self.assertRaises(ConfigurationError):
            load_versions(self.write(document))

    def test_os_support_declares_distinct_sets(self) -> None:
        matrix = load_versions()
        self.assertTrue(is_os_supported(matrix, "docker_hosts", "debian", "12", "amd64"))
        self.assertFalse(is_os_supported(matrix, "k3s_nodes", "debian", "12", "amd64"))

    def test_rhel_family_is_supported_for_docker_hosts_only(self) -> None:
        matrix = load_versions()
        for distribution in ("rocky", "almalinux"):
            for architecture in ("amd64", "arm64"):
                self.assertTrue(is_os_supported(matrix, "docker_hosts", distribution, "9", architecture))
                self.assertFalse(is_os_supported(matrix, "k3s_nodes", distribution, "9", architecture))
            self.assertFalse(is_os_supported(matrix, "docker_hosts", distribution, "8", "amd64"))

    def test_every_os_entry_states_its_evidence(self) -> None:
        matrix = load_versions()
        for target, entries in matrix["os_support"].items():
            for entry in entries:
                self.assertIn(entry["evidence"], ("container", "host", "declared"), (target, entry["distribution"]))
        document = copy.deepcopy(self.matrix)
        del document["os_support"]["docker_hosts"][0]["evidence"]
        with self.assertRaisesRegex(ConfigurationError, "'evidence' is a required property"):
            load_versions(self.write(document))
        document = copy.deepcopy(self.matrix)
        document["os_support"]["docker_hosts"][0]["evidence"] = "assumed"
        with self.assertRaises(ConfigurationError):
            load_versions(self.write(document))

    def test_automation_tools_and_collections_are_pinned_exactly(self) -> None:
        tools = load_versions()["automation_tools"]
        for name in ("ansible_core", "ansible_lint", "molecule", "molecule_plugins"):
            self.assertRegex(tools[name]["version"], r"^[0-9]+\.[0-9]+\.[0-9]+$")
            self.assertTrue(tools[name]["source"])
        self.assertLessEqual(
            {"ansible.posix", "community.general"}, {item["name"] for item in tools["collections"].values()},
        )
        for name in ("ansible_core", "collections"):
            document = copy.deepcopy(self.matrix)
            del document["automation_tools"][name]
            with self.assertRaises(ConfigurationError):
                load_versions(self.write(document))
        document = copy.deepcopy(self.matrix)
        document["automation_tools"]["collections"]["ansible_posix"]["version"] = ">=2.0"
        with self.assertRaises(ConfigurationError):
            load_versions(self.write(document))

    def test_host_artifacts_have_a_checksum_per_supported_architecture(self) -> None:
        matrix = load_versions()
        self.assertEqual(supported_architectures(matrix), ["amd64", "arm64"])
        self.assertEqual(artifact_checksum_problems(matrix), [])
        for artifact in ("k3s", "alloy"):
            document = copy.deepcopy(self.matrix)
            del document["host_artifacts"][artifact]["sha256"]["linux_arm64"]
            with self.assertRaisesRegex(
                ConfigurationError, f"host_artifacts.{artifact} has no checksum for linux_arm64",
            ):
                load_versions(self.write(document))

    def test_docker_repository_keys_are_pinned_by_fingerprint(self) -> None:
        docker = load_versions()["host_artifacts"]["docker_engine"]
        self.assertEqual(sorted(docker["signing_key_fingerprints"]), ["apt", "rpm"])
        for family in ("apt", "rpm"):
            document = copy.deepcopy(self.matrix)
            del document["host_artifacts"]["docker_engine"]["signing_key_fingerprints"][family]
            with self.assertRaises(ConfigurationError):
                load_versions(self.write(document))
        document = copy.deepcopy(self.matrix)
        document["host_artifacts"]["docker_engine"]["signing_key_fingerprints"]["apt"] = "not-a-fingerprint"
        with self.assertRaises(ConfigurationError):
            load_versions(self.write(document))

    def test_os_support_rejects_undeclared_target(self) -> None:
        matrix = load_versions()
        self.assertFalse(is_os_supported(matrix, "k3s_nodes", "fedora", "40", "amd64"))
        self.assertFalse(is_os_supported(matrix, "docker_hosts", "ubuntu", "20.04", "amd64"))
        self.assertFalse(is_os_supported(matrix, "docker_hosts", "ubuntu", "22.04", "riscv64"))

    def test_pin_change_requires_verification_reset(self) -> None:
        previous = copy.deepcopy(self.matrix)
        previous["backends"]["loki"]["runtime_verified"] = True
        updated = copy.deepcopy(previous)
        updated["backends"]["loki"]["version"] = "3.8.0"
        with self.assertRaisesRegex(ConfigurationError, "resetting runtime_verified"):
            validate_pin_changes(updated, previous)

    def test_pin_change_allows_reset_verification_flag(self) -> None:
        previous = copy.deepcopy(self.matrix)
        previous["backends"]["loki"]["runtime_verified"] = True
        updated = copy.deepcopy(previous)
        updated["backends"]["loki"]["version"] = "3.8.0"
        updated["backends"]["loki"]["runtime_verified"] = False
        validate_pin_changes(updated, previous)  # should not raise

    def test_pin_change_allows_bump_on_unverified_component(self) -> None:
        previous = copy.deepcopy(self.matrix)
        previous["backends"]["loki"]["runtime_verified"] = False
        updated = copy.deepcopy(previous)
        updated["backends"]["loki"]["version"] = "3.8.0"
        validate_pin_changes(updated, previous)  # should not raise

    def test_kubernetes_platform_pins_eks_and_ebs_csi_driver(self) -> None:
        matrix = load_versions()
        platform = matrix["kubernetes_platform"]
        for component in ("eks", "ebs_csi_driver"):
            self.assertIn(component, platform)
            self.assertTrue(platform[component]["version"])
            self.assertTrue(platform[component]["source"])

    def test_load_versions_rejects_missing_eks_pin(self) -> None:
        document = copy.deepcopy(self.matrix)
        del document["kubernetes_platform"]["eks"]
        with self.assertRaises(ConfigurationError):
            load_versions(self.write(document))

    def test_collectors_pin_alloy(self) -> None:
        alloy = load_versions()["collectors"]["alloy"]
        self.assertTrue(alloy["version"])
        self.assertTrue(alloy["source"])

    def test_load_versions_rejects_missing_alloy_pin(self) -> None:
        document = copy.deepcopy(self.matrix)
        del document["collectors"]["alloy"]
        with self.assertRaises(ConfigurationError):
            load_versions(self.write(document))

    def test_vault_entry_has_a_supported_range_and_a_tested_image(self) -> None:
        vault = load_versions()["secrets_store"]["vault"]
        self.assertEqual(vault["image"]["tag"], vault["version"])
        self.assertRegex(vault["image"]["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(vault["source"])
        for field in ("supported", "image"):
            document = copy.deepcopy(self.matrix)
            del document["secrets_store"]["vault"][field]
            with self.assertRaises(ConfigurationError):
                load_versions(self.write(document))

    def test_tested_vault_version_must_lie_in_the_supported_range(self) -> None:
        for minimum, below in (("9.0.0", "9.1.0"), ("1.0.0", "2.1.2")):
            document = copy.deepcopy(self.matrix)
            document["secrets_store"]["vault"]["supported"] = {"minimum": minimum, "below": below}
            with self.assertRaisesRegex(ConfigurationError, "outside the supported range"):
                load_versions(self.write(document))
        document = copy.deepcopy(self.matrix)
        document["secrets_store"]["vault"]["supported"] = {"minimum": "2.0.0", "below": "3.0.0"}
        load_versions(self.write(document))  # inside the range

    def test_vault_supports_compares_releases_numerically(self) -> None:
        matrix = load_versions()
        self.assertTrue(vault_supports(matrix, "2.1.2"))
        self.assertTrue(vault_supports(matrix, "2.1.10+ent"))
        self.assertFalse(vault_supports(matrix, "2.1.1"))
        self.assertFalse(vault_supports(matrix, "2.2.0"))

    def test_vault_test_image_tag_must_match_the_tested_version(self) -> None:
        document = copy.deepcopy(self.matrix)
        document["secrets_store"]["vault"]["image"]["tag"] = "2.1.1"
        with self.assertRaisesRegex(ConfigurationError, "image tag"):
            load_versions(self.write(document))

    def test_chart_and_backend_versions_agree_in_the_real_matrix(self) -> None:
        matrix = load_versions()
        self.assertEqual(chart_version_problems(matrix), [])
        for chart, backend in (("loki", "loki"), ("tempo_distributed", "tempo"), ("pyroscope", "pyroscope")):
            self.assertEqual(matrix["grafana_charts"][chart]["app_version"], matrix["backends"][backend]["version"])
            self.assertNotIn("app_version_exception", matrix["grafana_charts"][chart])
        # The one recorded difference, with its reason.
        self.assertIn("weekly", matrix["grafana_charts"]["mimir_distributed"]["app_version_exception"]["reason"])

    def test_unrecorded_chart_difference_fails_naming_the_chart_and_both_versions(self) -> None:
        document = copy.deepcopy(self.matrix)
        document["grafana_charts"]["tempo_distributed"]["app_version"] = "3.1.0"
        with self.assertRaises(ConfigurationError) as raised:
            load_versions(self.write(document))
        message = str(raised.exception)
        self.assertIn("grafana_charts.tempo_distributed packages tempo 3.1.0 but backends.tempo pins 3.0.3", message)
        self.assertIn("app_version_exception", message)

    def test_recorded_chart_difference_passes(self) -> None:
        document = copy.deepcopy(self.matrix)
        document["grafana_charts"]["tempo_distributed"].update(
            app_version="3.1.0", app_version_exception={"reason": "No chart release packages the pinned Tempo yet."},
        )
        load_versions(self.write(document))

    def test_stale_chart_exception_fails_naming_the_chart(self) -> None:
        document = copy.deepcopy(self.matrix)
        document["grafana_charts"]["loki"]["app_version_exception"] = {"reason": "Was needed for an earlier pin only."}
        with self.assertRaisesRegex(ConfigurationError, "grafana_charts.loki records an app_version_exception.*stale"):
            load_versions(self.write(document))

    def test_chart_exception_needs_a_real_reason(self) -> None:
        for exception in ({}, {"reason": ""}, {"reason": "tbd"}, {"reason": "A long enough reason.", "extra": 1}):
            with self.subTest(exception=exception):
                document = copy.deepcopy(self.matrix)
                document["grafana_charts"]["tempo_distributed"].update(app_version="3.1.0", app_version_exception=exception)
                with self.assertRaises(ConfigurationError):
                    load_versions(self.write(document))

    def test_every_compose_image_has_a_digest_and_its_pinned_version(self) -> None:
        matrix = load_versions()
        images = matrix["container_images"]
        pinned = {
            "mimir": matrix["backends"]["mimir"]["version"], "loki": matrix["backends"]["loki"]["version"],
            "tempo": matrix["backends"]["tempo"]["version"], "pyroscope": matrix["backends"]["pyroscope"]["version"],
            "grafana": matrix["grafana_charts"]["grafana"]["app_version"],
            "alloy": matrix["collectors"]["alloy"]["version"],
            "traefik": matrix["kubernetes_platform"]["traefik"]["version"],
            "seaweedfs": matrix["object_storage"]["version"],
        }
        for name, version in pinned.items():
            self.assertEqual(images[name]["tag"].lstrip("v"), version, name)
            self.assertRegex(images[name]["digest"], r"^sha256:[0-9a-f]{64}$")
        document = copy.deepcopy(self.matrix)
        del document["container_images"]["traefik"]["digest"]
        with self.assertRaises(ConfigurationError):
            load_versions(self.write(document))

    def test_check_pins_matches_tracked_consumers(self) -> None:
        self.assertEqual(check_pins(load_versions()), [])

    def test_check_pins_detects_divergence(self) -> None:
        matrix = load_versions()
        # Scope tracked_consumers to only the two paths this test populates, so
        # check_pins() does not also try (and fail) to read every other real
        # tracked consumer file, which this temp root never creates.
        scoped_paths = {
            "terraform/modules/object-storage/versions.tf",
            "terraform/modules/aws-s3-backends/versions.tf",
        }
        scoped_matrix = copy.deepcopy(matrix)
        scoped_matrix["tracked_consumers"] = [
            entry
            for entry in scoped_matrix["tracked_consumers"]
            if entry["path"] in scoped_paths
        ]
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            tf_dir = root_path / "terraform" / "modules" / "object-storage"
            tf_dir.mkdir(parents=True)
            (tf_dir / "versions.tf").write_text(
                'terraform {\n'
                '  required_version = "= 9.9.9"\n'
                '  required_providers {\n'
                '    aws = {\n'
                '      source  = "hashicorp/aws"\n'
                '      version = "= 6.12.0"\n'
                '    }\n'
                '  }\n'
                '}\n',
                encoding="utf-8",
            )
            second_dir = root_path / "terraform" / "modules" / "aws-s3-backends"
            second_dir.mkdir(parents=True)
            (second_dir / "versions.tf").write_text(
                (ROOT / "terraform" / "modules" / "aws-s3-backends" / "versions.tf").read_text(),
                encoding="utf-8",
            )
            mismatches = check_pins(scoped_matrix, root=root_path)
        self.assertEqual(len(mismatches), 1)
        self.assertIn("validation_tools.terraform.version", mismatches[0])
        self.assertIn("9.9.9", mismatches[0])


if __name__ == "__main__":
    unittest.main()
