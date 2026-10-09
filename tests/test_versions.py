from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from nighthawk.config import (
    ROOT, ConfigurationError, check_pins, is_os_supported, load_versions, validate_pin_changes,
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

    def test_missing_tool_checksum_fails(self) -> None:
        for tool in ("sops", "age"):
            document = copy.deepcopy(self.matrix)
            del document["secrets_tools"][tool]["sha256"]
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
