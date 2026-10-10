from __future__ import annotations

import io
import json
import os
import secrets as token_source
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from nighthawk import secrets, trust, vault
from nighthawk.__main__ import main
from nighthawk.config import ConfigurationError, load_platform, load_versions
from tests.fakes import ROOT_TOKEN, TOKEN, FakeVault, add_stream, example_document, fake_client, write_document

INTEGRATION_ADDRESS = os.environ.get("NIGHTHAWK_VAULT_TEST_ADDR")
INTEGRATION_REASON = (
    "needs a disposable dev-mode Vault: set NIGHTHAWK_VAULT_TEST_ADDR to its address and VAULT_TOKEN to its root token"
)


class VaultFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = example_document()
        self.load()

    def load(self) -> None:
        self.platform = load_platform(write_document(self.root, self.data))
        self.fake = FakeVault(self.platform)
        self.client = fake_client(self.platform, self.fake)

    def credential_file(self, name: str, value: str, mode: int = 0o600) -> Path:
        path = self.root / name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(mode)
        return path


class ClientTests(VaultFixture):
    def test_token_travels_only_in_its_header(self) -> None:
        self.client.kv_write("nighthawk/local", {"a": "1"}, 0)
        self.assertEqual(self.client.kv_read("nighthawk/local"), ({"a": "1"}, 1))
        for headers in self.fake.headers:
            self.assertEqual(headers["X-Vault-Token"], TOKEN)
        self.assertNotIn(TOKEN, self.fake.sent())
        self.assertNotIn(TOKEN, repr(self.client))

    def test_health_and_authority_certificate_need_no_credential(self) -> None:
        anonymous = vault.Client(self.platform.vault, None, self.fake)
        self.assertEqual(anonymous.health()["version"], "2.1.2")
        self.assertIn("BEGIN CERTIFICATE", anonymous.pki_ca_pem())
        for headers in self.fake.headers:
            self.assertNotIn("X-Vault-Token", headers)
        with self.assertRaisesRegex(vault.VaultError, "no Vault credential"):
            anonymous.kv_read("nighthawk/local")

    def test_namespace_is_sent_when_declared(self) -> None:
        self.data["vault"]["namespace"] = "team/observability"
        self.load()
        self.client.lookup_self()
        self.assertEqual(self.fake.headers[-1]["X-Vault-Namespace"], "team/observability")

    def test_new_path_reads_as_empty_at_version_zero(self) -> None:
        self.assertEqual(self.client.kv_read("nighthawk/local"), ({}, 0))

    def test_deleted_latest_version_still_counts_for_check_and_set(self) -> None:
        def transport(method, url, headers, body, timeout):
            return 404, json.dumps({"data": {"data": None, "metadata": {"version": 3, "deletion_time": "x"}}}).encode()

        self.assertEqual(vault.Client(self.platform.vault, TOKEN, transport).kv_read("nighthawk/local"), ({}, 3))

    def test_check_and_set_conflict_is_refused_not_overwritten(self) -> None:
        self.client.kv_write("nighthawk/local", {"a": "1"}, 0)
        with self.assertRaisesRegex(vault.VaultConflict, "nothing was overwritten, retry"):
            self.client.kv_write("nighthawk/local", {"a": "2"}, 0)
        self.assertEqual(self.fake.values("nighthawk/local"), {"a": "1"})

    def test_errors_name_the_path_and_operation_and_never_the_credential(self) -> None:
        self.fake.denied.add("nighthawk-kv/data/nighthawk/local")
        with self.assertRaises(vault.VaultError) as raised:
            self.client.kv_read("nighthawk/local")
        self.assertIn("denied read secret on nighthawk-kv/data/nighthawk/local", str(raised.exception))
        self.assertNotIn(TOKEN, str(raised.exception))
        self.fake.sealed = True
        with self.assertRaisesRegex(vault.VaultError, "sealed or unavailable"):
            self.client.lookup_self()
        self.fake.sealed, self.fake.reachable = False, False
        with self.assertRaisesRegex(vault.VaultError, "http://127.0.0.1:8200 is unreachable"):
            self.client.health()

    def test_absent_mount_and_role_read_as_none(self) -> None:
        self.assertIsNone(self.client.mount_type("absent"))
        self.assertIsNone(self.client.pki_role("absent"))
        self.assertEqual(self.client.mount_type("nighthawk-kv"), ("kv", {"version": "2"}))


