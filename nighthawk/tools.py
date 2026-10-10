"""Small operational helpers: checksum-verified tool downloads and an HTTP readiness wait."""

from __future__ import annotations

import hashlib
import io
import platform as host_platform
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nighthawk.config import ROOT, ConfigurationError, mapping, string

TOOLS_DIR = ROOT / ".tools"
TERRAFORM_URL = "https://releases.hashicorp.com/terraform/{version}/terraform_{version}_{system}_{arch}.zip"
Download = Callable[[str], bytes]
_ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


@dataclass(frozen=True)
class Tool:
    """Where a pinned tool is downloaded from and where its executable is in the download."""
    url: str
    # `zip` or `tar`: the executable is the named member. `binary`: the download is the executable.
    kind: str
    member: str = ""


TOOLS = {
    "terraform": Tool(TERRAFORM_URL, "zip", "terraform"),
    "helm": Tool("https://get.helm.sh/helm-v{version}-{system}-{arch}.tar.gz", "tar", "{system}-{arch}/helm"),
    "kubectl": Tool("https://dl.k8s.io/release/v{version}/bin/{system}/{arch}/kubectl", "binary"),
    "kubeconform": Tool(
        "https://github.com/yannh/kubeconform/releases/download/v{version}/kubeconform-{system}-{arch}.tar.gz",
        "tar", "kubeconform",
    ),
}


def platform_key() -> tuple[str, str]:
    system = host_platform.system().lower()
    architecture = _ARCHITECTURES.get(host_platform.machine().lower())
    if system != "linux" or architecture is None:
        raise ConfigurationError(
            f"no pinned download for {host_platform.system()}/{host_platform.machine()}; install the tools yourself"
        )
    return system, architecture


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=300) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as error:
        raise ConfigurationError(f"cannot download {url}: {getattr(error, 'reason', error)}") from None


def _executable(name: str, tool: Tool, content: bytes, member: str) -> bytes:
    if tool.kind == "binary":
        return content
    try:
        if tool.kind == "zip":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return archive.read(member)
        with tarfile.open(fileobj=io.BytesIO(content)) as archive:
            extracted = archive.extractfile(member)
            if extracted is None:
                raise KeyError(member)
            return extracted.read()
    except (zipfile.BadZipFile, tarfile.TarError, KeyError) as error:
        raise ConfigurationError(f"{name}: the verified archive holds no {name} executable ({type(error).__name__})") from None


def fetch_tools(
    matrix: dict, tools_dir: Path = TOOLS_DIR, download: Download = _download, names: tuple[str, ...] = tuple(TOOLS),
) -> list[str]:
    """Download each pinned tool that is absent. One is installed only if its download's checksum matches the matrix."""
    system, architecture = platform_key()
    pins = mapping(matrix["validation_tools"])
    installed: list[str] = []
    for name in names:
        tool, target = TOOLS[name], tools_dir / name
        if target.exists():
            continue
        pin = mapping(pins[name])
        version = string(pin["version"])
        expected = mapping(pin["sha256"]).get(f"{system}_{architecture}")
        if expected is None:
            raise ConfigurationError(f"{name}: the compatibility matrix has no checksum for {system}_{architecture}")
        content = download(tool.url.format(version=version, system=system, arch=architecture))
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            # Nothing is written: the mismatching download is discarded.
            raise ConfigurationError(f"{name}: download checksum {actual} does not match the pinned {expected}")
        binary = _executable(name, tool, content, tool.member.format(system=system, arch=architecture))
        tools_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(binary)
        target.chmod(0o755)
        installed.append(name)
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
