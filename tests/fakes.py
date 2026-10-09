"""Shared test doubles: a file-backed fake SOPS and platform-document builders."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from nighthawk.config import ROOT, Platform


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_sops(args, **kwargs):
    """Keeps values in clear text under their keys, as SOPS keeps key names in clear text."""
    if "--encrypt" in args:
        target = Path(args[args.index("--output") + 1])
        document = yaml.safe_load(Path(args[-1]).read_text(encoding="utf-8"))
        recipients = args[args.index("--age") + 1].split(",")
        document["sops"] = {"age": [{"recipient": recipient} for recipient in recipients]}
        target.write_text(yaml.safe_dump(document), encoding="utf-8")
        return FakeCompletedProcess(0)
    if args[1] == "set":
        target, key = Path(args[3]), args[4].strip('[]"')
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
        document[key] = json.loads(kwargs["input"])
        target.write_text(yaml.safe_dump(document), encoding="utf-8")
        return FakeCompletedProcess(0)
    if "--decrypt" in args:
        key = args[args.index("--extract") + 1].strip('[]"')
        document = yaml.safe_load(Path(args[-1]).read_text(encoding="utf-8"))
        return FakeCompletedProcess(0, stdout=document[key])
    raise AssertionError(f"unexpected sops call: {args}")


def example_document() -> dict:
    return yaml.safe_load((ROOT / "config" / "tenants.example.yaml").read_text(encoding="utf-8"))


def add_stream(data: dict, tenant_id: str, stream_id: str, backend_id: str, certificate: bool = False) -> None:
    stream = copy.deepcopy(data["tenants"][0]["datastreams"][0])
    stream.update(id=stream_id, backend_id=backend_id)
    tenant = next((item for item in data["tenants"] if item["id"] == tenant_id), None)
    if tenant is None:
        tenant = {"id": tenant_id, "datastreams": []}
        data["tenants"].append(tenant)
    tenant["datastreams"].append(stream)
    for permission in ("ingest", "query"):
        credential_id = f"{tenant_id}-{stream_id}-{permission}"
        data["secrets"][credential_id] = {"file": "secrets/local.sops.yaml", "key": credential_id}
        credential = {
            "id": credential_id, "secret_ref": credential_id,
            "tenant": tenant_id, "datastream": stream_id, "permission": permission,
        }
        if certificate and permission == "ingest":
            credential["certificate_identity"] = f"spiffe://nighthawk/{tenant_id}/{stream_id}/collector"
        data["credentials"].append(credential)


def four_stream_document() -> dict:
    """Two customers with two datastreams each."""
    data = example_document()
    add_stream(data, "example", "infrastructure", "example-infrastructure")
    add_stream(data, "second", "application", "second-application")
    add_stream(data, "second", "infrastructure", "second-infrastructure")
    return data


def write_document(directory: Path, data: dict, name: str = "platform.yaml") -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def secret_for(credential_id: str) -> str:
    return f"{credential_id}-" + "s" * 40


def materialize_plain(platform: Platform, directory: Path, overrides: dict[str, str] | None = None) -> Path:
    """Write gateway credential secrets where `materialize-secrets` would put them."""
    for credential in platform.credentials:
        reference = platform.secrets[credential.secret_ref]
        target = directory / reference.file
        target.mkdir(parents=True, exist_ok=True)
        value = (overrides or {}).get(credential.id, secret_for(credential.id))
        (target / reference.key).write_text(value + "\n", encoding="utf-8")
    return directory


def basic(credential_id: str, secret: str | None = None) -> str:
    value = f"{credential_id}:{secret_for(credential_id) if secret is None else secret}"
    return "Basic " + base64.b64encode(value.encode("utf-8")).decode("ascii")


def forwarded_certificate(path: Path) -> str:
    """Encode a certificate as the pinned Traefik passTLSClientCert middleware does."""
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    return base64.b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode("ascii")
