from __future__ import annotations

import copy
import hashlib
import http.server
import io
import tarfile
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from nighthawk.__main__ import main
from nighthawk.config import ConfigurationError, load_versions
from nighthawk.tools import TERRAFORM_URL, TOOLS, fetch_tools, wait_http


def terraform_archive(names: tuple[str, ...] = ("terraform", "LICENSE.txt")) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, f"content of {name}")
    return buffer.getvalue()


class FetchToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tools = Path(self.temp.name) / "tools"
        self.archive = terraform_archive()
        self.matrix = copy.deepcopy(load_versions())
        self.matrix["validation_tools"]["terraform"]["sha256"] = {"linux_amd64": hashlib.sha256(self.archive).hexdigest()}
        patcher = mock.patch("nighthawk.tools.platform_key", return_value=("linux", "amd64"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requested: list[str] = []

    def download(self, url: str) -> bytes:
        self.requested.append(url)
        return self.archive

    def test_verified_download_is_installed_executable(self) -> None:
        self.assertEqual(fetch_tools(self.matrix, self.tools, self.download, ("terraform",)), ["terraform"])
        version = self.matrix["validation_tools"]["terraform"]["version"]
        self.assertEqual(self.requested, [TERRAFORM_URL.format(version=version, system="linux", arch="amd64")])
        self.assertEqual((self.tools / "terraform").read_text(), "content of terraform")
        self.assertEqual((self.tools / "terraform").stat().st_mode & 0o777, 0o755)
        self.assertEqual(sorted(path.name for path in self.tools.iterdir()), ["terraform"])
        # Present already: nothing is downloaded again.
        self.assertEqual(fetch_tools(self.matrix, self.tools, self.download, ("terraform",)), [])
        self.assertEqual(len(self.requested), 1)

    def test_checksum_mismatch_installs_nothing_and_names_terraform(self) -> None:
        self.archive = terraform_archive(("terraform", "tampered"))
        with self.assertRaisesRegex(ConfigurationError, "terraform: download checksum .* does not match the pinned"):
            fetch_tools(self.matrix, self.tools, self.download, ("terraform",))
        self.assertFalse(self.tools.exists())

    def test_platform_without_a_checksum_fails_before_downloading(self) -> None:
        self.matrix["validation_tools"]["terraform"]["sha256"] = {"linux_arm64": "0" * 64}
        with self.assertRaisesRegex(ConfigurationError, "no checksum for linux_amd64"):
            fetch_tools(self.matrix, self.tools, self.download, ("terraform",))
        self.assertEqual(self.requested, [])

    def test_verified_archive_without_the_executable_installs_nothing(self) -> None:
        self.archive = terraform_archive(("README",))
        self.matrix["validation_tools"]["terraform"]["sha256"] = {"linux_amd64": hashlib.sha256(self.archive).hexdigest()}
        with self.assertRaisesRegex(ConfigurationError, "holds no terraform executable"):
            fetch_tools(self.matrix, self.tools, self.download, ("terraform",))
        self.assertFalse(self.tools.exists())

    def test_the_real_matrix_has_a_checksum_for_each_tool_and_supported_linux_build(self) -> None:
        pins = load_versions()["validation_tools"]
        for name in TOOLS:
            for build in ("linux_amd64", "linux_arm64"):
                self.assertRegex(pins[name]["sha256"][build], r"^[0-9a-f]{64}$", (name, build))

    def tarball(self, member: str, content: bytes = b"#!/bin/true") -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            entry = tarfile.TarInfo(member)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        return buffer.getvalue()

    def test_every_tool_is_fetched_from_its_pinned_release_and_verified(self) -> None:
        downloads = {
            "terraform": self.archive, "helm": self.tarball("linux-amd64/helm"),
            "kubectl": b"kubectl binary", "kubeconform": self.tarball("kubeconform"),
        }
        pins = self.matrix["validation_tools"]
        by_url = {}
        for name, content in downloads.items():
            pins[name]["sha256"] = {"linux_amd64": hashlib.sha256(content).hexdigest()}
            by_url[TOOLS[name].url.format(version=pins[name]["version"], system="linux", arch="amd64")] = content
        self.assertEqual(fetch_tools(self.matrix, self.tools, by_url.__getitem__), list(TOOLS))
        self.assertEqual(sorted(path.name for path in self.tools.iterdir()), sorted(TOOLS))
        self.assertEqual((self.tools / "kubectl").read_bytes(), b"kubectl binary")
        self.assertEqual((self.tools / "helm").read_bytes(), b"#!/bin/true")
        for name in TOOLS:
            self.assertEqual((self.tools / name).stat().st_mode & 0o777, 0o755)
        self.assertEqual(fetch_tools(self.matrix, self.tools, by_url.__getitem__), [])

    def test_a_tampered_helm_download_installs_nothing_and_names_helm(self) -> None:
        good = self.tarball("linux-amd64/helm")
        self.matrix["validation_tools"]["helm"]["sha256"] = {"linux_amd64": hashlib.sha256(good).hexdigest()}
        with self.assertRaisesRegex(ConfigurationError, "helm: download checksum .* does not match the pinned"):
            fetch_tools(self.matrix, self.tools, lambda url: good + b"x", ("helm",))
        self.assertFalse(self.tools.exists())
        # Verified, but not the layout Helm ships: still nothing is installed.
        other = self.tarball("somewhere/else")
        self.matrix["validation_tools"]["helm"]["sha256"] = {"linux_amd64": hashlib.sha256(other).hexdigest()}
        with self.assertRaisesRegex(ConfigurationError, "holds no helm executable"):
            fetch_tools(self.matrix, self.tools, lambda url: other, ("helm",))
        self.assertFalse(self.tools.exists())

    def test_command_line_reports_what_it_installed(self) -> None:
        output = io.StringIO()
        with mock.patch("nighthawk.tools.fetch_tools", return_value=["terraform"]) as fetched, redirect_stdout(output):
            self.assertEqual(main(["fetch-tools", "--tools-dir", str(self.tools)]), 0)
        self.assertEqual(fetched.call_args.args[1], self.tools)
        self.assertIn(f"Installed terraform into {self.tools}", output.getvalue())
        errors = io.StringIO()
        with mock.patch("nighthawk.tools.fetch_tools", side_effect=ConfigurationError("terraform: boom")), redirect_stderr(errors):
            self.assertEqual(main(["fetch-tools", "--tools-dir", str(self.tools)]), 1)
        self.assertIn("error: terraform: boom", errors.getvalue())


class WaitHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        test = self
        self.ready = False

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200 if test.ready else 503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/ready"

    def test_returns_when_every_url_is_ready(self) -> None:
        threading.Timer(0.3, lambda: setattr(self, "ready", True)).start()
        wait_http([self.url, self.url + "?second"], timeout=10, interval=0.1)

    def test_names_the_urls_that_never_became_ready(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "not ready after 0.3s: http://127.0.0.1:1/x, " + self.url.replace("?", "\\?")):
            wait_http(["http://127.0.0.1:1/x", self.url], timeout=0.3, interval=0.1)


if __name__ == "__main__":
    unittest.main()