class CredentialTests(VaultFixture):
    def resolve(self, auth: vault.Auth = vault.Auth(), environ: dict | None = None) -> str:
        return vault.resolve_token(self.platform.vault, auth, environ or {}, self.fake)

    def test_token_from_the_environment(self) -> None:
        self.assertEqual(self.resolve(environ={"VAULT_TOKEN": f" {TOKEN}\n"}), TOKEN)
        self.assertEqual(self.fake.calls, [])

    def test_token_from_an_owner_only_file(self) -> None:
        self.assertEqual(self.resolve(vault.Auth(token_file=self.credential_file("token", TOKEN))), TOKEN)

    def test_approle_login_from_files_yields_a_token_for_this_command(self) -> None:
        self.fake.approles[("role-1", "secret-1")] = TOKEN
        auth = vault.Auth(
            role_id_file=self.credential_file("role", "role-1"), secret_id_file=self.credential_file("secret", "secret-1"),
        )
        self.assertEqual(self.resolve(auth), TOKEN)
        (method, path, body), = self.fake.calls
        self.assertEqual((method, path), ("POST", "auth/approle/login"))
        self.assertEqual(body, {"role_id": "role-1", "secret_id": "secret-1"})
        # Nothing was written anywhere: the login token exists only in memory.
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["platform.yaml", "role", "secret"])

    def test_approle_needs_both_files(self) -> None:
        with self.assertRaisesRegex(vault.VaultError, "needs both"):
            self.resolve(vault.Auth(role_id_file=self.credential_file("role", "role-1")))

    def test_rejected_approle_login_does_not_echo_the_secret(self) -> None:
        auth = vault.Auth(
            role_id_file=self.credential_file("role", "role-1"),
            secret_id_file=self.credential_file("secret", "wrong-secret-value"),
        )
        with self.assertRaises(vault.VaultError) as raised:
            self.resolve(auth)
        self.assertIn("log in with AppRole", str(raised.exception))
        self.assertNotIn("wrong-secret-value", str(raised.exception))

    def test_no_credential_fails_before_contacting_vault_and_names_the_ways(self) -> None:
        with self.assertRaises(vault.VaultError) as raised:
            self.resolve()
        for way in ("VAULT_TOKEN", "--vault-token-file", "--vault-role-id-file", "--vault-secret-id-file"):
            self.assertIn(way, str(raised.exception))
        self.assertEqual(self.fake.calls, [])

    def test_credential_file_readable_by_others_is_refused(self) -> None:
        for mode in (0o640, 0o604, 0o644):
            with self.subTest(mode=oct(mode)):
                path = self.credential_file("token", TOKEN, mode)
                with self.assertRaisesRegex(vault.VaultError, f"{path}: credential file is readable by group or others"):
                    self.resolve(vault.Auth(token_file=path))
        self.assertEqual(self.fake.calls, [])

    def test_missing_or_empty_credential_file_is_named(self) -> None:
        with self.assertRaisesRegex(vault.VaultError, "cannot read credential file"):
            self.resolve(vault.Auth(token_file=self.root / "absent"))
        with self.assertRaisesRegex(vault.VaultError, "credential file is empty"):
            self.resolve(vault.Auth(token_file=self.credential_file("token", "")))

    def test_no_command_accepts_a_credential_as_an_argument(self) -> None:
        for command in ("doctor", "store-secret", "materialize-secrets", "quickstart-docker"):
            for option in ("--vault-token", "--token", "--vault-secret-id", "--vault-role-id"):
                with self.subTest(command=command, option=option), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        main([command, option, "value"])

    def test_production_refuses_a_root_credential(self) -> None:
        self.data["profile"] = "production"
        self.data["vault"]["address"] = "https://vault.example.com:8200"
        self.load()
        with self.assertRaisesRegex(vault.VaultError, "production refuses a Vault credential that carries the root policy"):
            vault.connect(self.platform, environ={"VAULT_TOKEN": ROOT_TOKEN}, transport=self.fake)
        self.assertEqual([path for _, path, _ in self.fake.calls], ["auth/token/lookup-self"])
        vault.connect(self.platform, environ={"VAULT_TOKEN": TOKEN}, transport=self.fake)

    def test_development_accepts_a_root_credential(self) -> None:
        vault.connect(self.platform, environ={"VAULT_TOKEN": ROOT_TOKEN}, transport=self.fake)


