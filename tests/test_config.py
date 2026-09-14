from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from nighthawk.__main__ import main
from nighthawk.config import (
    ConfigurationError, ROOT, load_network, load_platform, validate_migration,
)
from nighthawk.retention import pyroscope_overrides


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "platform.yaml"
        self.data = yaml.safe_load((ROOT / "config" / "tenants.example.yaml").read_text())

    def write(self, data: dict | None = None) -> Path:
        self.path.write_text(yaml.safe_dump(self.data if data is None else data), encoding="utf-8")
        return self.path

    def assert_invalid(self, message: str) -> None:
        with self.assertRaisesRegex(ConfigurationError, message):
            load_platform(self.write())

    def add_stream(self, tenant_id: str, stream_id: str, backend_id: str) -> None:
        stream = copy.deepcopy(self.data["tenants"][0]["datastreams"][0])
        stream.update(id=stream_id, backend_id=backend_id)
        tenant = next((item for item in self.data["tenants"] if item["id"] == tenant_id), None)
        if tenant is None:
            tenant = {"id": tenant_id, "datastreams": []}
            self.data["tenants"].append(tenant)
        tenant["datastreams"].append(stream)
        for permission in ("ingest", "query"):
            credential_id = f"{tenant_id}-{stream_id}-{permission}"
            self.data["secrets"][credential_id] = {
                "file": "secrets/local.sops.yaml", "key": credential_id,
            }
            self.data["credentials"].append({
                "id": credential_id, "secret_ref": credential_id,
                "tenant": tenant_id, "datastream": stream_id, "permission": permission,
            })

    def test_schema_documents_are_valid(self) -> None:
        for name in ("platform.schema.json", "network.schema.json"):
            with self.subTest(name=name):
                Draft202012Validator.check_schema(json.loads((ROOT / "config" / name).read_text()))

    def test_example_preserves_explicit_retention_and_mapping(self) -> None:
        platform = load_platform(ROOT / "config" / "tenants.example.yaml")
        self.assertEqual(platform.storage_provider, "seaweedfs")
        self.assertEqual(platform.streams[0].signals["profiles"].retention_hours, 168)
        self.assertEqual(platform.streams[0].signals["logs"].retention_hours, 72)
        self.assertEqual({credential.backend_id for credential in platform.credentials}, {"example-application"})
        self.assertEqual(platform.bindings["tempo-traces"].identity.ref, "tempo-storage")

    def test_two_customers_two_datastreams_have_independent_policies(self) -> None:
        self.add_stream("example", "infrastructure", "example-infrastructure")
        self.add_stream("second", "application", "second-application")
        self.add_stream("second", "infrastructure", "second-infrastructure")
        self.data["tenants"][1]["datastreams"][0]["signals"]["profiles"]["retention"] = "720h"
        platform = load_platform(self.write())
        self.assertEqual(len({stream.backend_id for stream in platform.streams}), 4)
        self.assertEqual(len(platform.credentials), 8)
        for credential in platform.credentials:
            stream = next(stream for stream in platform.streams if (
                stream.tenant, stream.datastream
            ) == (credential.tenant, credential.datastream))
            self.assertEqual(credential.backend_id, stream.backend_id)
        profiles = {(stream.tenant, stream.datastream): stream.signals["profiles"].retention_hours for stream in platform.streams}
        self.assertEqual(profiles[("second", "application")], 720)
        self.assertEqual(profiles[("example", "application")], 168)

    def test_unknown_properties_fail(self) -> None:
        self.data["implicit_retention"] = "168h"
        self.assert_invalid("Additional properties")

    def test_retention_is_required(self) -> None:
        del self.data["tenants"][0]["datastreams"][0]["signals"]["metrics"]["retention"]
        self.assert_invalid("required property")

    def test_invalid_durations_fail(self) -> None:
        signal = self.data["tenants"][0]["datastreams"][0]["signals"]["logs"]
        for duration in ("0h", "-1h", "23h", "1d", "1.5h", "9999999h", "10000000h"):
            with self.subTest(duration=duration):
                signal["retention"] = duration
                with self.assertRaises(ConfigurationError):
                    load_platform(self.write())

    def test_boolean_is_not_an_integer_limit(self) -> None:
        self.data["tenants"][0]["datastreams"][0]["signals"]["metrics"]["query_concurrency"] = True
        self.assert_invalid("not of type 'integer'")

    def test_unapproved_collection_fails(self) -> None:
        self.data["tenants"][0]["datastreams"][0]["collection"]["approved"] = False
        self.assert_invalid("True was expected")

    def test_duplicate_tenants_fail(self) -> None:
        self.data["tenants"].append(copy.deepcopy(self.data["tenants"][0]))
        self.assert_invalid("duplicate tenant")

    def test_duplicate_datastreams_fail(self) -> None:
        self.data["tenants"][0]["datastreams"].append(copy.deepcopy(self.data["tenants"][0]["datastreams"][0]))
        self.assert_invalid("duplicate datastream")

    def test_duplicate_backend_ids_fail(self) -> None:
        self.add_stream("second", "application", "example-application")
        self.assert_invalid("duplicate backend ID")

    def test_ambiguous_or_header_injected_ids_fail(self) -> None:
        for backend_id in ("a|b", "a:b", "a/b", "a_b", "example\n", "EXAMPLE"):
            with self.subTest(backend_id=backend_id):
                self.data["tenants"][0]["datastreams"][0]["backend_id"] = backend_id
                with self.assertRaises(ConfigurationError):
                    load_platform(self.write())

    def test_duplicate_credential_ids_fail(self) -> None:
        self.data["credentials"][1]["id"] = "example-ingest"
        self.assert_invalid("duplicate credential")

    def test_unknown_secret_fails(self) -> None:
        self.data["credentials"][0]["secret_ref"] = "unknown"
        self.assert_invalid("unknown secret")

    def test_ingestion_and_query_cannot_share_a_secret(self) -> None:
        self.data["credentials"][1]["secret_ref"] = "example-ingest"
        self.assert_invalid("distinct secret references")

    def test_secret_aliases_fail(self) -> None:
        self.data["secrets"]["example-query"] = copy.deepcopy(self.data["secrets"]["example-ingest"])
        self.assert_invalid("must not alias")

    def test_unknown_credential_tenant_fails(self) -> None:
        self.data["credentials"][0]["tenant"] = "unknown"
        self.assert_invalid("unknown tenant/datastream")

    def test_missing_query_credential_fails(self) -> None:
        self.data["credentials"].pop()
        self.assert_invalid("separate ingestion and query credentials")

    def test_certificate_must_belong_to_its_tenant(self) -> None:
        self.data["credentials"][0]["certificate_identity"] = "spiffe://nighthawk/second/application/collector"
        self.assert_invalid("certificate must identify")

    def test_ingestion_certificate_cannot_authorize_queries(self) -> None:
        self.data["credentials"][1]["certificate_identity"] = "spiffe://nighthawk/example/application/query"
        self.assert_invalid("certificate must identify")

    def test_duplicate_certificate_fails(self) -> None:
        extra = copy.deepcopy(self.data["credentials"][0])
        extra.update(id="extra", secret_ref="extra")
        self.data["secrets"]["extra"] = {"file": "secrets/local.sops.yaml", "key": "extra"}
        self.data["credentials"].append(extra)
        self.assert_invalid("duplicate certificate identity")

    def test_local_profiles_cannot_use_cloud_storage(self) -> None:
        for deployment in ("docker", "self-hosted-k8s"):
            with self.subTest(deployment=deployment):
                self.data["deployment"] = deployment
                self.data["storage"]["provider"] = "aws"
                self.assert_invalid("requires seaweedfs")

    def test_cloud_requires_aws_adapter(self) -> None:
        self.data["deployment"] = "aws"
        self.assert_invalid("requires aws")

    def test_missing_signal_bucket_fails(self) -> None:
        del self.data["storage"]["bindings"]["mimir-ruler"]
        self.assert_invalid("missing storage bindings")

    def test_overlapping_buckets_fail(self) -> None:
        self.data["storage"]["bindings"]["mimir-ruler"]["bucket"] = "nighthawk-mimir-blocks"
        self.assert_invalid("must not overlap")

    def test_bucket_name_cannot_be_ip(self) -> None:
        self.data["storage"]["bindings"]["mimir-ruler"]["bucket"] = "127.0.0.1"
        self.assert_invalid("must not be an IP")

    def test_plaintext_and_credential_bearing_endpoints_fail(self) -> None:
        for endpoint in ("http://seaweedfs:8333", "https://user:pass@host", "https://host/path", "https://host:invalid", "https://host:0", "https://host/?x=y"):
            with self.subTest(endpoint=endpoint):
                self.data["storage"]["bindings"]["tempo-traces"]["endpoint"] = endpoint
                with self.assertRaises(ConfigurationError):
                    load_platform(self.write())

    def test_unknown_ca_fails(self) -> None:
        self.data["storage"]["bindings"]["tempo-traces"]["tls"]["ca_secret_ref"] = "unknown"
        self.assert_invalid("unknown secret")

    def test_storage_identities_are_separated_by_backend(self) -> None:
        self.data["storage"]["bindings"]["tempo-traces"]["identity"]["ref"] = "mimir-storage"
        self.assert_invalid("shared across signal backends")

    def test_gateway_credentials_cannot_reuse_storage_identity(self) -> None:
        self.data["credentials"][0]["secret_ref"] = "mimir-storage"
        self.assert_invalid("must not reuse object-storage")

    def test_aws_binding_contract(self) -> None:
        self.data["deployment"] = "aws"
        self.data["storage"]["provider"] = "aws"
        for name, binding in self.data["storage"]["bindings"].items():
            owner = name.split("-")[0]
            binding["identity"] = {"type": "irsa", "ref": f"arn:aws:iam::123456789012:role/nighthawk-{owner}"}
            binding["capabilities"]["workload_identity"] = True
            binding["endpoint"] = "https://s3.eu-west-1.amazonaws.com"
            binding["region"] = "eu-west-1"
            binding["tls"]["ca_secret_ref"] = None
            binding["force_path_style"] = False
        platform = load_platform(self.write())
        self.assertEqual(platform.storage_provider, "aws")
        self.assertEqual(platform.bindings["tempo-traces"].identity.type, "irsa")
        self.data["storage"]["bindings"]["tempo-traces"]["identity"]["ref"] = "not-an-arn"
        self.assert_invalid("IRSA role ARN")

    def test_storage_output_import_is_validated_and_rendered(self) -> None:
        storage = copy.deepcopy(self.data["storage"])
        storage["bindings"]["tempo-traces"]["bucket"] = "different-tempo-bucket"
        storage_path = Path(self.temp.name) / "storage.json"
        storage_path.write_text(json.dumps(storage), encoding="utf-8")
        self.write()
        platform = load_platform(self.path, storage_path)
        self.assertEqual(platform.bindings["tempo-traces"].bucket, "different-tempo-bucket")
        output = Path(self.temp.name) / "imported"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                "render-contracts", "--config", str(self.path),
                "--storage-output", str(storage_path), "--output", str(output),
            ]), 0)
        rendered = json.loads((output / "platform.json").read_text())
        self.assertEqual(rendered["bindings"]["tempo-traces"]["bucket"], "different-tempo-bucket")
        storage["provider"] = "aws"
        storage_path.write_text(json.dumps(storage), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "requires seaweedfs"):
            load_platform(self.path, storage_path)

    def test_storage_output_wrapper_is_rejected(self) -> None:
        storage_path = Path(self.temp.name) / "storage.json"
        storage_path.write_text(json.dumps({"storage": {"value": self.data["storage"]}}), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "terraform output -json storage"):
            load_platform(self.write(), storage_path)

    def test_backend_id_changes_are_migrations(self) -> None:
        previous = load_platform(self.write())
        self.data["tenants"][0]["datastreams"][0]["backend_id"] = "new-backend"
        with self.assertRaisesRegex(ConfigurationError, "data migration"):
            validate_migration(load_platform(self.write()), previous)

    def test_backend_id_cannot_be_reassigned(self) -> None:
        previous = load_platform(self.write())
        self.data["tenants"][0]["id"] = "renamed"
        for credential in self.data["credentials"]:
            credential["tenant"] = "renamed"
            credential.pop("certificate_identity", None)
        with self.assertRaisesRegex(ConfigurationError, "cannot be reassigned"):
            validate_migration(load_platform(self.write()), previous)

    def test_new_backend_is_not_a_migration(self) -> None:
        previous = load_platform(self.write())
        self.add_stream("second", "application", "second-application")
        validate_migration(load_platform(self.write()), previous)

    def test_duplicate_yaml_key_fails(self) -> None:
        self.path.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "duplicate key"):
            load_platform(self.path)

    def test_nonstring_yaml_key_fails(self) -> None:
        self.path.write_text("true: value\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "keys must be strings"):
            load_platform(self.path)

    def test_recursive_yaml_fails(self) -> None:
        self.path.write_text("loop: &loop [*loop]\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "recursive YAML"):
            load_platform(self.path)

    def test_nonfinite_yaml_fails(self) -> None:
        self.path.write_text("value: .nan\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "non-finite"):
            load_platform(self.path)

    def test_network_duplicates_fail(self) -> None:
        data = yaml.safe_load((ROOT / "config" / "network.yaml").read_text())
        data["rules"].append(copy.deepcopy(data["rules"][0]))
        self.write(data)
        with self.assertRaisesRegex(ConfigurationError, "duplicate network rule"):
            load_network(self.path)
        data["rules"][-1]["id"] = "different-id"
        self.write(data)
        with self.assertRaisesRegex(ConfigurationError, "duplicate network flow"):
            load_network(self.path)

    def test_rendering_is_deterministic_and_does_not_overwrite(self) -> None:
        self.add_stream("example", "infrastructure", "example-infrastructure")
        self.add_stream("second", "application", "second-application")
        self.add_stream("second", "infrastructure", "second-infrastructure")
        first = Path(self.temp.name) / "first"
        second = Path(self.temp.name) / "second"
        self.write()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["render-contracts", "--config", str(self.path), "--output", str(first)]), 0)
            self.data["tenants"].reverse()
            self.data["credentials"].reverse()
            for tenant in self.data["tenants"]:
                tenant["datastreams"].reverse()
            self.write()
            self.assertEqual(main(["render-contracts", "--config", str(self.path), "--output", str(second)]), 0)
        self.assertEqual(
            {path.name for path in first.iterdir()},
            {"platform.json", "network.json", "ports.md", "pyroscope-overrides.yaml"},
        )
        for path in first.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes())
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["render-contracts", "--config", str(self.path), "--output", str(first)]), 1)
        self.assertIn("error:", errors.getvalue())

    def test_failed_validation_does_not_create_output(self) -> None:
        self.data["credentials"][0]["secret_ref"] = "missing"
        output = Path(self.temp.name) / "invalid"
        with redirect_stderr(io.StringIO()) as errors:
            result = main(["render-contracts", "--config", str(self.write()), "--output", str(output)])
        self.assertEqual(result, 1)
        self.assertIn("unknown secret", errors.getvalue())
        self.assertFalse(output.exists())

    def test_cli_missing_file_fails_with_actionable_error(self) -> None:
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["validate", "--config", str(self.path)]), 1)
        self.assertIn("cannot load", errors.getvalue())

    def test_cli_checks_retained_history_for_reassigned_ids(self) -> None:
        historical = Path(self.temp.name) / "historical.yaml"
        self.add_stream("retired", "application", "retired-application")
        historical.write_text(yaml.safe_dump(self.data), encoding="utf-8")
        self.data["tenants"].pop()
        self.data["credentials"] = [
            credential for credential in self.data["credentials"] if credential["tenant"] != "retired"
        ]
        recent = Path(self.temp.name) / "recent.yaml"
        recent.write_text(yaml.safe_dump(self.data), encoding="utf-8")
        self.add_stream("replacement", "application", "retired-application")
        with redirect_stderr(io.StringIO()) as errors:
            result = main([
                "validate", "--config", str(self.write()),
                "--previous", str(recent), "--previous", str(historical),
            ])
        self.assertEqual(result, 1)
        self.assertIn("cannot be reassigned", errors.getvalue())

    def test_pyroscope_uses_v2_per_backend_retention_without_defaults(self) -> None:
        self.add_stream("example", "infrastructure", "example-infrastructure")
        self.add_stream("second", "application", "second-application")
        self.add_stream("second", "infrastructure", "second-infrastructure")
        hours = (24, 48, 72, 168)
        index = 0
        expected = {}
        for tenant in self.data["tenants"]:
            for stream in tenant["datastreams"]:
                duration = f"{hours[index]}h"
                stream["signals"]["profiles"]["retention"] = duration
                expected[stream["backend_id"]] = {"retention_period": duration}
                index += 1
        self.assertEqual(pyroscope_overrides(load_platform(self.write())), {"overrides": expected})
        del self.data["tenants"][0]["datastreams"][0]["signals"]["profiles"]
        del expected["example-application"]
        self.assertEqual(pyroscope_overrides(load_platform(self.write())), {"overrides": expected})

    def test_pyroscope_unknown_version_or_storage_mode_fails(self) -> None:
        platform = load_platform(self.write())
        versions = yaml.safe_load((ROOT / "config" / "versions.yaml").read_text())
        versions_path = Path(self.temp.name) / "versions.yaml"
        for field, value in (("version", "latest"), ("storage", "v1"), ("retention_override", "compactor_blocks_retention_period")):
            with self.subTest(field=field):
                changed = copy.deepcopy(versions)
                changed["backends"]["pyroscope"][field] = value
                versions_path.write_text(yaml.safe_dump(changed), encoding="utf-8")
                with self.assertRaisesRegex(ConfigurationError, "source-verified 2.3.1"):
                    pyroscope_overrides(platform, versions_path)


if __name__ == "__main__":
    unittest.main()
