from __future__ import annotations

import io
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from nighthawk.__main__ import main
from nighthawk.collector import ALLOY_CONFIGS, PROFILES, REDACT_RECEIVERS, render_collector
from nighthawk.config import ConfigurationError, load_platform
from tests.fakes import example_document, write_document

BASE_PROFILES = [name for name, profile in PROFILES.items() if not profile.privileged]
EXPORTERS = ("prometheus.remote_write", "loki.write", "otelcol.exporter.otlphttp", "pyroscope.write")
SIGNALS = ("metrics", "logs", "traces", "profiles")


def block(text: str, header: str) -> str:
    """Return the body of the top-level block that starts with `header`."""
    start = text.index(header)
    depth, index = 0, text.index("{", start)
    for position in range(index, len(text)):
        depth += {"{": 1, "}": -1}.get(text[position], 0)
        if depth == 0:
            return text[index:position + 1]
    raise AssertionError(f"unterminated block {header}")


class StaticSourceTests(unittest.TestCase):
    def sources(self) -> dict[Path, str]:
        return {path: path.read_text(encoding="utf-8") for path in sorted(ALLOY_CONFIGS.glob("*/*.alloy"))}

    def test_every_profile_ships_sources(self) -> None:
        self.assertEqual(sorted(path.name for path in ALLOY_CONFIGS.iterdir() if path.is_dir()), sorted(PROFILES))

    def test_sources_forward_only_to_redaction_and_hold_no_exporter_or_secret(self) -> None:
        for path, text in self.sources().items():
            with self.subTest(path=path.relative_to(ALLOY_CONFIGS)):
                for exporter in EXPORTERS:
                    self.assertNotIn(exporter, text)
                for forbidden in ("password", "basic_auth", "X-Scope-OrgID", "stage.structured_metadata"):
                    self.assertNotIn(forbidden, text)
                targets = re.findall(r"forward_to\s*=\s*\[([^\]]*)\]", text)
                self.assertTrue(targets)
                self.assertEqual({item.strip() for item in targets}, {REDACT_RECEIVERS[path.stem]})

    def test_base_profiles_are_unprivileged(self) -> None:
        for path, text in self.sources().items():
            self.assertEqual("pyroscope.ebpf" in text, path.parent.name == "profiling-ebpf", path)

    def test_node_and_cluster_profiles_do_not_overlap(self) -> None:
        def facts(profile: str) -> tuple[set[str], set[str], str]:
            text = "\n".join(content for path, content in self.sources().items() if path.parent.name == profile)
            return set(re.findall(r'\brole\s*=\s*"([a-z]+)"', text)), set(re.findall(r'job_name\s*=\s*"([^"]+)"', text)), text

        node_roles, node_jobs, node_text = facts("k8s-node")
        cluster_roles, cluster_jobs, _ = facts("k8s-cluster")
        self.assertTrue(node_roles and cluster_roles and node_jobs and cluster_jobs)
        self.assertFalse(node_roles & cluster_roles)
        self.assertFalse(node_jobs & cluster_jobs)
        # Every Kubernetes discovery in the node profile is restricted to its own node.
        discoveries = re.findall(r'discovery\.kubernetes "[a-z_]+" \{.*?\n\}', node_text, re.S)
        self.assertTrue(discoveries)
        for discovery in discoveries:
            self.assertRegex(discovery, r'field\s*=\s*"(spec\.nodeName|metadata\.name)=" \+ coalesce\(sys\.env\("HOSTNAME"\)')


class RenderCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = example_document()
        self.stream = self.data["tenants"][0]["datastreams"][0]

    def render(self, profile: str = "docker", **options) -> dict[str, str]:
        platform = load_platform(write_document(Path(self.temp.name), self.data))
        options.setdefault("entry_point", "local-gateway")
        return render_collector(platform, "example", "application", profile, **options)

    def test_output_is_deterministic_for_every_profile(self) -> None:
        for profile in BASE_PROFILES:
            with self.subTest(profile=profile):
                first = self.render(profile)
                self.data["credentials"].reverse()
                self.assertEqual(first, self.render(profile))
                self.assertIn("datastream.alloy", first)

    def test_unknown_profile_or_pair_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown collector profile 'windows'"):
            self.render("windows")
        platform = load_platform(write_document(Path(self.temp.name), self.data))
        with self.assertRaisesRegex(ConfigurationError, "unknown tenant/datastream example/missing"):
            render_collector(platform, "example", "missing", "docker")

    def test_cli_writes_nothing_on_failure_and_refuses_to_overwrite(self) -> None:
        config = write_document(Path(self.temp.name), self.data)
        output = Path(self.temp.name) / "collector"
        arguments = [
            "render-collector", "--config", str(config), "--tenant", "example", "--datastream", "application",
            "--entry-point", "local-gateway", "--output", str(output),
        ]
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main([*arguments, "--profile", "windows"]), 1)
        self.assertIn("unknown collector profile", errors.getvalue())
        self.assertFalse(output.exists())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([*arguments, "--profile", "vm"]), 0)
        self.assertEqual(sorted(path.name for path in output.iterdir()), [
            "datastream.alloy", "logs.alloy", "metrics.alloy", "profiles.alloy",
        ])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main([*arguments, "--profile", "vm"]), 1)

    def test_only_enabled_signals_are_collected(self) -> None:
        del self.stream["signals"]["profiles"]
        del self.stream["signals"]["logs"]
        del self.data["gateway"]["upstreams"]["profiles"]
        files = self.render()
        self.assertEqual(sorted(files), ["datastream.alloy", "metrics.alloy"])
        text = "\n".join(files.values())
        for absent in ("pyroscope.", "loki."):
            self.assertNotIn(absent, text)
        outputs = block(files["datastream.alloy"], 'otelcol.receiver.otlp "default"')
        self.assertRegex(outputs, r"metrics\s+= \[")
        self.assertRegex(outputs, r"traces\s+= \[")
        self.assertNotRegex(outputs, r"logs\s+= \[")

    def test_otlp_is_received_over_grpc_and_http(self) -> None:
        receiver = block(self.render()["datastream.alloy"], 'otelcol.receiver.otlp "default"')
        self.assertIn('grpc {\n\t\tendpoint = "0.0.0.0:4317"', receiver)
        self.assertIn('http {\n\t\tendpoint = "0.0.0.0:4318"', receiver)
        for signal in ("metrics", "logs", "traces"):
            self.assertRegex(receiver, rf"{signal}\s+= \[otelcol\.processor\.memory_limiter\.default\.input\]")

    def test_profiles_only_datastream_has_no_otlp_receiver(self) -> None:
        self.stream["signals"] = {"profiles": self.stream["signals"]["profiles"]}
        self.assertNotIn("otelcol.", self.render()["datastream.alloy"])

    def test_no_secret_value_or_tenant_header_is_rendered(self) -> None:
        for profile in BASE_PROFILES:
            text = "\n".join(self.render(profile).values())
            self.assertNotIn("X-Scope-OrgID", text)
            self.assertNotIn("tenant_id", text)
            self.assertNotRegex(text, r'\bpassword\s*=')
            self.assertEqual(text.count('password_file = sys.env("NIGHTHAWK_CREDENTIAL_FILE")'), 4)
            self.assertIn('username      = "example-ingest"', text)
            self.assertIn("https://gateway.nighthawk.internal:8443", text)

    def test_every_exporter_is_bounded(self) -> None:
        text = self.render()["datastream.alloy"]
        remote_write = block(text, 'prometheus.remote_write "gateway"')
        for bound in ("capacity", "max_shards", "max_backoff", "sample_age_limit", "max_keepalive_time"):
            self.assertRegex(remote_write, rf"\b{bound}\s+= ")
        for header in ('loki.write "gateway"', 'pyroscope.write "gateway"'):
            for bound in ("max_backoff_period", "max_backoff_retries"):
                self.assertRegex(block(text, header), rf"\b{bound}\s+= ")
        self.assertRegex(block(text, 'loki.write "gateway"'), r"\bbatch_size\s+= ")
        otlp = block(text, 'otelcol.exporter.otlphttp "gateway"')
        for bound in ("queue_size", "max_interval", "max_elapsed_time"):
            self.assertRegex(otlp, rf"\b{bound}\s+= ")
        self.assertEqual(len(re.findall(r'^(?:prometheus\.remote_write|loki\.write|otelcol\.exporter\.\w+|pyroscope\.write) "', text, re.M)), 4)

    def test_every_drop_field_is_removed_in_every_pipeline_case_insensitively(self) -> None:
        self.stream["collection"]["drop_fields"] = ["password", "user.email"]
        text = self.render()["datastream.alloy"]
        alternation = "password|user[.]email"
        for header in ('prometheus.relabel "redact"', 'loki.relabel "redact"', 'pyroscope.relabel "redact"'):
            body = block(text, header)
            self.assertIn('action = "labeldrop"', body)
            self.assertIn(f'regex  = "(?i)({alternation})"', body)
        process = block(text, 'loki.process "redact"')
        self.assertIn('"PASSWORD", "Password", "USER.EMAIL", "User.Email", "password", "user.email"', process)
        self.assertEqual(process.count(alternation), 2)
        transform = block(text, 'otelcol.processor.transform "redact"')
        self.assertIn('error_mode = "propagate"', transform)
        for context in (
            "resource", "scope", "span", "spanevent", "datapoint", "log",
        ):
            self.assertIn(f'delete_matching_keys({context}.attributes, "(?i)^({alternation})$")', transform)
        self.assertEqual(transform.count("delete_matching_keys(resource.attributes"), 3)
        self.assertIn("replace_pattern(log.body", transform)

    def test_exporters_are_reachable_only_through_redaction(self) -> None:
        for profile in BASE_PROFILES:
            files = self.render(profile)
            generated = files["datastream.alloy"]
            references = {
                "prometheus.remote_write.gateway.receiver": 'prometheus.relabel "redact"',
                "loki.write.gateway.receiver": 'loki.process "redact"',
                "pyroscope.write.gateway.receiver": 'pyroscope.relabel "redact"',
                "otelcol.exporter.otlphttp.gateway.input": 'otelcol.processor.transform "redact"',
            }
            for reference, owner in references.items():
                with self.subTest(profile=profile, reference=reference):
                    inside = block(generated, owner).count(reference)
                    self.assertGreaterEqual(inside, 1)
                    self.assertEqual("\n".join(files.values()).count(reference), inside)
            self.assertEqual(generated.count("loki.process.redact.receiver"), 1)
            self.assertIn("loki.process.redact.receiver", block(generated, 'loki.relabel "redact"'))
            self.assertEqual(generated.count("otelcol.processor.transform.redact.input"), 3)
            self.assertNotIn("otelcol.processor.transform.redact.input", block(generated, 'otelcol.receiver.otlp "default"'))

    def test_remote_profiles_present_a_client_certificate(self) -> None:
        for profile in ("remote-cluster", "vm", "external-service"):
            text = self.render(profile)["datastream.alloy"]
            self.assertEqual(text.count('cert_file = sys.env("NIGHTHAWK_CLIENT_CERT_FILE")'), 4)
            self.assertEqual(text.count('key_file  = sys.env("NIGHTHAWK_CLIENT_KEY_FILE")'), 4)
        del self.data["credentials"][0]["certificate_identity"]
        self.assertNotIn("cert_file", self.render("docker")["datastream.alloy"])
        for profile in ("remote-cluster", "vm", "external-service"):
            with self.assertRaisesRegex(ConfigurationError, "declares a certificate identity"):
                self.render(profile)

    def test_ambiguous_choices_must_be_made_explicitly(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "several gateway entry points"):
            self.render(entry_point=None)
        self.data["secrets"]["example-ingest-next"] = {"file": "secrets/local.sops.yaml", "key": "example-ingest-next"}
        self.data["credentials"].append({
            "id": "example-ingest-next", "secret_ref": "example-ingest-next",
            "tenant": "example", "datastream": "application", "permission": "ingest",
        })
        with self.assertRaisesRegex(ConfigurationError, "several ingestion credentials qualify"):
            self.render()
        self.assertIn('"example-ingest-next"', self.render(credential_id="example-ingest-next")["datastream.alloy"])
        with self.assertRaisesRegex(ConfigurationError, "not an ingestion credential"):
            self.render(credential_id="example-query")

    def test_privileged_profiling_is_separate_and_opt_in(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "allow_privileged_profiling"):
            self.render("profiling-ebpf")
        self.stream["collection"]["allow_privileged_profiling"] = True
        files = self.render("profiling-ebpf")
        self.assertEqual(sorted(files), ["datastream.alloy", "profiles.alloy"])
        self.assertIn("pyroscope.ebpf", files["profiles.alloy"])
        self.assertNotIn("otelcol.", files["datastream.alloy"])
        self.assertNotIn("prometheus.", files["datastream.alloy"])
        for profile in BASE_PROFILES:
            self.assertNotIn("pyroscope.ebpf", "\n".join(self.render(profile).values()))
        del self.stream["signals"]["profiles"]
        del self.data["gateway"]["upstreams"]["profiles"]
        with self.assertRaisesRegex(ConfigurationError, "allow_privileged_profiling"):
            self.render("profiling-ebpf")

    def test_self_monitoring_is_explicit_and_limited_to_platform_profiles(self) -> None:
        for profile in BASE_PROFILES:
            text = self.render(profile)["datastream.alloy"]
            self.assertIn('prometheus.scrape "collector"', text)
            self.assertNotIn('prometheus.scrape "platform"', text)
            self.assertNotIn("mimir:8080", text)
        platform = block(self.render("docker", self_monitoring=True)["datastream.alloy"], 'prometheus.scrape "platform"')
        for target in ('"mimir:8080"', '"loki:3100"', '"tempo:3200"', '"pyroscope:4040"', '"authz:9180"',
                       'sys.env("NIGHTHAWK_GATEWAY_METRICS_ADDRESS")'):
            self.assertIn(target, platform)
        self.assertIn("prometheus.relabel.redact.receiver", platform)
        with self.assertRaisesRegex(ConfigurationError, "self-monitoring is available only"):
            self.render("vm", self_monitoring=True)


if __name__ == "__main__":
    unittest.main()
