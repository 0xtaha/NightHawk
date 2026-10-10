from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from nighthawk.__main__ import main
from nighthawk.config import ROOT, ConfigurationError, load_platform, load_versions
from nighthawk.secrets import (
    cleanup, doctor, ensure_doctor_ok, materialize, materialized_path, read_secrets, rotate_secret,
    secret_exists, store_secret,
)
from nighthawk.vault import VaultConflict
from tests.fakes import ROOT_TOKEN, TOKEN, FakeVault, example_document, fake_client, run_cli, write_document

PATH = "nighthawk/local"


class SecretsFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = example_document()
        self.load()

    def load(self) -> None:
        self.config = write_document(self.root, self.data)
        self.platform = load_platform(self.config)
        self.fake = FakeVault(self.platform)
        self.client = fake_client(self.platform, self.fake)

    def fill(self) -> dict[str, str]:
        values = {name: f"value-of-{name}" for name in self.platform.secrets}
        for name, value in values.items():
            store_secret(self.client, self.platform, name, value)
        return values

    def writes(self) -> list:
        return [call for call in self.fake.calls if call[0] != "GET"]


class DoctorTests(SecretsFixture):
    def check(self, token: str | None = TOKEN):
        environ = {} if token is None else {"VAULT_TOKEN": token}
        return doctor(load_versions(), self.platform, environ=environ, transport=self.fake)

    def failures(self, token: str | None = TOKEN) -> list[str]:
        return [check.detail for check in self.check(token)[0] if not check.ok]

    def test_all_prerequisites_satisfied_names_the_version(self) -> None:
        checks, client = self.check()
        self.assertTrue(all(check.ok for check in checks))
        self.assertIn("Vault 2.1.2 at http://127.0.0.1:8200 is reachable and unsealed", checks[0].detail)
        self.assertIsNotNone(client)
        self.assertEqual(self.writes(), [])

    def test_unreachable_sealed_and_uninitialized_are_told_apart(self) -> None:
        self.fake.reachable = False
        self.assertEqual(len(self.failures()), 1)
        self.assertIn("is unreachable", self.failures()[0])
        self.fake.reachable, self.fake.sealed = True, True
        self.assertEqual(self.failures(), ["Vault at http://127.0.0.1:8200 is sealed"])
        self.fake.sealed, self.fake.initialized = False, False
        self.assertEqual(self.failures(), ["Vault at http://127.0.0.1:8200 is not initialized"])
        self.assertIsNone(self.check()[1])

    def test_unsupported_version_names_reported_and_supported(self) -> None:
        for version in ("2.1.1", "2.2.0", "1.15.6", ""):
            with self.subTest(version=version):
                self.fake.version = version
                (failure,) = self.failures()
                self.assertIn("outside the supported range 2.1.2 up to, not including, 2.2.0", failure)
                if version:
                    self.assertIn(f"Vault {version}", failure)

    def test_missing_or_rejected_credential_stops_the_remaining_checks(self) -> None:
        (failure,) = self.failures(token=None)
        self.assertIn("no Vault credential", failure)
        (failure,) = self.failures(token="not-a-known-token")
        self.assertIn("the credential was rejected or lacks access", failure)
        self.assertNotIn("not-a-known-token", failure)
        self.assertIsNone(self.check(token=None)[1])

    def test_each_missing_mount_and_role_is_named(self) -> None:
        del self.fake.mounts["nighthawk-kv"]
        del self.fake.roles["nighthawk-server"]
        del self.fake.roles["nighthawk-collector"]
        self.assertEqual(self.failures(), [
            "key-value mount nighthawk-kv does not exist or is not readable",
            "PKI role nighthawk-server does not exist or is not readable",
            "PKI role nighthawk-collector does not exist or is not readable",
        ])

    def test_wrong_mount_types_and_a_missing_authority_are_named(self) -> None:
        self.fake.mounts["nighthawk-kv"] = {"type": "kv", "options": {"version": "1"}}
        self.fake.ca_certificate = None
        failures = self.failures()
        self.assertIn("nighthawk-kv is not a key-value version 2 mount", failures)
        self.assertIn("PKI mount nighthawk-pki has no certificate authority", failures)
        self.fake.mounts["nighthawk-pki"] = {"type": "transit", "options": {}}
        self.assertIn("nighthawk-pki is not a PKI mount", self.failures())

    def test_ensure_doctor_ok_returns_a_client_or_raises_every_failure(self) -> None:
        client = ensure_doctor_ok(load_versions(), self.platform, environ={"VAULT_TOKEN": TOKEN}, transport=self.fake)
        self.assertEqual(client.kv_read(PATH), ({}, 0))
        self.fake.version = "1.0.0"
        del self.fake.mounts["nighthawk-kv"]
        with self.assertRaises(ConfigurationError) as raised:
            ensure_doctor_ok(load_versions(), self.platform, environ={"VAULT_TOKEN": TOKEN}, transport=self.fake)
        self.assertIn("outside the supported range", str(raised.exception))
        self.assertIn("nighthawk-kv does not exist", str(raised.exception))


