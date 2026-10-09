from __future__ import annotations

import copy
import hashlib
import http.server
import io
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from nighthawk.config import ConfigurationError, load_versions
from nighthawk.tools import fetch_tools, wait_http


def age_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("age/age", "age/age-keygen", "age/LICENSE"):
            content = f"binary {name}".encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class FetchToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tools = Path(self.temp.name) / "tools"
        self.downloads = {"sops": b"fake sops binary", "age": age_archive()}
        self.matrix = copy.deepcopy(load_versions())
        for tool, content in self.downloads.items():
            self.matrix["secrets_tools"][tool]["sha256"] = {"linux_amd64": hashlib.sha256(content).hexdigest()}
        patcher = mock.patch("nighthawk.tools.platform_key", return_value=("linux", "amd64"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requested: list[str] = []

    def download(self, url: str) -> bytes:
        self.requested.append(url)
        return self.downloads["sops" if "/sops/" in url else "age"]

    def test_matching_downloads_are_installed_executable(self) -> None:
        self.assertEqual(fetch_tools(self.matrix, self.tools, self.download), ["sops", "age"])
        self.assertEqual(sorted(path.name for path in self.tools.iterdir()), ["age", "age-keygen", "sops"])
        for path in self.tools.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.tools / "age-keygen").read_bytes(), b"binary age/age-keygen")
        self.assertIn("sops-v3.13.3.linux.amd64", self.requested[0])
        self.assertIn("age-v1.3.2-linux-amd64.tar.gz", self.requested[1])
        # Present tools are not downloaded again.
        self.requested.clear()
        self.assertEqual(fetch_tools(self.matrix, self.tools, self.download), [])
        self.assertEqual(self.requested, [])

    def test_checksum_mismatch_is_discarded_and_names_the_tool(self) -> None:
        self.downloads["sops"] = b"tampered"
        with self.assertRaisesRegex(ConfigurationError, "^sops: download checksum"):
            fetch_tools(self.matrix, self.tools, self.download)
        self.assertFalse(self.tools.exists())
        self.downloads["sops"] = b"fake sops binary"
        self.downloads["age"] = b"tampered"
        with self.assertRaisesRegex(ConfigurationError, "^age: download checksum"):
            fetch_tools(self.matrix, self.tools, self.download)
        self.assertEqual([path.name for path in self.tools.iterdir()], ["sops"])

    def test_platform_without_a_checksum_fails(self) -> None:
        with mock.patch("nighthawk.tools.platform_key", return_value=("linux", "riscv64")):
            with self.assertRaisesRegex(ConfigurationError, "no checksum for linux_riscv64"):
                fetch_tools(self.matrix, self.tools, self.download)


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
