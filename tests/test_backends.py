from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from nighthawk.__main__ import main
from nighthawk.backends import render_backends
from nighthawk.config import BUCKET_OWNER, ConfigurationError, ROOT, load_platform
from nighthawk.storage import SIGNAL_BACKEND
from tests.fakes import example_document, four_stream_document, write_document


class BackendConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = four_stream_document()

    def platform(self):
        return load_platform(write_document(self.root, self.data))

    def documents(self) -> dict[str, dict]:
        return {name: yaml.safe_load(text) for name, text in render_backends(self.platform()).items()}

    def test_one_configuration_per_enabled_backend_and_deterministic(self) -> None:
        first = render_backends(self.platform())
        self.assertEqual(sorted(first), ["loki", "mimir", "pyroscope", "tempo"])
        self.data["tenants"].reverse()
        self.data["credentials"].reverse()
        self.assertEqual(first, render_backends(self.platform()))

    def test_disabled_signal_has_no_configuration(self) -> None:
        data = example_document()
        del data["tenants"][0]["datastreams"][0]["signals"]["profiles"]
        del data["gateway"]["upstreams"]["profiles"]
        self.data = data
        self.assertEqual(sorted(self.documents()), ["loki", "mimir", "tempo"])

    def test_buckets_are_exactly_the_backends_bindings(self) -> None:
        platform = self.platform()
        rendered = render_backends(platform)
        for backend, text in rendered.items():
            owned = {
                binding.bucket for name, binding in platform.bindings.items()
                if SIGNAL_BACKEND[BUCKET_OWNER[name]] == backend
            }
            others = {binding.bucket for binding in platform.bindings.values()} - owned
            for bucket in owned:
                self.assertIn(bucket, text, backend)
            for bucket in others:
                self.assertNotIn(bucket, text, backend)
            self.assertIn("seaweedfs:8333", text)

    def test_credentials_come_from_the_environment_only(self) -> None:
        for backend, text in render_backends(self.platform()).items():
            self.assertEqual(text.count("${NIGHTHAWK_S3_ACCESS_KEY}"), 1, backend)
            self.assertEqual(text.count("${NIGHTHAWK_S3_SECRET_KEY}"), 1, backend)
            document = yaml.safe_load(text)
            flat = str(document)
            self.assertNotIn("mimir-storage", flat)

    def test_tenancy_federation_overrides_and_retention_settings(self) -> None:
        documents = self.documents()
        self.assertIs(documents["mimir"]["multitenancy_enabled"], True)
        self.assertIs(documents["mimir"]["tenant_federation"]["enabled"], False)
        self.assertEqual(documents["mimir"]["runtime_config"]["file"], "/etc/nighthawk/overrides/mimir-overrides.yaml")
        self.assertIn("compactor", documents["mimir"])
        self.assertIs(documents["loki"]["auth_enabled"], True)
        self.assertIs(documents["loki"]["querier"]["multi_tenant_queries_enabled"], False)
        self.assertIs(documents["loki"]["compactor"]["retention_enabled"], True)
        self.assertEqual(documents["loki"]["compactor"]["delete_request_store"], "s3")
        self.assertEqual(documents["loki"]["runtime_config"]["file"], "/etc/nighthawk/overrides/loki-overrides.yaml")
        self.assertIs(documents["tempo"]["multitenancy_enabled"], True)
        self.assertIs(documents["tempo"]["query_frontend"]["multi_tenant_queries_enabled"], False)
        self.assertEqual(
            documents["tempo"]["overrides"]["per_tenant_override_config"], "/etc/nighthawk/overrides/tempo-overrides.yaml",
        )
        self.assertIs(documents["pyroscope"]["multitenancy_enabled"], True)
        self.assertEqual(
            documents["pyroscope"]["runtime_config"]["file"], "/etc/nighthawk/overrides/pyroscope-overrides.yaml",
        )
        for document in documents.values():
            self.assertNotIn("kafka", str(document).lower())

    def test_listen_ports_follow_the_gateway_upstreams(self) -> None:
        documents = self.documents()
        self.assertEqual(documents["mimir"]["server"]["http_listen_port"], 8080)
        self.assertEqual(documents["loki"]["server"]["http_listen_port"], 3100)
        self.assertEqual(documents["tempo"]["server"]["http_listen_port"], 3200)
        self.assertEqual(documents["pyroscope"]["server"]["http_listen_port"], 4040)
        protocols = documents["tempo"]["distributor"]["receivers"]["otlp"]["protocols"]
        self.assertEqual(protocols["grpc"]["endpoint"], "0.0.0.0:4317")
        self.assertEqual(protocols["http"]["endpoint"], "0.0.0.0:4318")

    def test_unreviewed_pin_or_architecture_fails_naming_the_backend(self) -> None:
        matrix = yaml.safe_load((ROOT / "config" / "versions.yaml").read_text(encoding="utf-8"))
        for backend, field, value in (
            ("mimir", "version", "9.9.9"), ("mimir", "compose_architecture", "ingest-storage"),
            ("loki", "version", "9.9.9"), ("tempo", "compose_architecture", "distributed-kafka"),
            ("pyroscope", "storage", "v1"),
        ):
            changed = yaml.safe_load(yaml.safe_dump(matrix))
            changed["backends"][backend][field] = value
            path = self.root / "versions.yaml"
            path.write_text(yaml.safe_dump(changed), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, f"{backend} configuration was reviewed for {field}"):
                render_backends(self.platform(), path)

    def test_other_deployments_are_skipped_with_a_message(self) -> None:
        self.data["deployment"] = "self-hosted-k8s"
        self.assertIsNone(render_backends(self.platform()))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([
                "render-contracts", "--config", str(write_document(self.root, self.data)),
                "--output", str(self.root / "out"),
            ]), 0)
        self.assertIn("Backend configuration is not rendered for the self-hosted-k8s deployment", output.getvalue())
        self.assertFalse((self.root / "out" / "backends").exists())
        self.assertTrue((self.root / "out" / "mimir-overrides.yaml").exists())


if __name__ == "__main__":
    unittest.main()