class StoreSecretTests(SecretsFixture):
    def test_first_key_creates_the_path(self) -> None:
        reference = store_secret(self.client, self.platform, "example-ingest", "first-value")
        self.assertEqual((reference.path, reference.key), (PATH, "example-ingest"))
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "first-value"})
        self.assertTrue(secret_exists(self.client, reference))

    def test_further_key_keeps_every_key_already_at_the_path(self) -> None:
        store_secret(self.client, self.platform, "example-ingest", "first-value")
        store_secret(self.client, self.platform, "example-query", "second-value")
        store_secret(self.client, self.platform, "grafana-admin", "third-value")
        self.assertEqual(self.fake.values(PATH), {
            "example-ingest": "first-value", "example-query": "second-value", "grafana-admin": "third-value",
        })
        self.assertEqual(len(self.fake.kv[PATH]), 3)

    def test_existing_key_is_refused_and_names_rotation(self) -> None:
        store_secret(self.client, self.platform, "example-ingest", "first-value")
        self.fake.calls.clear()
        with self.assertRaisesRegex(ConfigurationError, "already holds a value; use rotate-secret"):
            store_secret(self.client, self.platform, "example-ingest", "replacement")
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "first-value"})
        self.assertEqual(self.writes(), [])

    def test_undeclared_reference_writes_nothing(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "'mystery' is not a secret reference"):
            store_secret(self.client, self.platform, "mystery", "value")
        self.assertEqual(self.fake.calls, [])

    def test_empty_value_is_refused(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "non-empty"):
            store_secret(self.client, self.platform, "example-ingest", "")
        self.assertEqual(self.fake.calls, [])

    def test_concurrent_change_is_rejected_rather_than_overwritten(self) -> None:
        store_secret(self.client, self.platform, "example-ingest", "first-value")
        self.fake.conflict_next_write = True
        with self.assertRaisesRegex(VaultConflict, "nothing was overwritten, retry"):
            store_secret(self.client, self.platform, "example-query", "second-value")
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "first-value"})

    def test_value_travels_only_in_the_request_body(self) -> None:
        store_secret(self.client, self.platform, "example-ingest", "a-very-secret-value")
        self.assertNotIn("a-very-secret-value", self.fake.sent())
        self.assertNotIn("a-very-secret-value", str(self.fake.headers))


class RotateSecretTests(SecretsFixture):
    def test_rotation_adds_a_version_and_leaves_other_keys_alone(self) -> None:
        store_secret(self.client, self.platform, "example-ingest", "first-value")
        store_secret(self.client, self.platform, "example-query", "second-value")
        reference = rotate_secret(self.client, self.platform, "example-ingest", "rotated-value")
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "rotated-value", "example-query": "second-value"})
        self.assertEqual(len(self.fake.kv[PATH]), 3)
        # The earlier version is still in Vault's history, and the reference is unchanged.
        self.assertEqual(self.fake.kv[PATH][1]["example-ingest"], "first-value")
        self.assertEqual(reference, self.platform.secrets["example-ingest"])

    def test_nonexistent_path_or_key_is_refused_instead_of_created(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "cannot rotate a secret that does not exist"):
            rotate_secret(self.client, self.platform, "example-ingest", "value")
        store_secret(self.client, self.platform, "example-query", "second-value")
        with self.assertRaisesRegex(ConfigurationError, "cannot rotate a secret that does not exist"):
            rotate_secret(self.client, self.platform, "example-ingest", "value")
        self.assertEqual(self.fake.values(PATH), {"example-query": "second-value"})

    def test_concurrent_change_is_rejected(self) -> None:
        store_secret(self.client, self.platform, "example-ingest", "first-value")
        self.fake.conflict_next_write = True
        with self.assertRaises(VaultConflict):
            rotate_secret(self.client, self.platform, "example-ingest", "rotated-value")
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "first-value"})


