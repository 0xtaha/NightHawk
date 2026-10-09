"""Small operational helpers: checksum-verified tool downloads and an HTTP readiness wait."""

from __future__ import annotations

import hashlib
import io
import platform as host_platform
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from nighthawk.config import ROOT, ConfigurationError, mapping, string

TOOLS_DIR = ROOT / ".tools"
Download = Callable[[str], bytes]
_ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def platform_key() -> tuple[str, str]:
    system = host_platform.system().lower()
    architecture = _ARCHITECTURES.get(host_platform.machine().lower())
    if system != "linux" or architecture is None:
        raise ConfigurationError(
            f"no pinned download for {host_platform.system()}/{host_platform.machine()}; install sops and age yourself"
        )
    return system, architecture


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as error:
        raise ConfigurationError(f"cannot download {url}: {getattr(error, 'reason', error)}") from None


def _install(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o755)


def fetch_tools(matrix: dict, tools_dir: Path = TOOLS_DIR, download: Download = _download) -> list[str]:
    """Download pinned sops and age when absent. A file is installed only if its checksum matches the matrix."""
    system, architecture = platform_key()
    tools = mapping(matrix["secrets_tools"])
    installed: list[str] = []
    plans = {
        "sops": (
            ["sops"],
            "https://github.com/getsops/sops/releases/download/v{version}/sops-v{version}.{system}.{arch}",
        ),
        "age": (
            ["age", "age-keygen"],
            "https://github.com/FiloSottile/age/releases/download/v{version}/age-v{version}-{system}-{arch}.tar.gz",
        ),
    }
    for tool, (binaries, template) in plans.items():
        if all((tools_dir / binary).exists() for binary in binaries):
            continue
        pin = mapping(tools[tool])
        expected = mapping(pin["sha256"]).get(f"{system}_{architecture}")
        if expected is None:
            raise ConfigurationError(f"{tool}: the compatibility matrix has no checksum for {system}_{architecture}")
        content = download(template.format(version=string(pin["version"]), system=system, arch=architecture))
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            # Nothing is written: the mismatching download is discarded.
            raise ConfigurationError(f"{tool}: download checksum {actual} does not match the pinned {expected}")
        tools_dir.mkdir(parents=True, exist_ok=True)
        if tool == "sops":
            _install(tools_dir / "sops", content)
        else:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                for binary in binaries:
                    member = archive.extractfile(f"age/{binary}")
                    if member is None:
                        raise ConfigurationError(f"age: archive has no {binary}")
                    _install(tools_dir / binary, member.read())
        installed.append(tool)
    return installed


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
