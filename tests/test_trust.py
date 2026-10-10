from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from nighthawk.authz import RequestFacts, certificate_fingerprint, certificate_from_header, decide, parse_policy
from nighthawk.config import ConfigurationError, load_platform
from nighthawk.gateway import policy_bundle
from nighthawk.trust import (
    authority_certificate, generate_credential, generate_storage_identity, issue_certificate, revoke_certificate,
)
from nighthawk.vault import pki_roles
from tests.fakes import (
    FakeVault, add_stream, basic, example_document, fake_client, forwarded_certificate, materialize_plain, run_cli,
    write_document,
)

PATH = "nighthawk/local"


class TrustFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = example_document()
        self.fake: FakeVault | None = None
        self.load()

    def load(self) -> None:
        self.config = write_document(self.root, self.data)
        self.platform = load_platform(self.config)
        if self.fake is None:
            self.fake = FakeVault(self.platform)
        # As an operator re-applies the rendered roles after the document changes.
        self.fake.roles = pki_roles(self.platform)
        self.client = fake_client(self.platform, self.fake)

    def stored(self) -> dict[str, str]:
        return self.fake.values(PATH)

    def cli(self, *argv: str) -> tuple[int, str, str]:
        return run_cli(self.fake, [argv[0], "--config", str(self.config), *argv[1:]])