class AccessRequirementsTests(VaultFixture):
    def test_policy_covers_exactly_the_declared_paths_and_roles(self) -> None:
        self.data["secrets"]["example-query"] = {"path": "nighthawk/other", "key": "example-query"}
        self.load()
        self.assertEqual(vault.policy_paths(self.platform), {
            "nighthawk-kv/data/nighthawk/local": ["create", "read", "update"],
            "nighthawk-kv/data/nighthawk/other": ["create", "read", "update"],
            "nighthawk-pki/sign/nighthawk-server": ["create", "update"],
            "nighthawk-pki/sign/nighthawk-collector": ["create", "update"],
            "nighthawk-pki/roles/nighthawk-server": ["read"],
            "nighthawk-pki/roles/nighthawk-collector": ["read"],
            "nighthawk-pki/revoke": ["create", "update"],
            "auth/token/lookup-self": ["read"],
        })
        hcl = vault.policy_hcl(self.platform)
        self.assertEqual(hcl.count("\npath "), 8)
        for forbidden in ("*", "+", "sudo", "delete", "root/", "sys/"):
            self.assertNotIn(forbidden, hcl)

    def test_roles_are_limited_to_declared_hostnames_and_identities(self) -> None:
        add_stream(self.data, "second", "edge", "second-edge", certificate=True)
        self.load()
        roles = vault.pki_roles(self.platform)
        server, client = roles["nighthawk-server"], roles["nighthawk-collector"]
        self.assertEqual(server["allowed_domains"], ["gateway.nighthawk.internal", "grafana.nighthawk.internal", "seaweedfs"])
        self.assertEqual((server["server_flag"], server["client_flag"], server["allowed_uri_sans"]), (True, False, []))
        self.assertEqual(client["allowed_uri_sans"], [
            "spiffe://nighthawk/example/application/collector", "spiffe://nighthawk/second/edge/collector",
        ])
        self.assertEqual((client["server_flag"], client["client_flag"], client["allowed_domains"]), (False, True, []))
        for role in roles.values():
            for loose in ("allow_any_name", "allow_subdomains", "allow_glob_domains", "allow_ip_sans", "allow_localhost"):
                self.assertFalse(role[loose], loose)

    def test_cloud_storage_hostnames_are_not_in_the_server_role(self) -> None:
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
        self.assertEqual(
            vault.pki_roles(self.platform)["nighthawk-server"]["allowed_domains"],
            ["gateway.nighthawk.internal", "grafana.nighthawk.internal"],
        )

    def test_output_is_deterministic_and_rendered_without_contacting_vault(self) -> None:
        add_stream(self.data, "second", "edge", "second-edge", certificate=True)
        self.load()
        first = vault.access_requirements(self.platform)
        self.data["credentials"].reverse()
        self.data["secrets"] = dict(reversed(list(self.data["secrets"].items())))
        self.load()
        self.assertEqual(vault.access_requirements(self.platform), first)
        self.assertEqual(sorted(first), ["vault/pki-roles.json", "vault/policy.hcl"])
        self.assertEqual(self.fake.calls, [])


