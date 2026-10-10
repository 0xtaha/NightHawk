from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from nighthawk.authz import AuthService, RequestFacts, decide, load_policy, make_server, parse_policy
from nighthawk.config import ConfigurationError, load_platform
from nighthawk.gateway import policy_bundle, write_policy_bundle
from nighthawk.secrets import materialized_path
from nighthawk.trust import issue_certificate
from nighthawk.vault import pki_roles
from tests.fakes import (
    FakeVault, add_stream, basic, fake_client, forwarded_certificate, four_stream_document, materialize_plain,
    write_document,
)


class PolicyFixture(unittest.TestCase):
    """Two customers with two datastreams each; `remote` has a certificate-bound collector."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = four_stream_document()
        # The example's collector is certificate-bound; these tests use it as a plain local credential.
        del next(item for item in self.data["credentials"] if item["id"] == "example-ingest")["certificate_identity"]
        self.data["gateway"]["entry_points"] = ["local-gateway", "remote-gateway"]
        self.data["gateway"]["grafana_entry_point"] = "remote-gateway"
        add_stream(self.data, "second", "remote", "second-remote", certificate=True)
        del self.data["tenants"][1]["datastreams"][-1]["signals"]["profiles"]
        self.build()

    def build(self) -> None:
        self.platform = load_platform(write_document(self.root, self.data))
        self.bundle = policy_bundle(self.platform, materialize_plain(self.platform, self.root / "materialized"))
        self.policy = parse_policy(self.bundle)

    def facts(self, credential: str, signal: str = "metrics", permission: str = "ingest", **overrides) -> RequestFacts:
        values = {
            "entry": "port-8443", "signal": signal, "permission": permission,
            "authorization": (basic(credential),),
        }
        values.update(overrides)
        return RequestFacts(**values)

    def certificate(self, credential: str, name: str) -> str:
        if not hasattr(self, "vault"):
            self.vault = FakeVault(self.platform)
        # As an operator re-applies the rendered roles after declaring another collector identity.
        self.vault.roles = pki_roles(self.platform)
        issued = issue_certificate(
            self.platform, self.root / name, 7, fake_client(self.platform, self.vault), credential_id=credential,
        )
        return forwarded_certificate(issued.certificate_path)


class DecisionTests(PolicyFixture):
    def test_absent_tenant_header_is_bound_to_the_credential(self) -> None:
        decision = decide(self.policy, self.facts("example-ingest"))
        self.assertEqual((decision.status, decision.backend_id), (200, "example-application"))

    def test_matching_tenant_header_is_accepted(self) -> None:
        decision = decide(self.policy, self.facts("example-ingest", tenant_headers=("example-application",)))
        self.assertEqual((decision.status, decision.backend_id), (200, "example-application"))

    def test_spoofed_conflicting_and_repeated_tenant_headers_are_rejected(self) -> None:
        for headers in (
            ("second-application",),
            ("example-application|second-application",),
            ("example-application", "example-application"),
            ("example-application, second-application",),
            ("",),
        ):
            with self.subTest(headers=headers):
                decision = decide(self.policy, self.facts("example-ingest", tenant_headers=headers))
                self.assertEqual((decision.status, decision.backend_id), (403, None))

    def test_missing_malformed_unknown_and_wrong_credentials_fail_identically(self) -> None:
        unknown = decide(self.policy, self.facts("example-ingest", authorization=(basic("nobody", "x" * 40),)))
        wrong = decide(self.policy, self.facts("example-ingest", authorization=(basic("example-ingest", "x" * 40),)))
        self.assertEqual(unknown, wrong)
        self.assertEqual(unknown.status, 401)
        for authorization in ((), ("Bearer token",), ("Basic !!!",), ("Basic bm9jb2xvbg==",), (basic("example-ingest"),) * 2):
            with self.subTest(authorization=authorization):
                self.assertEqual(decide(self.policy, self.facts("example-ingest", authorization=authorization)).status, 401)

    def test_permissions_are_separate(self) -> None:
        self.assertEqual(decide(self.policy, self.facts("example-ingest", permission="query")).status, 403)
        self.assertEqual(decide(self.policy, self.facts("example-query", permission="ingest")).status, 403)
        self.assertEqual(decide(self.policy, self.facts("example-query", permission="query")).status, 200)

    def test_disabled_signal_is_rejected(self) -> None:
        certificate = self.certificate("second-remote-ingest", "issued")
        facts = self.facts("second-remote-ingest", signal="profiles", client_certificates=(certificate,))
        self.assertEqual(decide(self.policy, facts).status, 403)

    def test_unknown_route_class_is_rejected(self) -> None:
        for override in ({"entry": "port-1"}, {"signal": "events"}, {"permission": "admin"}):
            with self.subTest(override=override):
                self.assertEqual(decide(self.policy, self.facts("example-ingest", **override)).status, 403)

    def test_certificate_binding(self) -> None:
        matching = self.certificate("second-remote-ingest", "matching")
        allowed = decide(self.policy, self.facts(
            "second-remote-ingest", entry="port-443", client_certificates=(matching,),
        ))
        self.assertEqual((allowed.status, allowed.backend_id), (200, "second-remote"))
        # A certificate issued for another datastream's identity.
        add_stream(self.data, "second", "other", "second-other", certificate=True)
        self.build()
        other = self.certificate("second-other-ingest", "other")
        for certificates in ((other,), (), (matching, matching), ("not-base64!",), ("AAAA",)):
            with self.subTest(certificates=certificates):
                decision = decide(self.policy, self.facts("second-remote-ingest", client_certificates=certificates))
                self.assertEqual(decision.status, 403)

    def test_revoked_certificate_is_rejected_while_a_sibling_is_accepted(self) -> None:
        first = self.certificate("second-remote-ingest", "first")
        second = self.certificate("second-remote-ingest", "second")
        from nighthawk.authz import certificate_fingerprint, certificate_from_header
        self.data["gateway"]["revoked_certificate_fingerprints"] = [
            certificate_fingerprint(certificate_from_header(first)),
        ]
        self.build()
        self.assertEqual(decide(self.policy, self.facts("second-remote-ingest", client_certificates=(first,))).status, 403)
        self.assertEqual(decide(self.policy, self.facts("second-remote-ingest", client_certificates=(second,))).status, 200)

    def test_external_ingestion_requires_a_certificate_bound_credential(self) -> None:
        self.assertEqual(decide(self.policy, self.facts("example-ingest", entry="port-443")).status, 403)
        self.assertEqual(decide(self.policy, self.facts("example-ingest", entry="port-8443")).status, 200)
        query = self.facts("example-query", entry="port-443", permission="query")
        self.assertEqual(decide(self.policy, query).status, 200)

    def test_no_credential_obtains_another_pairs_backend_id(self) -> None:
        backend_ids = {stream.backend_id for stream in self.platform.streams}
        certificate = self.certificate("second-remote-ingest", "sweep")
        for credential in self.platform.credentials:
            for claimed in backend_ids:
                with self.subTest(credential=credential.id, claimed=claimed):
                    decision = decide(self.policy, self.facts(
                        credential.id, permission=credential.permission, tenant_headers=(claimed,),
                        client_certificates=(certificate,),
                    ))
                    if claimed == credential.backend_id:
                        self.assertEqual((decision.status, decision.backend_id), (200, claimed))
                    else:
                        self.assertEqual((decision.status, decision.backend_id), (403, None))


class PolicyBundleTests(PolicyFixture):
    def test_bundle_holds_digests_only_and_is_owner_only(self) -> None:
        output = self.root / "policy.json"
        write_policy_bundle(self.bundle, output)
        text = output.read_text(encoding="utf-8")
        for credential in self.platform.credentials:
            self.assertNotIn("s" * 40, text)
            self.assertIn(credential.id, text)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_policy(output), self.policy)

    def test_short_or_missing_secret_names_the_credential_without_printing_it(self) -> None:
        directory = materialize_plain(self.platform, self.root / "weak", {"example-query": "short-secret"})
        with self.assertRaises(ConfigurationError) as raised:
            policy_bundle(self.platform, directory)
        self.assertIn("example-query", str(raised.exception))
        self.assertNotIn("short-secret", str(raised.exception))
        reference = self.platform.secrets["example-ingest"]
        materialized_path(directory, reference).unlink()
        materialized_path(directory, self.platform.secrets["example-query"]).write_text("q" * 40, encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "example-ingest"):
            policy_bundle(self.platform, directory)

    def test_removed_credential_leaves_the_bundle(self) -> None:
        self.data["secrets"]["example-ingest-next"] = {"path": "nighthawk/local", "key": "example-ingest-next"}
        self.data["credentials"].append({
            "id": "example-ingest-next", "secret_ref": "example-ingest-next",
            "tenant": "example", "datastream": "application", "permission": "ingest",
        })
        self.build()
        for credential in ("example-ingest", "example-ingest-next"):
            self.assertEqual(decide(self.policy, self.facts(credential)).backend_id, "example-application")
        self.data["credentials"] = [item for item in self.data["credentials"] if item["id"] != "example-ingest"]
        self.build()
        self.assertEqual(decide(self.policy, self.facts("example-ingest")).status, 401)
        self.assertEqual(decide(self.policy, self.facts("example-ingest-next")).status, 200)

    def test_malformed_bundles_are_rejected(self) -> None:
        for mutate in (
            lambda bundle: bundle.update(schema_version=2),
            lambda bundle: bundle.update(extra=True),
            lambda bundle: bundle.update(credentials={}),
            lambda bundle: bundle["credentials"]["example-ingest"].update(secret_sha256="plain"),
            lambda bundle: bundle["credentials"]["example-ingest"].update(permission="admin"),
            lambda bundle: bundle["credentials"]["example-query"].update(certificate_identity="spiffe://x"),
            lambda bundle: bundle["entry_points"].update({"port-1": "public"}),
        ):
            bundle = json.loads(json.dumps(self.bundle))
            mutate(bundle)
            with self.assertRaises(ConfigurationError):
                parse_policy(bundle)


class ServiceTests(PolicyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.policy_path = self.root / "policy.json"
        write_policy_bundle(self.bundle, self.policy_path)
        self.service = AuthService(self.policy_path)
        self.server = make_server(self.service, "127.0.0.1", 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path: str, headers: list[tuple[str, str]] | None = None) -> tuple[int, dict[str, str], str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("GET", path)
        for name, value in headers or []:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode("utf-8")

    VERIFY = "/verify?entry=port-8443&signal=metrics&permission=ingest"

    def test_allowed_request_returns_the_bound_tenant(self) -> None:
        status, headers, _ = self.get(self.VERIFY, [("Authorization", basic("example-ingest"))])
        self.assertEqual((status, headers["X-Scope-OrgID"]), (200, "example-application"))

    def test_denied_requests_carry_no_tenant_header(self) -> None:
        status, headers, _ = self.get(self.VERIFY)
        self.assertEqual(status, 401)
        self.assertNotIn("X-Scope-OrgID", headers)
        status, headers, _ = self.get(self.VERIFY, [
            ("Authorization", basic("example-ingest")),
            ("X-Scope-OrgID", "example-application"), ("X-Scope-OrgID", "second-application"),
        ])
        self.assertEqual(status, 403)
        self.assertNotIn("X-Scope-OrgID", headers)
        status, _, _ = self.get("/verify?signal=metrics&permission=ingest", [("Authorization", basic("example-ingest"))])
        self.assertEqual(status, 403)

    def test_missing_or_invalid_policy_prevents_start(self) -> None:
        with self.assertRaises(ConfigurationError):
            AuthService(self.root / "absent.json")
        broken = self.root / "broken.json"
        broken.write_text("{}", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            AuthService(broken)

    def test_bad_reload_keeps_the_old_policy_and_reports_it(self) -> None:
        self.assertEqual(json.loads(self.get("/healthz")[2])["status"], "ok")
        self.policy_path.write_text("not json", encoding="utf-8")
        self.assertFalse(self.service.reload())
        status, _, body = self.get("/healthz")
        self.assertEqual((status, json.loads(body)["policy_reload_failed"]), (200, True))
        self.assertIn("nighthawk_authz_policy_reload_failed 1", self.get("/metrics")[2])
        self.assertEqual(self.get(self.VERIFY, [("Authorization", basic("example-ingest"))])[0], 200)

    def test_reload_applies_a_removed_credential(self) -> None:
        del self.bundle["credentials"]["example-ingest"]
        write_policy_bundle(self.bundle, self.policy_path)
        self.assertTrue(self.service.reload())
        self.assertEqual(self.get(self.VERIFY, [("Authorization", basic("example-ingest"))])[0], 401)
        self.assertEqual(json.loads(self.get("/healthz")[2])["status"], "ok")


if __name__ == "__main__":
    unittest.main()
