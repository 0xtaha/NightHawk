from __future__ import annotations

import functools
import gzip
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from nighthawk import fixtures
from nighthawk.__main__ import main
from nighthawk.config import ConfigurationError, load_platform
from tests.fakes import example_document, write_document


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = example_document()
        self.posts: list[tuple[str, dict[str, str], bytes]] = []
        self.statuses: dict[str, int] = {}

    def stream(self):
        return load_platform(write_document(Path(self.temp.name), self.data)).streams[0]

    def post(self, url: str, headers: dict[str, str], body: bytes) -> int:
        self.posts.append((url, headers, body))
        return next((status for part, status in self.statuses.items() if part in url), 200)

    def emit(self, run_id: str = "run1") -> dict:
        return fixtures.emit(self.stream(), run_id, "http://alloy:4318/", "http://alloy:4040", now_ns=1_700_000_000_000_000_000, post=self.post)

    def test_every_signal_is_sent_with_the_run_identifier(self) -> None:
        sent = self.emit()
        self.assertEqual(sent["signals"], ["metrics", "logs", "traces", "profiles"])
        self.assertEqual([urlsplit(url).path for url, _, _ in self.posts], ["/v1/metrics", "/v1/logs", "/v1/traces", "/ingest"])
        for url, headers, body in self.posts[:3]:
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertIn("run1", json.dumps(json.loads(body)))
        metrics, logs, traces = (json.loads(body) for _, _, body in self.posts[:3])
        point = metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
        self.assertEqual(point["name"], fixtures.METRIC)
        self.assertEqual(point["gauge"]["dataPoints"][0]["asDouble"], 42.0)
        self.assertEqual(len(logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"]), 4)
        spans = traces["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual([span["name"] for span in spans], list(fixtures.SPAN_NAMES))
        self.assertEqual({span["traceId"] for span in spans}, {sent["trace_id"]})
        self.assertEqual(spans[1]["parentSpanId"], spans[0]["spanId"])
        query = parse_qs(urlsplit(self.posts[3][0]).query)
        self.assertIn("run_id=run1", query["name"][0])
        self.assertEqual(query["format"], ["pprof"])
        self.assertIn(b"fixture_work", gzip.decompress(self.posts[3][2]))

    def test_output_is_deterministic_and_differs_per_run(self) -> None:
        first = self.emit()
        first_posts = list(self.posts)
        self.posts.clear()
        self.assertEqual(self.emit(), first)
        self.assertEqual(self.posts, first_posts)
        self.assertNotEqual(self.emit("run2")["trace_id"], first["trace_id"])

    def test_every_drop_field_carries_a_unique_marker_in_every_position(self) -> None:
        sent = self.emit()
        fields = self.stream().drop_fields
        self.assertEqual(sorted(sent["markers"]), [
            "logattr", "logjson", "logkv", "metric", "profile", "resource", "span", "spanevent",
        ])
        values = [value for by_field in sent["markers"].values() for value in by_field.values()]
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(len(values), 8 * len(fields))
        everything = b"\n".join(body for _, _, body in self.posts[:3]).decode() + self.posts[3][0]
        for position, by_field in sent["markers"].items():
            self.assertEqual(sorted(by_field), sorted(fields))
            for value in by_field.values():
                self.assertIn(value, everything, position)
        self.assertIn(sent["free_text_marker"], everything)
        logs = json.loads(self.posts[1][2])
        bodies = [record["body"]["stringValue"] for record in logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"]]
        self.assertIn(f"password={sent['markers']['logkv']['password']}", bodies[1])
        self.assertIn(f'"password": "{sent["markers"]["logjson"]["password"]}"', bodies[2])
        self.assertNotIn("=", bodies[3])

    def test_only_enabled_signals_are_sent(self) -> None:
        signals = self.data["tenants"][0]["datastreams"][0]["signals"]
        del signals["traces"]
        del signals["profiles"]
        del self.data["gateway"]["upstreams"]["profiles"]
        self.assertEqual(self.emit()["signals"], ["metrics", "logs"])
        self.assertEqual(len(self.posts), 2)

    def test_rejection_names_the_signal_and_status(self) -> None:
        self.statuses = {"/v1/logs": 403}
        with self.assertRaisesRegex(ConfigurationError, "^logs: fixture rejected with HTTP 403$"):
            self.emit()
        self.statuses = {"/ingest": 404}
        with self.assertRaisesRegex(ConfigurationError, "^profiles: fixture rejected with HTTP 404$"):
            self.emit()
        with self.assertRaisesRegex(ConfigurationError, "alphanumeric"):
            self.emit("bad id")

    def test_cli_exits_nonzero_on_rejection(self) -> None:
        config = write_document(Path(self.temp.name), self.data)
        arguments = [
            "emit-fixtures", "--config", str(config), "--tenant", "example", "--datastream", "application",
            "--run-id", "run1", "--otlp", "http://alloy:4318", "--profiles", "http://alloy:4040",
        ]
        errors = io.StringIO()
        rejecting = functools.partial(fixtures.emit, post=lambda url, headers, body: 500)
        with mock.patch("nighthawk.__main__.fixtures.emit", rejecting), redirect_stderr(errors):
            self.assertEqual(main(arguments), 1)
        self.assertIn("metrics: fixture rejected with HTTP 500", errors.getvalue())
        output = io.StringIO()
        accepting = functools.partial(fixtures.emit, post=lambda url, headers, body: 200)
        with mock.patch("nighthawk.__main__.fixtures.emit", accepting), redirect_stdout(output):
            self.assertEqual(main(arguments), 0)
        self.assertEqual(json.loads(output.getvalue())["run_id"], "run1")


if __name__ == "__main__":
    unittest.main()
