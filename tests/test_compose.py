from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from nighthawk.config import ROOT, load_versions

COMPOSE_DIR = ROOT / "docker-compose"
BASE = COMPOSE_DIR / "docker-compose.yaml"
S3_OVERRIDE = COMPOSE_DIR / "docker-compose.s3.yaml"
ENVIRONMENT = {
    "NIGHTHAWK_RENDERED_DIR": "/rendered", "NIGHTHAWK_UID": "1000",
    "NIGHTHAWK_GID": "1000", "NIGHTHAWK_GATEWAY_HOSTNAME": "gateway.test", "NIGHTHAWK_GRAFANA_HOSTNAME": "grafana.test",
    "NIGHTHAWK_GATEWAY_PORT": "9443",
}
OPTIONAL_PROFILES = {"tools", "sample", "host-collection"}
ONE_SHOT = {"volume-init", "storage-init", "wait-backends", "wait-alloy"}


def resolve(secrets: str, *files) -> dict:
    """Resolve with the Compose CLI itself; `config` needs no running daemon."""
    command = ["docker", "compose"]
    for path in files:
        command += ["--file", str(path)]
    result = subprocess.run(
        [*command, "--profile", "*", "config", "--format", "json"], capture_output=True, text=True,
        env={**os.environ, **ENVIRONMENT, "NIGHTHAWK_SECRETS_DIR": secrets}, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return yaml.safe_load(result.stdout)


@unittest.skipUnless(shutil.which("docker"), "the Compose CLI is not installed")
class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # `config` insists that env files exist; they are empty stand-ins here.
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.secrets = temporary.name
        backends = Path(cls.secrets) / "runtime" / "backends"
        backends.mkdir(parents=True)
        for backend in ("mimir", "loki", "tempo", "pyroscope"):
            (backends / f"{backend}.env").write_text("", encoding="utf-8")
        cls.config = resolve(cls.secrets, BASE)
        cls.services = cls.config["services"]
        cls.default = {
            name: service for name, service in cls.services.items()
            if not set(service.get("profiles", [])) & OPTIONAL_PROFILES
        }

    def test_stack_contains_every_component_and_no_kafka(self) -> None:
        for name in ("mimir", "loki", "tempo", "pyroscope", "seaweedfs", "traefik", "authz", "grafana", "alloy"):
            self.assertIn(name, self.default)
        text = BASE.read_text(encoding="utf-8").lower()
        self.assertNotIn("kafka", text)
        self.assertNotIn("strimzi", text)

    def test_every_image_is_pinned_by_digest_from_the_matrix_or_built_here(self) -> None:
        images = load_versions()["container_images"]
        allowed = {f"{item['repository']}:{item['tag']}@{item['digest']}" for item in images.values()}
        for name, service in self.services.items():
            if "build" in service:
                self.assertTrue(service["image"].startswith("localhost/nighthawk/"), name)
            else:
                self.assertIn(service["image"], allowed, name)
        dockerfile = (COMPOSE_DIR / "nighthawk.Dockerfile").read_text(encoding="utf-8")
        base = images["python_base"]
        self.assertIn(f"FROM {base['repository']}:{base['tag']}@{base['digest']}", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER [1-9][0-9]*:[1-9][0-9]*$")

    def test_only_the_gateway_is_published_and_on_loopback_by_default(self) -> None:
        published = {name: service["ports"] for name, service in self.services.items() if service.get("ports")}
        self.assertEqual(list(published), ["traefik"])
        (port,) = published["traefik"]
        # The port follows the environment file, which the quickstart fills from the platform document.
        self.assertEqual((port["host_ip"], str(port["published"]), port["target"]), ("127.0.0.1", "9443", 9443))
        text = BASE.read_text(encoding="utf-8")
        self.assertIn("${NIGHTHAWK_BIND_ADDRESS:-127.0.0.1}:${NIGHTHAWK_GATEWAY_PORT:?}:${NIGHTHAWK_GATEWAY_PORT:?}", text)
        self.assertNotIn("8443", text)
        self.assertEqual(self.services["grafana"]["environment"]["GF_SERVER_ROOT_URL"], "https://grafana.test:9443/")

    def test_grafana_provisioning_takes_appended_options(self) -> None:
        service = self.services["grafana-init"]
        self.assertEqual(service["entrypoint"][:4], ["python", "-m", "nighthawk", "provision-grafana"])
        self.assertFalse(service.get("command"))

    def test_backends_storage_and_auth_are_on_internal_networks_only(self) -> None:
        networks = self.config["networks"]
        internal = {name for name, network in networks.items() if network.get("internal")}
        self.assertEqual(internal, {"backend", "storage", "authz"})
        for name in ("mimir", "loki", "tempo", "pyroscope"):
            self.assertEqual(set(self.services[name]["networks"]), {"backend", "storage"}, name)
        self.assertEqual(set(self.services["seaweedfs"]["networks"]), {"storage"})
        self.assertEqual(set(self.services["authz"]["networks"]), {"authz"})
        self.assertEqual(set(self.services["grafana"]["networks"]), {"edge"})
        # Only the proxy and the self-monitoring collector share a network with the auth service.
        on_authz = {name for name, service in self.services.items() if "authz" in (service.get("networks") or {})}
        self.assertEqual(on_authz, {"authz", "traefik", "alloy"})
        # The proxy is the only default service on both the edge and backend networks besides the collector.
        bridging = {
            name for name, service in self.default.items()
            if {"edge", "backend"} <= set(service.get("networks") or {})
        }
        self.assertEqual(bridging, {"traefik", "alloy"})

    def test_default_services_are_unprivileged_and_never_get_the_runtime_socket(self) -> None:
        for name, service in self.default.items():
            self.assertFalse(service.get("privileged", False), name)
            self.assertFalse(service.get("cap_add"), name)
            for volume in service.get("volumes", []):
                self.assertNotIn("docker.sock", volume.get("source", ""), name)
                self.assertNotIn(volume.get("source"), ("/", "/sys", "/proc"), name)
            expected = "0:0" if name == "volume-init" else "1000:1000"
            self.assertEqual(service.get("user"), expected, name)
        host = self.services["alloy-host"]
        self.assertEqual(host["profiles"], ["host-collection"])
        self.assertTrue(host["privileged"])

    def test_long_running_services_restart_and_one_shots_do_not(self) -> None:
        for name, service in self.default.items():
            if name in ONE_SHOT:
                self.assertNotIn("restart", service, name)
            else:
                self.assertEqual(service.get("restart"), "unless-stopped", name)
                self.assertIn("mem_limit", service, name)

    def test_start_order_is_gated_on_health_or_completion(self) -> None:
        def condition(service: str, dependency: str) -> str:
            return self.services[service]["depends_on"][dependency]["condition"]

        self.assertEqual(condition("storage-init", "seaweedfs"), "service_healthy")
        for backend in ("mimir", "loki", "tempo", "pyroscope"):
            self.assertEqual(condition(backend, "storage-init"), "service_completed_successfully")
            self.assertEqual(condition(backend, "volume-init"), "service_completed_successfully")
            self.assertNotIn("healthcheck", self.services[backend], "these images have no probe binary")
        self.assertEqual(condition("traefik", "authz"), "service_healthy")
        self.assertEqual(condition("traefik", "wait-backends"), "service_completed_successfully")
        self.assertEqual(condition("grafana", "traefik"), "service_healthy")
        for name in ("seaweedfs", "authz", "traefik", "grafana"):
            self.assertIn("healthcheck", self.services[name], name)

    def test_no_secret_value_appears_in_the_compose_files(self) -> None:
        for path in (BASE, S3_OVERRIDE, COMPOSE_DIR / ".env.example"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)(password|secret_key|access_key|token)\s*[:=]\s*[\"']?[A-Za-z0-9+/_-]{12,}")
            self.assertNotIn("BEGIN", text)
        for name, service in self.services.items():
            for key, value in (service.get("environment") or {}).items():
                if re.search(r"(?i)password|secret|token|key", key):
                    self.assertTrue(key.endswith("_FILE") and str(value).startswith("/"), f"{name}.{key}")
        for backend in ("mimir", "loki", "tempo", "pyroscope"):
            self.assertRegex(BASE.read_text(encoding="utf-8"), rf"runtime/backends/{backend}\.env")

    def test_backends_cannot_read_storage_keys_or_the_identity_file(self) -> None:
        for backend in ("mimir", "loki", "tempo", "pyroscope"):
            sources = [volume.get("source", "") for volume in self.services[backend]["volumes"]]
            self.assertIn(f"{self.secrets}/runtime/storage-trust", sources)
            self.assertNotIn(f"{self.secrets}/runtime/storage", sources)
            self.assertFalse([source for source in sources if source.startswith(f"{self.secrets}/secrets")])

    def test_pyroscope_runs_v2_storage(self) -> None:
        self.assertIn("-architecture.storage=v2", self.services["pyroscope"]["command"])

    def test_s3_override_removes_local_storage(self) -> None:
        config = resolve(self.secrets, BASE, S3_OVERRIDE)
        services = config["services"]
        self.assertNotIn("seaweedfs", services)
        self.assertNotIn("storage-init", services)
        self.assertNotIn("storage", config["networks"])
        self.assertNotIn("seaweedfs-data", config["volumes"])
        for backend in ("mimir", "loki", "tempo", "pyroscope"):
            self.assertEqual(list(services[backend]["depends_on"]), ["volume-init"])
            self.assertNotIn("SSL_CERT_FILE", services[backend].get("environment") or {})
            self.assertEqual(set(services[backend]["networks"]), {"backend", "egress"})
        for name, service in services.items():
            self.assertFalse({"seaweedfs", "storage-init"} & set(service.get("depends_on") or {}), name)


if __name__ == "__main__":
    unittest.main()