class BootstrapTests(VaultFixture):
    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeVault(self.platform, configured=False)
        self.client = fake_client(self.platform, self.fake, ROOT_TOKEN)

    def bootstrap(self, **overrides) -> list[str]:
        arguments = {"confirmed": True, "ca_valid_days": 365, **overrides}
        return vault.bootstrap_dev(self.platform, self.client, **arguments)

    def doctor(self) -> list[secrets.Check]:
        return secrets.doctor(load_versions(), self.platform, environ={"VAULT_TOKEN": ROOT_TOKEN}, transport=self.fake)[0]

    def test_empty_vault_fails_the_prerequisite_check_then_passes_after_bootstrap(self) -> None:
        self.assertFalse(all(check.ok for check in self.doctor()))
        self.assertEqual(self.bootstrap(), [
            "mount nighthawk-kv", "mount nighthawk-pki", "certificate authority",
            "PKI role nighthawk-server", "PKI role nighthawk-collector", "policy nighthawk",
        ])
        self.assertEqual([check.detail for check in self.doctor() if not check.ok], [])
        self.assertEqual(self.fake.policies["nighthawk"], vault.policy_hcl(self.platform))
        self.assertEqual(self.fake.roles, vault.pki_roles(self.platform))

    def test_second_run_changes_nothing_and_keeps_the_authority(self) -> None:
        self.bootstrap()
        authority = self.fake.ca_pem()
        self.fake.calls.clear()
        self.assertEqual(self.bootstrap(), [])
        self.assertEqual(self.fake.ca_pem(), authority)
        self.assertEqual([call for call in self.fake.calls if call[0] != "GET"], [])

    def test_changed_document_updates_roles_and_policy_only(self) -> None:
        self.bootstrap()
        authority = self.fake.ca_pem()
        add_stream(self.data, "second", "edge", "second-edge", certificate=True)
        self.platform = load_platform(write_document(self.root, self.data))
        self.assertEqual(self.bootstrap(), ["PKI role nighthawk-collector"])
        self.assertEqual(self.fake.ca_pem(), authority)

    def test_production_document_is_refused(self) -> None:
        self.data["profile"] = "production"
        self.data["vault"]["address"] = "https://vault.example.com:8200"
        self.platform = load_platform(write_document(self.root, self.data))
        with self.assertRaisesRegex(vault.VaultError, "refuses a production platform document"):
            self.bootstrap()
        self.assertEqual(self.fake.calls, [])

    def test_missing_confirmation_is_refused(self) -> None:
        with self.assertRaisesRegex(vault.VaultError, "confirm that this Vault is disposable"):
            self.bootstrap(confirmed=False)
        self.assertEqual(self.fake.calls, [])

    def test_existing_mount_of_another_type_is_not_replaced(self) -> None:
        self.fake.mounts["nighthawk-kv"] = {"type": "transit", "options": {}}
        with self.assertRaisesRegex(vault.VaultError, "nighthawk-kv already exists in Vault as a transit mount"):
            self.bootstrap()

    def test_credential_without_administrative_access_is_denied(self) -> None:
        self.client = fake_client(self.platform, self.fake, TOKEN)
        with self.assertRaisesRegex(vault.VaultError, "denied list mounts on sys/mounts"):
            self.bootstrap()

    def test_cli_requires_the_confirmation_and_an_explicit_validity(self) -> None:
        config = str(write_document(self.root, self.data))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["bootstrap-dev-vault", "--config", config, "--confirm-disposable-vault"])


