"""Consume real Terraform mock-plan outputs through the Python renderer."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from nighthawk.__main__ import main
from nighthawk.config import ROOT, load_platform


class TerraformStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        terraform = os.environ.get("NIGHTHAWK_TERRAFORM") or shutil.which("terraform")
        if not terraform:
            raise RuntimeError(
                "Terraform 1.13.5 is required; initialize both storage modules and set NIGHTHAWK_TERRAFORM."
            )
        cls.plans = {}
        for module in ("aws-s3-backends", "object-storage"):
            result = subprocess.run(
                [terraform, "test", "-json", "-verbose"],
                cwd=ROOT / "terraform" / "modules" / module,
                capture_output=True, text=True, encoding="utf-8", check=False,
            )
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            if result.returncode:
                diagnostics = "\n".join(
                    json.dumps(event) for event in events
                    if event.get("@level") == "error" or event.get("type") == "test_summary"
                )
                raise RuntimeError(f"{module} mocked tests failed:\n{diagnostics}\n{result.stderr}")
            cls.plans[module] = {
                event["@testrun"]: event["test_plan"] for event in events if event.get("type") == "test_plan"
            }

    def test_adapter_output_imports_and_renders(self) -> None:
        example = yaml.safe_load((ROOT / "config" / "tenants.example.yaml").read_text())
        example["deployment"] = "aws"
        for run in ("secure_storage_contract", "versioned_storage"):
            with self.subTest(run=run), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                storage = self.plans["aws-s3-backends"][run]["output_changes"]["storage"]["after"]
                storage_path, config_path = directory / "storage.json", directory / "platform.yaml"
                storage_path.write_text(json.dumps(storage), encoding="utf-8")
                config_path.write_text(yaml.safe_dump(example), encoding="utf-8")
                platform = load_platform(config_path, storage_path)
                self.assertEqual(platform.storage_provider, "aws")
                self.assertEqual(
                    {binding.identity.ref for binding in platform.bindings.values()},
                    {f"arn:aws:iam::123456789012:role/test-{backend}-storage" for backend in (
                        "mimir", "loki", "tempo", "pyroscope"
                    )},
                )
                output = directory / "rendered"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main([
                        "render-contracts", "--config", str(config_path),
                        "--storage-output", str(storage_path), "--output", str(output),
                    ]), 0)
                rendered = json.loads((output / "platform.json").read_text())
                self.assertEqual(rendered["bindings"], storage["bindings"])
                self.assertEqual(
                    yaml.safe_load((output / "pyroscope-overrides.yaml").read_text()),
                    {"overrides": {"example-application": {"retention_period": "168h"}}},
                )

    def test_facade_preserves_output_contract(self) -> None:
        storage = self.plans["object-storage"]["aws_contract"]["output_changes"]["storage"]["after"]
        self.assertEqual(storage["provider"], "aws")
        self.assertEqual(set(storage["bindings"]), {"tempo-traces"})
        self.assertEqual(storage["bindings"]["tempo-traces"]["endpoint"], "https://s3.eu-west-1.amazonaws.com")

    def test_current_telemetry_has_no_lifecycle_expiration_or_archive(self) -> None:
        for run in ("secure_storage_contract", "versioned_storage"):
            plan = self.plans["aws-s3-backends"][run]
            resources = [
                change for change in plan["resource_changes"]
                if change["type"] == "aws_s3_bucket_lifecycle_configuration"
            ]
            self.assertTrue(resources)
            for resource in resources:
                with self.subTest(run=run, resource=resource["address"]):
                    for rule in resource["change"]["after"]["rule"]:
                        self.assertFalse(rule["transition"])
                        self.assertFalse(rule["noncurrent_version_transition"])
                        for expiration in rule["expiration"]:
                            self.assertTrue(expiration["expired_object_delete_marker"])
                            self.assertIsNone(expiration.get("days"))
                            self.assertIsNone(expiration.get("date"))

    def test_partition_aware_output_imports(self) -> None:
        data = copy.deepcopy(yaml.safe_load((ROOT / "config" / "tenants.example.yaml").read_text()))
        data["deployment"] = "aws"
        signals = data["tenants"][0]["datastreams"][0]["signals"]
        for signal in tuple(signals):
            if signal != "traces":
                del signals[signal]
        storage = self.plans["aws-s3-backends"]["china_partition"]["output_changes"]["storage"]["after"]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config_path, storage_path = directory / "platform.yaml", directory / "storage.json"
            config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
            storage_path.write_text(json.dumps(storage), encoding="utf-8")
            platform = load_platform(config_path, storage_path)
        self.assertEqual(platform.bindings["tempo-traces"].region, "cn-north-1")
        self.assertTrue(platform.bindings["tempo-traces"].identity.ref.startswith("arn:aws-cn:"))


if __name__ == "__main__":
    unittest.main()
