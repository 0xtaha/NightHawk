from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nighthawk.config import ConfigurationError, load_platform
from nighthawk.storage import backend_environment, buckets, s3_identities
from tests.fakes import example_document, write_document


def reader(values: dict[str, str]):
    return lambda reference: values[reference]


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = load_platform(write_document(Path(self.temp.name), example_document()))
        self.values = {
            f"{name}-storage": json.dumps({"access_key": f"{name.upper()}KEY", "secret_key": f"{name}-secret-value"})
            for name in ("mimir", "loki", "tempo", "pyroscope")
        }

    def test_one_identity_per_backend_limited_to_its_buckets(self) -> None:
        document = s3_identities(self.platform, reader(self.values))
        identities = {item["name"]: item for item in document["identities"]}
        self.assertEqual(sorted(identities), ["loki-storage", "mimir-storage", "pyroscope-storage", "tempo-storage"])
        self.assertEqual(identities["mimir-storage"]["actions"], [
            f"{action}:{bucket}"
            for bucket in ("nighthawk-mimir-alertmanager", "nighthawk-mimir-blocks", "nighthawk-mimir-ruler")
            for action in ("Read", "Write", "List")
        ])
        self.assertEqual(identities["loki-storage"]["actions"], [
            "Read:nighthawk-loki-chunks", "Write:nighthawk-loki-chunks", "List:nighthawk-loki-chunks",
        ])
        self.assertEqual(identities["tempo-storage"]["credentials"], [
            {"accessKey": "TEMPOKEY", "secretKey": "tempo-secret-value"},
        ])
        for identity in identities.values():
            self.assertNotIn("Admin", identity["actions"])
            self.assertTrue(all(":" in action for action in identity["actions"]))
        self.assertNotIn("anonymous", json.dumps(document).lower())
        self.assertEqual(document, s3_identities(self.platform, reader(self.values)))

    def test_bucket_list_is_every_binding(self) -> None:
        self.assertEqual(buckets(self.platform), sorted(
            binding.bucket for binding in self.platform.bindings.values()
        ))

    def test_backend_environment_carries_each_backends_own_keys(self) -> None:
        environment = backend_environment(self.platform, reader(self.values))
        self.assertEqual(sorted(environment), ["loki", "mimir", "pyroscope", "tempo"])
        self.assertEqual(
            environment["loki"], "NIGHTHAWK_S3_ACCESS_KEY=LOKIKEY\nNIGHTHAWK_S3_SECRET_KEY=loki-secret-value\n",
        )

    def test_malformed_identity_names_the_reference_without_printing_it(self) -> None:
        for bad in ("plain-old-secret", '{"access_key": "A"}', '{"access_key": "", "secret_key": "s"}',
                    '{"access_key": "A", "secret_key": "s", "extra": 1}', "[]"):
            self.values["loki-storage"] = bad
            with self.assertRaises(ConfigurationError) as raised:
                s3_identities(self.platform, reader(self.values))
            self.assertIn("loki-storage", str(raised.exception))
            self.assertNotIn("plain-old-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
