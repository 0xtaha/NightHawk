from __future__ import annotations

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import nighthawk.__main__ as main_module
from nighthawk.config import ROOT, ConfigurationError, load_versions
from nighthawk.secrets import (
    cleanup, doctor, encrypt_secret, ensure_doctor_ok, generate_recipient, guard_production_recipients,
    materialize, rotate_secret,
)


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def matching_runner(matrix: dict):
    sops_version = matrix["secrets_tools"]["sops"]["version"]
    age_version = matrix["secrets_tools"]["age"]["version"]

    def runner(args, **kwargs):
        tool = args[0]
        if tool == "sops":
            return FakeCompletedProcess(0, stdout=f"sops {sops_version} (latest)\n")
        if tool == "age":
            return FakeCompletedProcess(0, stdout=f"v{age_version}\n")
        raise AssertionError(f"unexpected tool: {tool}")

    return runner


def missing_binary_runner(args, **kwargs):
    raise FileNotFoundError(f"no such file or directory: {args[0]!r}")


def mismatched_runner(args, **kwargs):
    tool = args[0]
    if tool == "sops":
        return FakeCompletedProcess(0, stdout="sops 0.0.1 (latest)\n")
    if tool == "age":
        return FakeCompletedProcess(0, stdout="v0.0.1\n")
    raise AssertionError(f"unexpected tool: {tool}")


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_versions()

    def test_doctor_reports_success_when_versions_match(self) -> None:
        checks = doctor(self.matrix, runner=matching_runner(self.matrix))
        self.assertTrue(all(check.ok for check in checks))
        ensure_doctor_ok(self.matrix, runner=matching_runner(self.matrix))  # should not raise

    def test_doctor_reports_missing_binary(self) -> None:
        checks = doctor(self.matrix, runner=missing_binary_runner)
        self.assertTrue(all(not check.ok for check in checks))
        self.assertTrue(all("not available" in check.detail for check in checks))
        with self.assertRaises(ConfigurationError):
            ensure_doctor_ok(self.matrix, runner=missing_binary_runner)

    def test_doctor_reports_version_mismatch(self) -> None:
        checks = doctor(self.matrix, runner=mismatched_runner)
        self.assertTrue(all(not check.ok for check in checks))
        self.assertTrue(all("does not match the pinned version" in check.detail for check in checks))
        with self.assertRaisesRegex(ConfigurationError, "does not match the pinned version"):
            ensure_doctor_ok(self.matrix, runner=mismatched_runner)


def fake_age_keygen(public_key: str = "age1testrecipient00000000000000000000000000000000000000000000000"):
    def runner(args, **kwargs):
        path = Path(args[2])
        path.write_text(
            f"# created: 2026-01-01T00:00:00Z\n# public key: {public_key}\nAGE-SECRET-KEY-1TEST\n",
            encoding="utf-8",
        )
        return FakeCompletedProcess(0, stderr=f"Public key: {public_key}\n")

    return runner


def fake_sops_encrypt(args, **kwargs):
    output_path = Path(args[args.index("--output") + 1])
    input_path = Path(args[-1])
    output_path.write_text(f"sops:\n  age: []\ndata: ENC[{input_path.read_text(encoding='utf-8')}]\n", encoding="utf-8")
    return FakeCompletedProcess(0)


def fake_sops_rotate(args, **kwargs):
    target = Path(args[-1])
    target.write_text(target.read_text(encoding="utf-8") + "# rotated\n", encoding="utf-8")
    return FakeCompletedProcess(0)


def fake_sops_decrypt(args, **kwargs):
    extract = args[args.index("--extract") + 1]
    key = extract.strip('[]"')
    return FakeCompletedProcess(0, stdout=f"value-for-{key}")


class RecipientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = Path(self.temp.name) / "registry.json"
        self.patcher = mock.patch("nighthawk.secrets.LOCAL_RECIPIENTS_REGISTRY", self.registry)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_generates_fresh_recipient(self) -> None:
        path = Path(self.temp.name) / "key.txt"
        recipient = generate_recipient(path, runner=fake_age_keygen())
        self.assertTrue(recipient.startswith("age1"))
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn(recipient, self.registry.read_text(encoding="utf-8"))

    def test_refuses_to_overwrite_without_flag(self) -> None:
        path = Path(self.temp.name) / "key.txt"
        path.write_text("existing", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            generate_recipient(path, runner=fake_age_keygen())
        self.assertEqual(path.read_text(encoding="utf-8"), "existing")

    def test_overwrite_flag_allows_replacement(self) -> None:
        path = Path(self.temp.name) / "key.txt"
        path.write_text("existing", encoding="utf-8")
        recipient = generate_recipient(path, overwrite=True, runner=fake_age_keygen())
        self.assertTrue(recipient.startswith("age1"))
        self.assertNotEqual(path.read_text(encoding="utf-8"), "existing")


class EncryptSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.secrets_dir = Path(self.temp.name) / "secrets"

    def test_encrypts_a_new_secret(self) -> None:
        target = encrypt_secret(
            "local.sops.yaml", "mykey", "myvalue", ["age1recipient"],
            secrets_dir=self.secrets_dir, runner=fake_sops_encrypt,
        )
        self.assertTrue(target.exists())
        self.assertIn("myvalue", target.read_text(encoding="utf-8"))
        self.assertEqual(list(self.secrets_dir.glob("*.yaml")), [target])  # no stray plaintext temp file

    def test_rejects_empty_recipient_list(self) -> None:
        with self.assertRaises(ConfigurationError):
            encrypt_secret("local.sops.yaml", "mykey", "myvalue", [], secrets_dir=self.secrets_dir, runner=fake_sops_encrypt)
        self.assertFalse(self.secrets_dir.exists() and any(self.secrets_dir.iterdir()))


class RotateSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.secrets_dir = Path(self.temp.name) / "secrets"
        self.secrets_dir.mkdir()

    def test_rotates_an_existing_secret(self) -> None:
        target = self.secrets_dir / "local.sops.yaml"
        target.write_text("sops:\n  age: []\ndata: ENC[old]\n", encoding="utf-8")
        result = rotate_secret("local.sops.yaml", "mykey", "newvalue", secrets_dir=self.secrets_dir, runner=fake_sops_rotate)
        self.assertEqual(result, target)
        self.assertIn("# rotated", target.read_text(encoding="utf-8"))

    def test_rejects_rotating_a_nonexistent_secret(self) -> None:
        with self.assertRaises(ConfigurationError):
            rotate_secret("missing.sops.yaml", "mykey", "newvalue", secrets_dir=self.secrets_dir, runner=fake_sops_rotate)


class MaterializeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.secrets_dir = Path(self.temp.name) / "secrets"
        self.secrets_dir.mkdir()
        (self.secrets_dir / "local.sops.yaml").write_text(
            "sops:\n  age:\n    - recipient: age1recipient\ndata: ENC[...]\n", encoding="utf-8",
        )
        self.output_dir = Path(self.temp.name) / "materialized"
        self.platform_path = Path(self.temp.name) / "platform.yaml"
        self.platform_path.write_text(
            yaml.safe_dump(yaml.safe_load((ROOT / "config" / "tenants.example.yaml").read_text())),
            encoding="utf-8",
        )

    def test_materializes_a_valid_platform_document(self) -> None:
        platform = materialize(
            self.platform_path, root=Path(self.temp.name), output_dir=self.output_dir, runner=fake_sops_decrypt,
        )
        self.assertEqual(stat.S_IMODE(self.output_dir.stat().st_mode), 0o700)
        self.assertTrue(any(self.output_dir.rglob("*")))
        for reference in platform.secrets.values():
            content = (self.output_dir / reference.file / reference.key).read_text(encoding="utf-8")
            self.assertEqual(content, f"value-for-{reference.key}")

    def test_refuses_to_materialize_an_invalid_configuration(self) -> None:
        invalid = yaml.safe_load(self.platform_path.read_text(encoding="utf-8"))
        del invalid["deployment"]
        self.platform_path.write_text(yaml.safe_dump(invalid), encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            materialize(self.platform_path, root=Path(self.temp.name), output_dir=self.output_dir, runner=fake_sops_decrypt)
        self.assertFalse(self.output_dir.exists())


class CleanupTests(unittest.TestCase):
    def test_cleanup_removes_all_decrypted_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_dir = Path(temp) / "materialized"
            output_dir.mkdir(mode=0o700)
            (output_dir / "local.sops.yaml").mkdir()
            (output_dir / "local.sops.yaml" / "mykey").write_text("secret", encoding="utf-8")
            cleanup(output_dir)
            self.assertFalse(output_dir.exists())


class ProductionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = Path(self.temp.name) / "registry.json"
        self.registry.write_text('["age1autogenerated"]', encoding="utf-8")

    def test_rejects_auto_generated_recipient_in_production(self) -> None:
        with self.assertRaises(ConfigurationError):
            guard_production_recipients(
                "production", {"age1autogenerated"}, confirmed=False, registry_path=self.registry,
            )

    def test_accepts_operator_supplied_production_recipients(self) -> None:
        guard_production_recipients(
            "production", {"age1operator-supplied"}, confirmed=False, registry_path=self.registry,
        )  # should not raise: not a locally auto-generated recipient

    def test_confirmed_auto_generated_recipient_is_accepted(self) -> None:
        guard_production_recipients(
            "production", {"age1autogenerated"}, confirmed=True, registry_path=self.registry,
        )  # should not raise: explicit confirmation


class ValidationNeverDecryptsTests(unittest.TestCase):
    def test_validate_succeeds_without_secrets_files_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "platform.yaml"
            config_path.write_text((ROOT / "config" / "tenants.example.yaml").read_text(), encoding="utf-8")
            result = main_module.main(["validate", "--config", str(config_path)])
        self.assertEqual(result, 0)

    def test_render_contracts_succeeds_without_secrets_files_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "platform.yaml"
            config_path.write_text((ROOT / "config" / "tenants.example.yaml").read_text(), encoding="utf-8")
            output_dir = Path(temp) / "out"
            result = main_module.main(["render-contracts", "--config", str(config_path), "--output", str(output_dir)])
        self.assertEqual(result, 0)


class DoctorGateCliTests(unittest.TestCase):
    def test_failing_doctor_check_blocks_generate_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "key.txt"
            with mock.patch.object(main_module, "ensure_doctor_ok", side_effect=ConfigurationError("boom")), \
                 mock.patch.object(main_module, "generate_recipient", side_effect=AssertionError("must not run")):
                result = main_module.main(["generate-recipient", "--output", str(target)])
            self.assertEqual(result, 1)
            self.assertFalse(target.exists())

    def test_failing_doctor_check_blocks_encrypt_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            secrets_dir = Path(temp) / "secrets"
            with mock.patch.object(main_module, "ensure_doctor_ok", side_effect=ConfigurationError("boom")), \
                 mock.patch.object(main_module, "encrypt_secret", side_effect=AssertionError("must not run")):
                result = main_module.main([
                    "encrypt-secret", "--file", "local.sops.yaml", "--key", "mykey", "--value", "v",
                    "--recipient", "age1recipient", "--secrets-dir", str(secrets_dir),
                ])
            self.assertEqual(result, 1)
            self.assertFalse(secrets_dir.exists())


if __name__ == "__main__":
    unittest.main()
