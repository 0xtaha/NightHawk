from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from nighthawk.config import ConfigurationError, ROOT, load_platform
from nighthawk.overrides import render_overrides
from nighthawk.retention import pyroscope_overrides
from tests.fakes import four_stream_document, write_document

BACKENDS = ("mimir", "loki", "tempo", "pyroscope")


class OverridesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = four_stream_document()

    def render(self, versions: Path | None = None):
        platform = load_platform(write_document(Path(self.temp.name), self.data))
        return render_overrides(platform) if versions is None else render_overrides(platform, versions)

    def signals(self, tenant: int, stream: int) -> dict:
        return self.data["tenants"][tenant]["datastreams"][stream]["signals"]

    def test_four_pairs_have_independent_entries_in_every_backend(self) -> None:
        self.signals(0, 1)["metrics"].update(retention="720h", ingestion_rate_samples_per_second=250)
        self.signals(1, 0)["logs"].update(retention="48h", ingestion_rate_bytes_per_second=3 * 1048576)
        self.signals(1, 1)["traces"].update(retention="24h", ingestion_rate_bytes_per_second=5000)
        self.signals(1, 1)["profiles"].update(retention="96h", ingestion_rate_bytes_per_second=524288)
        documents, _ = self.render()
        for backend in BACKENDS:
            self.assertEqual(set(documents[backend]), {"overrides"})
            self.assertEqual(sorted(documents[backend]["overrides"]), [
                "example-application", "example-infrastructure", "second-application", "second-infrastructure",
            ])
        self.assertEqual(documents["mimir"]["overrides"]["example-infrastructure"], {
            "compactor_blocks_retention_period": "720h", "ingestion_rate": 250,
        })
        self.assertEqual(documents["mimir"]["overrides"]["example-application"], {
            "compactor_blocks_retention_period": "168h", "ingestion_rate": 10000,
        })
        self.assertEqual(documents["loki"]["overrides"]["second-application"], {
            "retention_period": "48h", "ingestion_rate_mb": 3.0,
        })
        self.assertEqual(documents["tempo"]["overrides"]["second-infrastructure"], {
            "compaction": {"block_retention": "24h"},
            "ingestion": {"rate_limit_bytes": 5000, "burst_size_bytes": 5000},
        })
        self.assertEqual(documents["pyroscope"]["overrides"]["second-infrastructure"], {
            "retention_period": "96h", "ingestion_rate_mb": 0.5,
        })

    def test_disabled_signal_has_no_entry(self) -> None:
        del self.signals(0, 0)["traces"]
        del self.signals(0, 0)["profiles"]
        documents, unenforced = self.render()
        self.assertNotIn("example-application", documents["tempo"]["overrides"])
        self.assertNotIn("example-application", documents["pyroscope"]["overrides"])
        self.assertIn("example-application", documents["mimir"]["overrides"])
        self.assertFalse([
            item for item in unenforced
            if item["backend_id"] == "example-application" and item["backend"] in ("tempo", "pyroscope")
        ])

    def test_no_default_or_wildcard_tenant_entry(self) -> None:
        documents, _ = self.render()
        backend_ids = {stream["backend_id"] for tenant in self.data["tenants"] for stream in tenant["datastreams"]}
        for backend in BACKENDS:
            self.assertEqual(set(documents[backend]["overrides"]), backend_ids)
            self.assertNotIn("*", yaml.safe_dump(documents[backend]))

    def test_every_declared_value_is_rendered_or_reported(self) -> None:
        self.signals(1, 0)["logs"]["query_concurrency"] = 9
        documents, unenforced = self.render()
        keys = {"metrics": "mimir", "logs": "loki", "traces": "tempo", "profiles": "pyroscope"}
        reported = {(item["backend"], item["backend_id"]): item for item in unenforced}
        for tenant in self.data["tenants"]:
            for stream in tenant["datastreams"]:
                for signal, declared in stream["signals"].items():
                    entry = yaml.safe_dump(documents[keys[signal]]["overrides"][stream["backend_id"]])
                    self.assertIn(declared["retention"], entry)
                    item = reported[(keys[signal], stream["backend_id"])]
                    self.assertEqual((item["field"], item["value"]), ("query_concurrency", declared["query_concurrency"]))
                    self.assertTrue(item["reason"])
        self.assertEqual(reported[("loki", "second-application")]["value"], 9)
        self.assertEqual(len(unenforced), 16)

    def test_unreviewed_pin_fails_naming_the_backend(self) -> None:
        matrix = yaml.safe_load((ROOT / "config" / "versions.yaml").read_text(encoding="utf-8"))
        for backend in ("mimir", "loki", "tempo"):
            changed = yaml.safe_load(yaml.safe_dump(matrix))
            changed["backends"][backend]["version"] = "9.9.9"
            path = Path(self.temp.name) / f"{backend}.yaml"
            path.write_text(yaml.safe_dump(changed), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, f"{backend} overrides were reviewed for"):
                self.render(path)
        changed = yaml.safe_load(yaml.safe_dump(matrix))
        changed["backends"]["pyroscope"]["storage"] = "v1"
        path = Path(self.temp.name) / "pyroscope.yaml"
        path.write_text(yaml.safe_dump(changed), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "Pyroscope overrides require"):
            self.render(path)

    def test_retention_fragment_stays_importable_from_its_original_module(self) -> None:
        platform = load_platform(write_document(Path(self.temp.name), self.data))
        self.assertEqual(
            pyroscope_overrides(platform)["overrides"]["example-application"], {"retention_period": "168h"},
        )


if __name__ == "__main__":
    unittest.main()