class MaterializeTests(SecretsFixture):
    def setUp(self) -> None:
        super().setUp()
        self.output = self.root / "materialized"

    def test_materializes_every_reference_owner_only(self) -> None:
        values = self.fill()
        materialize(self.platform, self.client, self.output)
        for name, value in values.items():
            path = materialized_path(self.output, self.platform.secrets[name])
            self.assertEqual(path, self.output / "kv" / "nighthawk" / "local" / name)
            self.assertEqual(path.read_text(encoding="utf-8"), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for directory in (self.output, self.output / "kv", self.output / "kv" / "nighthawk", self.output / "kv" / "nighthawk" / "local"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)

    def test_default_directory_is_ignored_by_version_control(self) -> None:
        self.assertIn(".materialized-secrets/", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_absent_secrets_are_all_named_and_nothing_partial_is_left(self) -> None:
        store_secret(self.client, self.platform, "example-ingest", "first-value")
        with self.assertRaises(ConfigurationError) as raised:
            materialize(self.platform, self.client, self.output)
        message = str(raised.exception)
        for name in self.platform.secrets:
            (self.assertNotIn if name == "example-ingest" else self.assertIn)(f"{name} (", message)
        self.assertNotIn("first-value", message)
        self.assertFalse(self.output.exists())

    def test_existing_directory_with_other_permissions_is_refused_before_reading(self) -> None:
        self.fill()
        self.output.mkdir(mode=0o755)
        self.output.chmod(0o755)
        self.fake.calls.clear()
        with self.assertRaisesRegex(ConfigurationError, "unexpected permissions 0o755"):
            materialize(self.platform, self.client, self.output)
        self.assertEqual(self.fake.calls, [])

    def test_second_run_rewrites_nothing_and_removes_what_is_no_longer_referenced(self) -> None:
        self.fill()
        materialize(self.platform, self.client, self.output)
        (self.output / "runtime").mkdir()
        (self.output / "runtime" / "kept").write_text("rendered", encoding="utf-8")
        kept = materialized_path(self.output, self.platform.secrets["example-ingest"])
        before = kept.stat().st_mtime_ns
        del self.data["secrets"]["grafana-admin"]
        self.data["secrets"]["grafana-admin"] = {"path": "nighthawk/other", "key": "grafana-admin"}
        self.platform = load_platform(write_document(self.root, self.data))
        store_secret(self.client, self.platform, "grafana-admin", "moved-value")
        materialize(self.platform, self.client, self.output)
        self.assertEqual(kept.stat().st_mtime_ns, before)
        self.assertFalse((self.output / "kv" / "nighthawk" / "local" / "grafana-admin").exists())
        self.assertEqual((self.output / "kv" / "nighthawk" / "other" / "grafana-admin").read_text(), "moved-value")
        self.assertTrue((self.output / "runtime" / "kept").exists())

    def test_rotated_value_is_materialized(self) -> None:
        self.fill()
        materialize(self.platform, self.client, self.output)
        rotate_secret(self.client, self.platform, "example-query", "rotated-value")
        materialize(self.platform, self.client, self.output)
        self.assertEqual(materialized_path(self.output, self.platform.secrets["example-query"]).read_text(), "rotated-value")

    def test_read_secrets_reads_each_path_once(self) -> None:
        self.fill()
        self.fake.calls.clear()
        self.assertEqual(len(read_secrets(self.client, self.platform)), len(self.platform.secrets))
        self.assertEqual(len(self.fake.calls), 1)

    def test_cleanup_removes_all_materialized_material(self) -> None:
        self.fill()
        materialize(self.platform, self.client, self.output)
        cleanup(self.output)
        self.assertFalse(self.output.exists())
        cleanup(self.output)  # already clean


class CommandLineTests(SecretsFixture):
    def cli(self, *argv: str, **kwargs) -> tuple[int, str, str]:
        return run_cli(self.fake, [argv[0], "--config", str(self.config), *argv[1:]], **kwargs)

    def test_store_reads_the_value_from_standard_input_and_never_prints_it(self) -> None:
        status, output, errors = self.cli("store-secret", "--secret", "example-ingest", stdin="typed-secret-value\n")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "typed-secret-value"})
        self.assertIn("Stored example-ingest in Vault at nighthawk/local key example-ingest", output)
        self.assertNotIn("typed-secret-value", output)

    def test_store_and_rotate_read_the_value_from_a_file(self) -> None:
        value_file = self.root / "value"
        value_file.write_text("file-secret-value\n", encoding="utf-8")
        self.assertEqual(self.cli("store-secret", "--secret", "example-ingest", "--value-file", str(value_file))[0], 0)
        value_file.write_text("rotated-secret-value\n", encoding="utf-8")
        status, output, _ = self.cli("rotate-secret", "--secret", "example-ingest", "--value-file", str(value_file))
        self.assertEqual(status, 0)
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "rotated-secret-value"})
        self.assertNotIn("rotated-secret-value", output)

    def test_a_value_cannot_be_given_as_an_argument(self) -> None:
        for command in ("store-secret", "rotate-secret"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                self.cli(command, "--secret", "example-ingest", "--value", "argument-value")
        self.assertEqual(self.fake.kv, {})

    def test_existing_key_fails_explicitly_through_the_command_line(self) -> None:
        self.cli("store-secret", "--secret", "example-ingest", stdin="first-value")
        status, _, errors = self.cli("store-secret", "--secret", "example-ingest", stdin="replacement-value")
        self.assertEqual(status, 1)
        self.assertIn("use rotate-secret", errors)
        self.assertNotIn("replacement-value", errors)
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "first-value"})

    def test_failing_prerequisite_check_blocks_every_read_and_write(self) -> None:
        self.fill()
        self.fake.version = "1.0.0"
        self.fake.calls.clear()
        for argv in (
            ("store-secret", "--secret", "example-ingest"), ("rotate-secret", "--secret", "example-ingest"),
            ("materialize-secrets", "--output-dir", str(self.root / "out")),
            ("generate-credential", "--credential", "example-ingest"),
            ("issue-certificate", "--server", "--valid-days", "7", "--output-dir", str(self.root / "issued")),
        ):
            with self.subTest(command=argv[0]):
                status, _, errors = self.cli(*argv, stdin="value")
                self.assertEqual(status, 1)
                self.assertIn("Vault prerequisite check failed", errors)
        self.assertEqual(self.writes(), [])
        self.assertFalse(any("/data/" in path or "/sign/" in path for _, path, _ in self.fake.calls))
        self.assertFalse((self.root / "out").exists())

    def test_missing_credential_fails_and_names_the_ways_to_supply_one(self) -> None:
        status, _, errors = self.cli("materialize-secrets", "--output-dir", str(self.root / "out"), token=None)
        self.assertEqual(status, 1)
        self.assertIn("VAULT_TOKEN", errors)
        self.assertIn("--vault-token-file", errors)

    def test_materialize_refuses_an_invalid_configuration_without_contacting_vault(self) -> None:
        self.fill()
        self.data["credentials"][0]["secret_ref"] = "missing"
        self.config = write_document(self.root, self.data)
        self.fake.calls.clear()
        status, _, errors = self.cli("materialize-secrets", "--output-dir", str(self.root / "out"))
        self.assertEqual(status, 1)
        self.assertIn("unknown secret reference", errors)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.root / "out").exists())

    def test_materialize_and_clean_through_the_command_line(self) -> None:
        self.fill()
        output = self.root / "out"
        status, text, _ = self.cli("materialize-secrets", "--output-dir", str(output))
        self.assertEqual(status, 0)
        self.assertIn(f"Materialized {len(self.platform.secrets)} secret(s)", text)
        self.assertNotIn("value-of-", text)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["clean-secrets", "--output-dir", str(output)]), 0)
        self.assertFalse(output.exists())

    def test_doctor_prints_each_check_and_exits_by_result(self) -> None:
        status, output, _ = self.cli("doctor")
        self.assertEqual(status, 0)
        self.assertEqual(output.count("ok: "), 7)
        del self.fake.roles["nighthawk-server"]
        status, output, _ = self.cli("doctor")
        self.assertEqual(status, 1)
        self.assertIn("FAIL: PKI role nighthawk-server does not exist or is not readable", output)

    def test_removed_commands_are_gone(self) -> None:
        for command in ("generate-recipient", "encrypt-secret", "init-ca"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main([command])


class ProductionSafeguardTests(SecretsFixture):
    def setUp(self) -> None:
        super().setUp()
        self.data["profile"] = "production"
        self.data["vault"]["address"] = "https://vault.example.com:8200"
        self.load()

    def test_root_token_is_refused_before_any_read_or_write(self) -> None:
        for argv in (
            ["store-secret", "--config", str(self.config), "--secret", "example-ingest"],
            ["materialize-secrets", "--config", str(self.config), "--output-dir", str(self.root / "out")],
            ["generate-credential", "--config", str(self.config), "--credential", "example-ingest"],
        ):
            with self.subTest(command=argv[0]):
                status, _, errors = run_cli(self.fake, argv, token=ROOT_TOKEN, stdin="value")
                self.assertEqual(status, 1)
                self.assertIn("production refuses a Vault credential that carries the root policy", errors)
        self.assertEqual(self.fake.kv, {})
        self.assertFalse(any("/data/" in path for _, path, _ in self.fake.calls))

    def test_scoped_credential_with_a_tls_address_proceeds(self) -> None:
        status, _, errors = run_cli(
            self.fake, ["store-secret", "--config", str(self.config), "--secret", "example-ingest"], stdin="value",
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(self.fake.values(PATH), {"example-ingest": "value"})


class ValidationNeverContactsVaultTests(SecretsFixture):
    def test_validate_and_render_contracts_work_with_no_vault_and_no_credential(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "VAULT_TOKEN"}
        with mock.patch("nighthawk.vault.http_transport", side_effect=AssertionError("Vault was contacted")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("a network request was made")), \
                mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["validate", "--config", str(self.config)]), 0)
            output = self.root / "contracts"
            self.assertEqual(main(["render-contracts", "--config", str(self.config), "--output", str(output)]), 0)
        self.assertTrue((output / "vault" / "policy.hcl").is_file())
        self.assertTrue((output / "vault" / "pki-roles.json").is_file())
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