class GenerateCredentialTests(TrustFixture):
    def test_declared_credential_is_stored_in_vault_at_its_reference(self) -> None:
        reference = generate_credential(self.platform, "example-ingest", self.client)
        self.assertEqual((reference.path, reference.key), (PATH, "example-ingest"))
        self.assertGreaterEqual(len(self.stored()["example-ingest"]), 40)
        self.assertEqual(list(self.stored()), ["example-ingest"])

    def test_existing_value_is_never_replaced(self) -> None:
        generate_credential(self.platform, "example-ingest", self.client)
        before = self.stored()
        with self.assertRaisesRegex(ConfigurationError, "already holds a value"):
            generate_credential(self.platform, "example-ingest", self.client)
        self.assertEqual(self.stored(), before)

    def test_undeclared_credential_writes_nothing(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown credential"):
            generate_credential(self.platform, "missing", self.client)
        self.assertEqual(self.fake.calls, [])

    def test_value_is_never_printed_or_placed_in_a_url(self) -> None:
        status, output, errors = self.cli("generate-credential", "--credential", "example-query")
        self.assertEqual((status, errors), (0, ""))
        secret = self.stored()["example-query"]
        self.assertNotIn(secret, output)
        self.assertNotIn(secret, self.fake.sent())
        self.assertIn("in Vault at nighthawk/local key example-query", output)


class StorageIdentityTests(TrustFixture):
    def test_declared_identity_is_stored_as_access_and_secret_key(self) -> None:
        generate_storage_identity(self.platform, "tempo-storage", self.client)
        value = json.loads(self.stored()["tempo-storage"])
        self.assertEqual(sorted(value), ["access_key", "secret_key"])
        self.assertRegex(value["access_key"], r"^[A-Z0-9]{20}$")
        self.assertGreaterEqual(len(value["secret_key"]), 40)

    def test_existing_value_is_not_replaced(self) -> None:
        generate_storage_identity(self.platform, "tempo-storage", self.client)
        before = self.stored()
        with self.assertRaisesRegex(ConfigurationError, "already holds a value"):
            generate_storage_identity(self.platform, "tempo-storage", self.client)
        self.assertEqual(self.stored(), before)

    def test_secret_that_is_not_a_storage_identity_is_refused(self) -> None:
        for reference in ("example-ingest", "missing"):
            with self.subTest(reference=reference), self.assertRaisesRegex(ConfigurationError, "is not a storage identity"):
                generate_storage_identity(self.platform, reference, self.client)
        self.assertEqual(self.fake.calls, [])

    def test_keys_are_never_printed(self) -> None:
        status, output, _ = self.cli("generate-storage-identity", "--identity", "tempo-storage")
        self.assertEqual(status, 0)
        value = json.loads(self.stored()["tempo-storage"])
        self.assertNotIn(value["access_key"], output)
        self.assertNotIn(value["secret_key"], output)


class IssueCertificateTests(TrustFixture):
    def setUp(self) -> None:
        super().setUp()
        self.output = self.root / "issued"

    def issue(self, **what):
        return issue_certificate(self.platform, self.output, what.pop("valid_days", 7), self.client, **what)

    def sign_requests(self) -> list[dict]:
        return [body for _, path, body in self.fake.calls if "/sign/" in path]

    def test_collector_certificate_carries_only_the_declared_identity(self) -> None:
        issued = self.issue(credential_id="example-ingest")
        certificate = x509.load_pem_x509_certificate(issued.certificate_path.read_bytes())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(
            names.get_values_for_type(x509.UniformResourceIdentifier),
            ["spiffe://nighthawk/example/application/collector"],
        )
        self.assertEqual(len(list(names)), 1)
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        self.assertEqual(list(usage), [ExtendedKeyUsageOID.CLIENT_AUTH])
        lifetime = certificate.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
        self.assertAlmostEqual(lifetime.total_seconds(), 7 * 86400, delta=60)
        self.assertEqual(issued.key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(issued.certificate_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            issued.fingerprint,
            certificate_fingerprint(certificate_from_header(forwarded_certificate(issued.certificate_path))),
        )
        certificate.verify_directly_issued_by(self.fake.ca_certificate)
        self.assertEqual([path for _, path, _ in self.fake.calls if "/sign/" in path], ["nighthawk-pki/sign/nighthawk-collector"])

    def test_server_certificate_is_for_the_gateway_and_grafana_hostnames_only(self) -> None:
        issued = self.issue(server=True)
        certificate = x509.load_pem_x509_certificate(issued.certificate_path.read_bytes())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(
            sorted(names.get_values_for_type(x509.DNSName)), ["gateway.nighthawk.internal", "grafana.nighthawk.internal"],
        )
        self.assertEqual(len(list(names)), 2)
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        self.assertEqual(list(usage), [ExtendedKeyUsageOID.SERVER_AUTH])
        self.assertEqual(issued.certificate_path.name, "gateway-server.crt.pem")
        self.assertEqual([path for _, path, _ in self.fake.calls if "/sign/" in path], ["nighthawk-pki/sign/nighthawk-server"])

    def test_storage_certificate_is_for_the_binding_endpoint_hostnames_only(self) -> None:
        issued = self.issue(storage=True)
        certificate = x509.load_pem_x509_certificate(issued.certificate_path.read_bytes())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(names.get_values_for_type(x509.DNSName), ["seaweedfs"])
        self.assertEqual(len(list(names)), 1)

    def test_storage_certificate_is_refused_for_cloud_storage(self) -> None:
        self.data.update(deployment="aws")
        self.data["storage"]["provider"] = "aws"
        for name, binding in self.data["storage"]["bindings"].items():
            binding.update(
                identity={"type": "irsa", "ref": f"arn:aws:iam::123456789012:role/nighthawk-{name.split('-')[0]}"},
                endpoint="https://s3.eu-west-1.amazonaws.com", region="eu-west-1",
                tls={"enabled": True, "ca_secret_ref": None}, force_path_style=False,
            )
            binding["capabilities"]["workload_identity"] = True
        self.load()
        with self.assertRaisesRegex(ConfigurationError, "only for local SeaweedFS"):
            self.issue(storage=True)

    def test_private_key_stays_local_and_only_a_signing_request_is_sent(self) -> None:
        issued = self.issue(credential_id="example-ingest")
        (request,) = self.sign_requests()
        self.assertEqual(sorted(request), ["common_name", "csr", "exclude_cn_from_sans", "format", "ttl"])
        self.assertIn("BEGIN CERTIFICATE REQUEST", request["csr"])
        self.assertEqual(request["ttl"], "168h")
        key = issued.key_path.read_text(encoding="ascii")
        self.assertIn("PRIVATE KEY", key)
        for _, path, body in self.fake.calls:
            self.assertNotIn("PRIVATE KEY", json.dumps(body or {}), path)
            self.assertNotIn(key.splitlines()[1], json.dumps(body or {}), path)

    def test_undeclared_identity_requests_nothing_from_vault(self) -> None:
        for credential in ("example-query", "missing"):
            with self.subTest(credential=credential), self.assertRaisesRegex(ConfigurationError, "declares a certificate identity"):
                self.issue(credential_id=credential)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.output.exists())

    def test_missing_validity_and_overwrite_fail(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "explicit validity period is required; there is no default"):
            self.issue(server=True, valid_days=None)
        with self.assertRaisesRegex(ConfigurationError, "at least one day"):
            self.issue(server=True, valid_days=0)
        with self.assertRaisesRegex(ConfigurationError, "choose exactly one"):
            self.issue(server=True, storage=True)
        self.assertEqual(self.sign_requests(), [])
        self.issue(server=True)
        with self.assertRaisesRegex(ConfigurationError, "refusing to overwrite"):
            self.issue(server=True)
        self.assertEqual(len(self.sign_requests()), 1)

    def test_validity_cannot_outlive_the_authority(self) -> None:
        self.fake.new_authority(days=5)
        with self.assertRaisesRegex(ConfigurationError, "outlives the certificate authority"):
            self.issue(server=True, valid_days=30)
        self.assertEqual(self.sign_requests(), [])

    def test_missing_authority_is_reported(self) -> None:
        self.fake.ca_certificate = None
        with self.assertRaisesRegex(ConfigurationError, "PKI mount nighthawk-pki has no certificate authority"):
            authority_certificate(self.client)
        with self.assertRaisesRegex(ConfigurationError, "has no certificate authority"):
            self.issue(server=True)

    def test_certificate_that_differs_from_the_request_is_refused_and_nothing_is_written(self) -> None:
        other_key = ec.generate_private_key(ec.SECP256R1())
        tampered = {
            "names or identity": {"extra_names": [x509.DNSName("extra.example.com")]},
            "usage": {"usage": ExtendedKeyUsageOID.CLIENT_AUTH},
            "validity": {"hours": 24 * 30},
            "validity ": {"hours": 1},
            "public key": {"public_key": other_key.public_key()},
            "issuer": {"signer": other_key},
        }
        for what, tamper in tampered.items():
            with self.subTest(what=what):
                self.fake.tamper = tamper
                with self.assertRaisesRegex(ConfigurationError, f"whose {what.strip()} differs from the request; nothing was written"):
                    self.issue(server=True)
                self.assertFalse(self.output.exists())

    def test_role_in_vault_that_does_not_cover_the_request_fails_explicitly(self) -> None:
        self.fake.roles["nighthawk-server"]["allowed_domains"] = ["gateway.nighthawk.internal"]
        with self.assertRaisesRegex(ConfigurationError, "grafana.nighthawk.internal not allowed by this role"):
            self.issue(server=True)
        self.assertFalse(self.output.exists())

    def test_issuing_through_the_command_line_prints_paths_and_fingerprint_only(self) -> None:
        status, output, errors = self.cli(
            "issue-certificate", "--credential", "example-ingest", "--valid-days", "7", "--output-dir", str(self.output),
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("SHA-256 fingerprint: ", output)
        self.assertNotIn("PRIVATE KEY", output)
        status, _, errors = self.cli("issue-certificate", "--server", "--output-dir", str(self.output))
        self.assertEqual(status, 1)
        self.assertIn("explicit validity period is required", errors)


class RotationAndRevocationTests(TrustFixture):
    """Rotation and revocation across issuance, the policy render, and the decision."""

    def policy(self):
        self.load()
        return parse_policy(policy_bundle(self.platform, materialize_plain(self.platform, self.root / "materialized")))

    def facts(self, credential: str, certificate: str | None = None) -> RequestFacts:
        return RequestFacts(
            "port-8443", "metrics", "ingest", (basic(credential),), (),
            () if certificate is None else (certificate,),
        )

    def test_credential_rotation_overlaps_then_retires_the_old_credential(self) -> None:
        add_stream(self.data, "example", "batch", "example-batch")
        self.data["secrets"]["example-batch-ingest-2"] = {"path": PATH, "key": "example-batch-ingest-2"}
        replacement = {
            "id": "example-batch-ingest-2", "secret_ref": "example-batch-ingest-2",
            "tenant": "example", "datastream": "batch", "permission": "ingest",
        }
        self.data["credentials"].append(replacement)
        policy = self.policy()
        for credential in ("example-batch-ingest", "example-batch-ingest-2"):
            self.assertEqual(decide(policy, self.facts(credential)).backend_id, "example-batch")
        self.data["credentials"] = [item for item in self.data["credentials"] if item["id"] != "example-batch-ingest"]
        policy = self.policy()
        self.assertEqual(decide(policy, self.facts("example-batch-ingest")).status, 401)
        self.assertEqual(decide(policy, self.facts("example-batch-ingest-2")).backend_id, "example-batch")

    def test_certificate_renewal_and_revocation(self) -> None:
        first = issue_certificate(self.platform, self.root / "first", 7, self.client, credential_id="example-ingest")
        renewed = issue_certificate(self.platform, self.root / "renewed", 7, self.client, credential_id="example-ingest")
        policy = self.policy()
        # A renewed certificate with the same identity needs no document change.
        for issued in (first, renewed):
            decision = decide(policy, self.facts("example-ingest", forwarded_certificate(issued.certificate_path)))
            self.assertEqual(decision.backend_id, "example-application")
        self.data["gateway"]["revoked_certificate_fingerprints"] = [first.fingerprint]
        policy = self.policy()
        self.assertEqual(decide(policy, self.facts("example-ingest", forwarded_certificate(first.certificate_path))).status, 403)
        self.assertEqual(decide(policy, self.facts("example-ingest", forwarded_certificate(renewed.certificate_path))).status, 200)

    def test_revoking_in_vault_reports_the_fingerprint_to_list(self) -> None:
        issued = issue_certificate(self.platform, self.root / "issued", 7, self.client, credential_id="example-ingest")
        certificate = x509.load_pem_x509_certificate(issued.certificate_path.read_bytes())
        self.assertEqual(revoke_certificate(self.client, issued.certificate_path), issued.fingerprint)
        (serial,) = self.fake.revoked
        self.assertEqual(int(serial.replace(":", ""), 16), certificate.serial_number)
        self.assertRegex(serial, r"^[0-9a-f]{2}(:[0-9a-f]{2})+$")
        status, output, _ = self.cli("revoke-certificate", "--certificate", str(issued.certificate_path))
        self.assertEqual(status, 0)
        self.assertIn(f"gateway.revoked_certificate_fingerprints: {issued.fingerprint}", output)
        self.assertEqual(issued.fingerprint, certificate.fingerprint(hashes.SHA256()).hex())

    def test_unreadable_certificate_is_not_sent_for_revocation(self) -> None:
        (self.root / "not-a-certificate.pem").write_text("nonsense", encoding="utf-8")
        for name in ("not-a-certificate.pem", "absent.pem"):
            with self.subTest(name=name), self.assertRaisesRegex(ConfigurationError, "not a readable certificate"):
                revoke_certificate(self.client, self.root / name)
        self.assertEqual(self.fake.revoked, [])

    def test_gateway_enforces_revocation_without_reaching_vault(self) -> None:
        first = issue_certificate(self.platform, self.root / "first", 7, self.client, credential_id="example-ingest")
        other = issue_certificate(self.platform, self.root / "other", 7, self.client, credential_id="example-ingest")
        self.data["gateway"]["revoked_certificate_fingerprints"] = [first.fingerprint]
        policy = self.policy()
        self.fake.reachable = False
        self.fake.calls.clear()
        self.assertEqual(decide(policy, self.facts("example-ingest", forwarded_certificate(first.certificate_path))).status, 403)
        self.assertEqual(decide(policy, self.facts("example-ingest", forwarded_certificate(other.certificate_path))).status, 200)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
