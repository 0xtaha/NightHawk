from __future__ import annotations

import datetime
import functools
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from nighthawk.__main__ import main
from nighthawk.authz import RequestFacts, certificate_fingerprint, certificate_from_header, decide, parse_policy
from nighthawk.config import ConfigurationError, load_platform
from nighthawk.gateway import policy_bundle
from nighthawk.trust import generate_credential, init_ca, issue_certificate, secret_key_exists
from tests.fakes import (
    add_stream, basic, example_document, fake_sops, forwarded_certificate, materialize_plain, write_document,
)

RECIPIENTS = ["age1recipient"]


class TrustFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = example_document()
        self.load()

    def load(self) -> None:
        self.platform = load_platform(write_document(self.root, self.data))

    def stored(self) -> dict:
        return yaml.safe_load((self.root / "secrets" / "local.sops.yaml").read_text(encoding="utf-8"))


class GenerateCredentialTests(TrustFixture):
    def test_declared_credential_is_encrypted_at_its_reference(self) -> None:
        target = generate_credential(self.platform, "example-ingest", RECIPIENTS, root=self.root, runner=fake_sops)
        self.assertEqual(target, self.root / "secrets" / "local.sops.yaml")
        self.assertGreaterEqual(len(self.stored()["example-ingest"]), 43)
        # A second credential is added to the existing file without touching the first.
        first = self.stored()["example-ingest"]
        generate_credential(self.platform, "example-query", [], root=self.root, runner=fake_sops)
        self.assertEqual(self.stored()["example-ingest"], first)
        self.assertNotEqual(self.stored()["example-query"], first)

    def test_existing_value_is_never_replaced(self) -> None:
        generate_credential(self.platform, "example-ingest", RECIPIENTS, root=self.root, runner=fake_sops)
        before = self.stored()
        with self.assertRaisesRegex(ConfigurationError, "already holds a value"):
            generate_credential(self.platform, "example-ingest", RECIPIENTS, root=self.root, runner=fake_sops)
        self.assertEqual(self.stored(), before)

    def test_undeclared_credential_writes_nothing(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown credential"):
            generate_credential(self.platform, "nobody", RECIPIENTS, root=self.root, runner=fake_sops)
        self.assertFalse((self.root / "secrets").exists())

    def test_new_file_requires_recipients(self) -> None:
        with self.assertRaises(ConfigurationError):
            generate_credential(self.platform, "example-ingest", [], root=self.root, runner=fake_sops)

    def test_value_is_sent_on_stdin_and_never_printed(self) -> None:
        generate_credential(self.platform, "example-ingest", RECIPIENTS, root=self.root, runner=fake_sops)
        calls = []

        def recording(args, **kwargs):
            calls.append((args, kwargs))
            return fake_sops(args, **kwargs)

        config = write_document(self.root, self.data)
        output = io.StringIO()
        through_fake = functools.partial(generate_credential, runner=recording)
        with mock.patch("nighthawk.__main__.ensure_doctor_ok"), \
                mock.patch("nighthawk.__main__.trust.generate_credential", through_fake), redirect_stdout(output):
            self.assertEqual(main([
                "generate-credential", "--config", str(config), "--credential", "example-query",
                "--root", str(self.root),
            ]), 0)
        secret = self.stored()["example-query"]
        (args, kwargs), = calls
        self.assertNotIn(secret, " ".join(args))
        self.assertIn(secret, kwargs["input"])
        self.assertNotIn(secret, output.getvalue())

    def test_failing_doctor_check_blocks_generation(self) -> None:
        config = write_document(self.root, self.data)
        with mock.patch("nighthawk.__main__.ensure_doctor_ok", side_effect=ConfigurationError("boom")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main([
                "generate-credential", "--config", str(config), "--credential", "example-ingest",
                "--recipient", "age1recipient", "--root", str(self.root),
            ]), 1)
        self.assertFalse((self.root / "secrets").exists())


class CertificateAuthorityTests(TrustFixture):
    def test_development_ca_is_stored_encrypted_at_both_references(self) -> None:
        fingerprint = init_ca(self.platform, RECIPIENTS, 30, root=self.root, runner=fake_sops)
        stored = self.stored()
        certificate = x509.load_pem_x509_certificate(stored["gateway-client-ca"].encode("ascii"))
        self.assertTrue(certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertEqual(certificate_fingerprint(certificate), fingerprint)
        self.assertIn("PRIVATE KEY", stored["gateway-client-ca-key"])
        self.assertEqual([path.name for path in self.root.rglob("*.pem")], [])

    def test_existing_ca_is_not_replaced(self) -> None:
        init_ca(self.platform, RECIPIENTS, 30, root=self.root, runner=fake_sops)
        with self.assertRaisesRegex(ConfigurationError, "refusing to replace a CA"):
            init_ca(self.platform, RECIPIENTS, 30, root=self.root, runner=fake_sops)

    def test_production_refuses_a_local_ca_without_confirmation(self) -> None:
        self.data["profile"] = "production"
        self.load()
        with self.assertRaisesRegex(ConfigurationError, "operator-supplied client CA"):
            init_ca(self.platform, RECIPIENTS, 30, root=self.root, runner=fake_sops)
        self.assertFalse((self.root / "secrets").exists())
        init_ca(self.platform, RECIPIENTS, 30, root=self.root, confirm_local_ca=True, runner=fake_sops)
        self.assertTrue(secret_key_exists(self.root / "secrets" / "local.sops.yaml", "gateway-client-ca-key"))

    def test_document_without_a_ca_key_reference_fails(self) -> None:
        del self.data["gateway"]["client_ca_key_secret_ref"]
        self.load()
        with self.assertRaisesRegex(ConfigurationError, "client_ca_key_secret_ref is required"):
            init_ca(self.platform, RECIPIENTS, 30, root=self.root, runner=fake_sops)


class IssueCertificateTests(TrustFixture):
    def setUp(self) -> None:
        super().setUp()
        init_ca(self.platform, RECIPIENTS, 30, root=self.root, runner=fake_sops)
        self.output = self.root / "issued"

    def test_collector_certificate_carries_only_the_declared_identity(self) -> None:
        issued = issue_certificate(
            self.platform, self.output, 7, credential_id="example-ingest", root=self.root, runner=fake_sops,
        )
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
        self.assertEqual(
            issued.fingerprint,
            certificate_fingerprint(certificate_from_header(forwarded_certificate(issued.certificate_path))),
        )

    def test_server_certificate_is_for_the_gateway_hostname_only(self) -> None:
        issued = issue_certificate(self.platform, self.output, 7, server=True, root=self.root, runner=fake_sops)
        certificate = x509.load_pem_x509_certificate(issued.certificate_path.read_bytes())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(names.get_values_for_type(x509.DNSName), ["gateway.nighthawk.internal"])
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        self.assertEqual(list(usage), [ExtendedKeyUsageOID.SERVER_AUTH])

    def test_undeclared_identity_missing_validity_and_overwrite_fail(self) -> None:
        for credential in ("example-query", "nobody"):
            with self.assertRaisesRegex(ConfigurationError, "declares a certificate identity"):
                issue_certificate(self.platform, self.output, 7, credential_id=credential, root=self.root, runner=fake_sops)
        with self.assertRaisesRegex(ConfigurationError, "no default"):
            issue_certificate(self.platform, self.output, None, credential_id="example-ingest", root=self.root, runner=fake_sops)
        with self.assertRaisesRegex(ConfigurationError, "outlives the client CA"):
            issue_certificate(self.platform, self.output, 365, credential_id="example-ingest", root=self.root, runner=fake_sops)
        self.assertFalse(self.output.exists())
        issue_certificate(self.platform, self.output, 7, credential_id="example-ingest", root=self.root, runner=fake_sops)
        with self.assertRaisesRegex(ConfigurationError, "refusing to overwrite"):
            issue_certificate(self.platform, self.output, 7, credential_id="example-ingest", root=self.root, runner=fake_sops)


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
        self.data["secrets"]["example-batch-ingest-2"] = {"file": "secrets/local.sops.yaml", "key": "example-batch-ingest-2"}
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
        init_ca(self.platform, RECIPIENTS, 30, root=self.root, runner=fake_sops)
        first = issue_certificate(self.platform, self.root / "first", 7, credential_id="example-ingest", root=self.root, runner=fake_sops)
        renewed = issue_certificate(self.platform, self.root / "renewed", 7, credential_id="example-ingest", root=self.root, runner=fake_sops)
        policy = self.policy()
        # A renewed certificate with the same identity needs no document change.
        for issued in (first, renewed):
            decision = decide(policy, self.facts("example-ingest", forwarded_certificate(issued.certificate_path)))
            self.assertEqual(decision.backend_id, "example-application")
        self.data["gateway"]["revoked_certificate_fingerprints"] = [first.fingerprint]
        policy = self.policy()
        self.assertEqual(decide(policy, self.facts("example-ingest", forwarded_certificate(first.certificate_path))).status, 403)
        self.assertEqual(decide(policy, self.facts("example-ingest", forwarded_certificate(renewed.certificate_path))).status, 200)


if __name__ == "__main__":
    unittest.main()
