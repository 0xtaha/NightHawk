"""Chart checks: every release of the development profile templates, validates, and pins its images.

The platform's own chart is checked whenever the pinned Helm is installed (`fetch-tools`).
The upstream charts and the schema validation need the network, so they are opt-in:

    NIGHTHAWK_CHART_TESTS=1 python -m unittest tests.test_helm_charts
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from nighthawk.__main__ import main
from nighthawk.config import ROOT, load_versions

HELM, KUBECONFORM = ROOT / ".tools" / "helm", ROOT / ".tools" / "kubeconform"
CHART = ROOT / "helm" / "nighthawk-platform"
NETWORK = os.environ.get("NIGHTHAWK_CHART_TESTS") == "1"
CACHE = ROOT / ".generated" / "chart-tests"
IMAGE = "registry.example.com/nighthawk@sha256:" + "a" * 64


@unittest.skipUnless(HELM.exists(), "run `python -m nighthawk fetch-tools` to install the pinned Helm")
class ChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.output = Path(cls.temp.name) / "contracts"
        assert main([
            "render-contracts", "--config", str(ROOT / "tests" / "k8s" / "platform.yaml"), "--output", str(cls.output),
            "--collector-tenant", "acme", "--collector-datastream", "web",
        ]) == 0
        cls.directory = cls.output / "kubernetes"
        cls.releases = yaml.safe_load((cls.directory / "releases.yaml").read_text(encoding="utf-8"))
        cls.matrix = load_versions()
        cls.environment = {
            **os.environ, "HELM_CACHE_HOME": str(CACHE / "helm-cache"), "HELM_CONFIG_HOME": str(CACHE / "helm-config"),
            "HELM_DATA_HOME": str(CACHE / "helm-data"),
        }

    def helm(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(HELM), *args], capture_output=True, text=True, env=self.environment, timeout=300)

    def template(self, release: dict, chart: str) -> str:
        values = [argument for name in release["values"] for argument in ("--values", str(self.directory / "values" / name))]
        extra = ["--set", f"images.platform={IMAGE}"] if release["chart"] == self.releases["platform_chart"] else []
        result = self.helm("template", release["name"], chart, "--namespace", release["namespace"], *values, *extra)
        self.assertEqual(result.returncode, 0, f"{release['name']}: {result.stderr[-800:]}")
        return result.stdout

    def platform_releases(self) -> list[dict]:
        return [item for item in self.releases["releases"] if item["chart"] == self.releases["platform_chart"]]

    def allowed_digests(self) -> set[str]:
        return {
            image["digest"] for section in ("container_images", "kubernetes_images") for image in self.matrix[section].values()
        } | {IMAGE.rsplit("@", 1)[1]}

    def container_images(self, value: object) -> list[str]:
        """Every image a container of any workload in the manifests runs."""
        found: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("containers", "initContainers") and isinstance(item, list):
                    found += [container["image"] for container in item if isinstance(container, dict) and "image" in container]
                found += self.container_images(item)
        elif isinstance(value, list):
            for item in value:
                found += self.container_images(item)
        return found

    def assert_images_pinned(self, name: str, manifests: str) -> None:
        for reference in self.container_images([item for item in yaml.safe_load_all(manifests) if item]):
            self.assertIn("@sha256:", reference, f"{name}: {reference} is not pinned by digest")
            self.assertIn(reference.rsplit("@", 1)[1], self.allowed_digests(), f"{name}: {reference} is not in the compatibility matrix")

    def test_the_platform_chart_lints(self) -> None:
        result = self.helm(
            "lint", str(CHART), "--values", str(self.directory / "values" / "platform.yaml"),
            "--values", str(self.directory / "values" / "nighthawk-foundation.yaml"),
        )
        self.assertEqual(result.returncode, 0, result.stdout[-800:])

    def test_every_platform_stage_templates_with_images_by_matrix_digest(self) -> None:
        kinds: dict[str, list[str]] = {}
        for release in self.platform_releases():
            manifests = self.template(release, str(CHART))
            documents = [item for item in yaml.safe_load_all(manifests) if item]
            self.assertTrue(documents, release["name"])
            kinds[release["name"]] = sorted({item["kind"] for item in documents})
            self.assert_images_pinned(release["name"], manifests)
        self.assertEqual(kinds["nighthawk-network"], ["NetworkPolicy"])
        self.assertIn("VaultStaticSecret", kinds["nighthawk-foundation"])
        self.assertIn("Certificate", kinds["nighthawk-gateway-identity"])
        self.assertEqual(kinds["grafana-provision"], ["Job"])

    def test_no_template_holds_a_secret_and_no_pod_is_privileged(self) -> None:
        for release in self.platform_releases():
            for item in (item for item in yaml.safe_load_all(self.template(release, str(CHART))) if item):
                self.assertNotEqual(item["kind"], "Secret", release["name"])
                pod = (item.get("spec") or {}).get("template", {}).get("spec") or {}
                for container in pod.get("containers", []) + pod.get("initContainers", []):
                    context = container.get("securityContext") or {}
                    self.assertFalse(context.get("privileged"), (release["name"], container["name"]))
                    self.assertIs(context.get("allowPrivilegeEscalation"), False, (release["name"], container["name"]))

    def test_every_namespace_denies_by_default(self) -> None:
        for name in ("nighthawk-network", "nighthawk-gateway-network"):
            release = next(item for item in self.releases["releases"] if item["name"] == name)
            policies = [item for item in yaml.safe_load_all(self.template(release, str(CHART))) if item]
            deny = next(item for item in policies if item["metadata"]["name"] == "default-deny")
            self.assertEqual(deny["spec"], {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]})
            for policy in policies:
                self.assertEqual(policy["kind"], "NetworkPolicy")
                if policy["metadata"]["name"] != "default-deny":
                    self.assertTrue(policy["metadata"]["annotations"]["nighthawk.io/contract-rules"])

    def chart_package(self, key: str) -> Path:
        pin = self.releases["charts"][key]
        directory = CACHE / "charts" / f"{key}-{pin['version']}"
        directory.mkdir(parents=True, exist_ok=True)
        if not list(directory.glob("*.tgz")):
            result = self.helm("pull", pin["chart"], "--repo", pin["repository"], "--version", pin["version"], "--destination", str(directory))
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
        (package,) = directory.glob("*.tgz")
        self.assertEqual(hashlib.sha256(package.read_bytes()).hexdigest(), pin["sha256"], f"{key}: the chart is not the locked one")
        return package

    @unittest.skipUnless(NETWORK, "set NIGHTHAWK_CHART_TESTS=1 to pull the upstream charts")
    def test_every_upstream_release_matches_its_locked_digest_templates_and_pins_its_images(self) -> None:
        for release in self.releases["addons"] + self.releases["releases"]:
            if release["chart"] == self.releases["platform_chart"]:
                continue
            with self.subTest(release=release["name"]):
                manifests = self.template(release, str(self.chart_package(release["chart"])))
                self.assert_images_pinned(release["name"], manifests)

    @unittest.skipUnless(NETWORK and KUBECONFORM.exists(), "set NIGHTHAWK_CHART_TESTS=1 to validate against the Kubernetes schema")
    def test_platform_manifests_validate_against_the_kubernetes_schema(self) -> None:
        (CACHE / "schemas").mkdir(parents=True, exist_ok=True)
        for release in self.platform_releases():
            with self.subTest(release=release["name"]):
                result = subprocess.run(
                    # Custom resources of the add-ons have no schema in the Kubernetes catalogue.
                    [str(KUBECONFORM), "-strict", "-summary", "-ignore-missing-schemas", "-cache", str(CACHE / "schemas"), "-"],
                    input=self.template(release, str(CHART)), capture_output=True, text=True, timeout=300,
                )
                self.assertEqual(result.returncode, 0, result.stdout[-800:])
                self.assertIn("Invalid: 0, Errors: 0", result.stdout)
