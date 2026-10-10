"""Small operational helpers: a checksum-verified Terraform download and an HTTP readiness wait."""

from __future__ import annotations

import hashlib
import io
import platform as host_platform
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from nighthawk.config import ROOT, ConfigurationError, mapping, string

TOOLS_DIR = ROOT / ".tools"
TERRAFORM_URL = "https://releases.hashicorp.com/terraform/{version}/terraform_{version}_{system}_{arch}.zip"
Download = Callable[[str], bytes]
_ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def platform_key() -> tuple[str, str]:
    system = host_platform.system().lower()
    architecture = _ARCHITECTURES.get(host_platform.machine().lower())
    if system != "linux" or architecture is None:
        raise ConfigurationError(
            f"no pinned download for {host_platform.system()}/{host_platform.machine()}; install Terraform yourself"
        )
    return system, architecture


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=300) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as error:
        raise ConfigurationError(f"cannot download {url}: {getattr(error, 'reason', error)}") from None


def fetch_tools(matrix: dict, tools_dir: Path = TOOLS_DIR, download: Download = _download) -> list[str]:
    """Download the pinned Terraform when absent. It is installed only if the archive's checksum matches the matrix."""
    system, architecture = platform_key()
    target = tools_dir / "terraform"
    if target.exists():
        return []
    pin = mapping(mapping(matrix["validation_tools"])["terraform"])
    version = string(pin["version"])
    expected = mapping(pin["sha256"]).get(f"{system}_{architecture}")
    if expected is None:
        raise ConfigurationError(f"terraform: the compatibility matrix has no checksum for {system}_{architecture}")
    content = download(TERRAFORM_URL.format(version=version, system=system, arch=architecture))
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        # Nothing is written: the mismatching download is discarded.
        raise ConfigurationError(f"terraform: download checksum {actual} does not match the pinned {expected}")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            binary = archive.read("terraform")
    except (zipfile.BadZipFile, KeyError) as error:
        raise ConfigurationError(f"terraform: the verified archive holds no terraform executable ({type(error).__name__})") from None
    tools_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(binary)
    target.chmod(0o755)
    return ["terraform"]


def wait_http(urls: list[str], timeout: float, interval: float = 2.0) -> None:
    """Return once every URL answers 200; raise naming the ones that did not."""
    deadline = time.monotonic() + timeout
    pending = list(urls)
    while True:
        still_pending = []
        for url in pending:
            try:
                with urllib.request.urlopen(url, timeout=min(5.0, max(timeout, 1.0))) as response:
                    if response.status != 200:
                        still_pending.append(url)
            except urllib.error.HTTPError as error:
                error.close()
                still_pending.append(url)
            except (urllib.error.URLError, OSError, ValueError):
                still_pending.append(url)
        pending = still_pending
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise ConfigurationError(f"not ready after {timeout:g}s: {', '.join(pending)}")
        time.sleep(interval)