@unittest.skipUnless(INTEGRATION_ADDRESS, INTEGRATION_REASON)
class VaultIntegrationTests(unittest.TestCase):
    """Against a real dev-mode Vault, in mounts of its own that are removed afterwards."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        suffix = token_source.token_hex(4)
        cls.data = example_document()
        add_stream(cls.data, "second", "edge", "second-edge", certificate=True)
        cls.data["vault"].update(address=INTEGRATION_ADDRESS, kv_mount=f"nhtest-{suffix}-kv")
        cls.data["vault"]["pki"]["mount"] = f"nhtest-{suffix}-pki"
        cls.platform = load_platform(write_document(cls.root, cls.data))
        cls.policy = f"nhtest-{suffix}"
        cls.admin = vault.connect(cls.platform)
        cls.addClassCleanup(cls.remove)
        cls.created = vault.bootstrap_dev(cls.platform, cls.admin, confirmed=True, ca_valid_days=30, policy_name=cls.policy)
        issued = cls.admin.call(
            "POST", "auth/token/create", {"policies": [cls.policy], "no_default_policy": True, "ttl": "10m"},
            operation="create a test token",
        )
        cls.limited = vault.Client(cls.platform.vault, issued["auth"]["client_token"])

    @classmethod
    def remove(cls) -> None:
        for mount in (cls.platform.vault.kv_mount, cls.platform.vault.pki_mount):
            cls.admin.call("DELETE", f"sys/mounts/{mount}", operation="remove a test mount")
        cls.admin.call("DELETE", f"sys/policies/acl/{cls.policy}", operation="remove a test policy")

    def test_bootstrap_made_the_prerequisite_check_pass_and_is_idempotent(self) -> None:
        self.assertEqual(len(self.created), 6)
        checks, client = secrets.doctor(load_versions(), self.platform)
        self.assertEqual([check.detail for check in checks if not check.ok], [])
        self.assertIsNotNone(client)
        authority = self.admin.pki_ca_pem()
        self.assertEqual(
            vault.bootstrap_dev(self.platform, self.admin, confirmed=True, ca_valid_days=30, policy_name=self.policy), [],
        )
        self.assertEqual(self.admin.pki_ca_pem(), authority)

    def test_token_limited_to_the_rendered_policy_passes_the_check_and_is_confined(self) -> None:
        self.assertEqual(self.limited.lookup_self()["policies"], [self.policy])
        self.assertEqual(self.limited.mount_type(self.platform.vault.kv_mount), ("kv", {"version": "2"}))
        self.assertIsNotNone(self.limited.pki_role(self.platform.vault.client_role))
        secrets.store_secret(self.limited, self.platform, "grafana-admin", "limited-token-value")
        self.assertEqual(self.limited.kv_read("nighthawk/local")[0]["grafana-admin"], "limited-token-value")
        with self.assertRaisesRegex(vault.VaultError, "denied read secret"):
            self.limited.kv_read("nighthawk/undeclared")
        with self.assertRaisesRegex(vault.VaultError, "denied"):
            self.limited.call("GET", "sys/mounts", operation="list mounts")

    def test_two_keys_share_a_path_and_rotation_changes_only_one(self) -> None:
        secrets.store_secret(self.admin, self.platform, "example-ingest", "first-value")
        secrets.store_secret(self.admin, self.platform, "example-query", "second-value")
        with self.assertRaisesRegex(ConfigurationError, "already holds a value"):
            secrets.store_secret(self.admin, self.platform, "example-ingest", "other")
        secrets.rotate_secret(self.admin, self.platform, "example-ingest", "rotated-value")
        values, _ = self.admin.kv_read("nighthawk/local")
        self.assertEqual((values["example-ingest"], values["example-query"]), ("rotated-value", "second-value"))

    def test_real_check_and_set_conflict_overwrites_nothing(self) -> None:
        path = "nighthawk/conflict"
        _, version = self.admin.kv_read(path)
        self.admin.kv_write(path, {"winner": "first"}, version)
        with self.assertRaises(vault.VaultConflict):
            self.admin.kv_write(path, {"winner": "second"}, version)
        self.assertEqual(self.admin.kv_read(path)[0], {"winner": "first"})

    def test_certificates_chain_to_the_authority_and_revocation_is_recorded(self) -> None:
        authority = x509.load_pem_x509_certificate(self.admin.pki_ca_pem().encode())
        issued = {}
        for name, what in (
            ("gateway", {"server": True}), ("storage", {"storage": True}),
            ("collector", {"credential_id": "second-edge-ingest"}),
        ):
            issued[name] = trust.issue_certificate(self.platform, self.root / f"issued-{name}", 7, self.limited, **what)
            certificate = x509.load_pem_x509_certificate(issued[name].certificate_path.read_bytes())
            certificate.verify_directly_issued_by(authority)
        gateway = x509.load_pem_x509_certificate(issued["gateway"].certificate_path.read_bytes())
        names = gateway.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(
            sorted(names.get_values_for_type(x509.DNSName)), ["gateway.nighthawk.internal", "grafana.nighthawk.internal"],
        )
        collector = x509.load_pem_x509_certificate(issued["collector"].certificate_path.read_bytes())
        fingerprint = trust.revoke_certificate(self.limited, issued["collector"].certificate_path)
        self.assertEqual(fingerprint, collector.fingerprint(hashes.SHA256()).hex())
        serial = f"{collector.serial_number:x}"
        serial = ":".join(("0" * (len(serial) % 2) + serial)[index:index + 2] for index in range(0, len(serial) + len(serial) % 2, 2))
        record = self.admin.call("GET", f"{self.platform.vault.pki_mount}/cert/{serial}", operation="read a certificate")
        self.assertGreater(record["data"]["revocation_time"], 0)

    def test_vault_itself_refuses_names_outside_the_roles(self) -> None:
        key = ec.generate_private_key(ec.SECP256R1())

        def request(common_name: str, name: x509.GeneralName) -> str:
            return (
                x509.CertificateSigningRequestBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
                .add_extension(x509.SubjectAlternativeName([name]), critical=False).sign(key, hashes.SHA256())
            ).public_bytes(serialization.Encoding.PEM).decode("ascii")

        cases = (
            (self.platform.vault.client_role, "collector", x509.UniformResourceIdentifier("spiffe://nighthawk/other/x/collector")),
            (self.platform.vault.client_role, "collector", x509.DNSName("gateway.nighthawk.internal")),
            (self.platform.vault.server_role, "evil.example.com", x509.DNSName("evil.example.com")),
            (self.platform.vault.server_role, "gateway.nighthawk.internal", x509.DNSName("sub.gateway.nighthawk.internal")),
            (self.platform.vault.server_role, "gateway.nighthawk.internal", x509.UniformResourceIdentifier("spiffe://nighthawk/second/edge/collector")),
        )
        for role, common_name, name in cases:
            with self.subTest(role=role, name=name.value), self.assertRaisesRegex(vault.VaultError, "HTTP 400"):
                self.limited.pki_sign(role, request(common_name, name), common_name, "24h")

    def test_approle_login_against_the_real_auth_method(self) -> None:
        methods = self.admin.call("GET", "sys/auth", operation="list auth methods")["data"]
        if f"{vault.APPROLE_MOUNT}/" not in methods:
            self.admin.call("POST", f"sys/auth/{vault.APPROLE_MOUNT}", {"type": "approle"}, operation="enable AppRole")
            self.addCleanup(self.admin.call, "DELETE", f"sys/auth/{vault.APPROLE_MOUNT}", operation="disable AppRole")
        role = f"auth/{vault.APPROLE_MOUNT}/role/{self.policy}"
        self.admin.call("POST", role, {"token_policies": [self.policy], "token_ttl": "5m"}, operation="write an AppRole")
        self.addCleanup(self.admin.call, "DELETE", role, operation="remove an AppRole")
        files = {}
        for name, value in (
            ("role-id", self.admin.call("GET", f"{role}/role-id", operation="read role ID")["data"]["role_id"]),
            ("secret-id", self.admin.call("POST", f"{role}/secret-id", operation="create secret ID")["data"]["secret_id"]),
        ):
            files[name] = self.root / name
            files[name].write_text(value, encoding="utf-8")
            files[name].chmod(0o600)
        client = vault.connect(
            self.platform, vault.Auth(role_id_file=files["role-id"], secret_id_file=files["secret-id"]), environ={},
        )
        self.assertIn(self.policy, client.lookup_self()["policies"])

    def test_cli_doctor_reports_the_real_server(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["doctor", "--config", str(self.root / "platform.yaml")])
        self.assertEqual(status, 0, output.getvalue())
        self.assertIn(f"at {INTEGRATION_ADDRESS} is reachable and unsealed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
