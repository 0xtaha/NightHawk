"""End-to-end checks against the running Compose stack.

Skipped unless NIGHTHAWK_E2E=1. They start their own stack (project `nighthawk-e2e`) from
tests/e2e/platform.yaml, so the default stack must be stopped first: both publish 8443.
They also start their own disposable dev-mode Vault (container `nighthawk-e2e-vault`, the
image pinned in config/versions.yaml) on loopback port $NIGHTHAWK_E2E_VAULT_PORT (default
8210) and bootstrap it. Set NIGHTHAWK_E2E_KEEP=1 to leave the stack and that Vault running.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from nighthawk import fixtures, quickstart, secrets, trust, vault
from nighthawk.config import ROOT, load_platform, load_versions
from nighthawk.grafana import datasource_uid
from nighthawk.secrets import materialized_path

ENABLED = os.environ.get("NIGHTHAWK_E2E") == "1"
WORK = ROOT / ".generated" / "e2e"
CONFIG = WORK / "platform.yaml"
SECRETS = ROOT / ".materialized-secrets-e2e"
PORT = 8443
VAULT_CONTAINER = "nighthawk-e2e-vault"
VAULT_ADDRESS = f"http://127.0.0.1:{int(os.environ.get('NIGHTHAWK_E2E_VAULT_PORT', '8210'))}"
# A throwaway dev-mode server on loopback; this token protects nothing and is never written to disk.
VAULT_DEV_TOKEN = "nighthawk-e2e-dev-root"
PROFILE_TYPE = fixtures.PROFILE_TYPE


# The external entry point, as a deployment for another machine publishes it. A second loopback
# address and an unprivileged port stand in for the host's own address and the contract's port.
EXTERNAL_ADDRESS, EXTERNAL_PORT = "127.0.0.2", 9444
EXTERNAL: dict[str, object] = {}


def options(*credentials: str) -> quickstart.Options:
    compose = tuple(os.environ.get("NIGHTHAWK_COMPOSE", "docker compose").split())
    return quickstart.Options(
        config=CONFIG, compose=compose, container=compose[0], rendered_dir=WORK / "rendered",
        secrets_dir=SECRETS, project="nighthawk-e2e", build=False, timeout=600, tenant="acme", datastream="web",
        credentials=credentials, **EXTERNAL,
    )


def bring_up(*credentials: str) -> None:
    quickstart.quickstart(options(*credentials), out=lambda line: None)


def hours(duration: str) -> float:
    """Hours in a duration as a backend reports it: `1w`, `4d`, `96h`, or `48h0m0s`."""
    units = {"w": 168, "d": 24, "h": 1, "m": 1 / 60, "s": 1 / 3600}
    parts = re.findall(r"([0-9]+)([wdhms])", duration.strip())
    if not parts or "".join(number + unit for number, unit in parts) != duration.strip():
        raise AssertionError(f"not a duration: {duration!r}")
    return sum(int(number) * units[unit] for number, unit in parts)


def start_vault() -> None:
    """Start the pinned Vault image in dev mode, unless a kept one is already answering, and bootstrap it."""
    def healthy() -> bool:
        try:
            with urllib.request.urlopen(f"{VAULT_ADDRESS}/v1/sys/health", timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    container = options().container
    if not healthy():
        image = load_versions()["secrets_store"]["vault"]["image"]
        subprocess.run([container, "rm", "--force", VAULT_CONTAINER], capture_output=True, timeout=60)
        result = subprocess.run([
            container, "run", "--detach", "--name", VAULT_CONTAINER, "--cap-add", "IPC_LOCK",
            "--publish", f"{VAULT_ADDRESS.removeprefix('http://')}:8200",
            "--env", f"VAULT_DEV_ROOT_TOKEN_ID={VAULT_DEV_TOKEN}", "--env", "VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200",
            f"{image['repository']}@{image['digest']}", "server", "-dev",
        ], capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise AssertionError(f"could not start the development Vault: {result.stderr[-600:]}")
        if not eventually(healthy, timeout=60, interval=1):
            raise AssertionError("the development Vault did not become healthy")
    os.environ["VAULT_TOKEN"] = VAULT_DEV_TOKEN
    platform = load_platform(CONFIG)
    vault.bootstrap_dev(platform, vault.connect(platform), confirmed=True, ca_valid_days=365)


def compose(*args: str, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [*quickstart._compose(options()), *args], capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"compose {' '.join(args)} failed: {result.stderr[-600:]}")
    return result


def in_network(code: str) -> str:
    """Run Python inside the backend network, where the backends are reachable directly."""
    return compose("run", "--rm", "--no-deps", "--entrypoint", "python", "wait-backends", "-c", code).stdout


def secret(reference: str) -> str:
    platform = load_platform(CONFIG)
    return materialized_path(SECRETS, platform.secrets[reference]).read_text(encoding="utf-8").rstrip("\r\n")


def call(
    path: str, credential: str | None = None, *, host: str | None = None, method: str = "GET",
    headers: dict[str, str] | None = None, data: bytes | None = None, params: dict[str, str] | None = None,
    certificate: Path | None = None, password: str | None = None, user: str | None = None, http2: bool = False,
    include_headers: bool = False, address: str = "127.0.0.1", port: int = PORT, published: int | None = None,
) -> tuple[int, str]:
    platform = load_platform(CONFIG)
    host = host or platform.gateway.hostname
    command = [
        "curl", "--silent", "--max-time", "30", "--cacert", str(SECRETS / "runtime" / "grafana" / "gateway-ca.pem"),
        "--resolve", f"{host}:{published or port}:{address}", "--request", method, "--write-out", "\n%{http_code}",
    ]
    if credential is not None or user is not None:
        name = user or credential
        command += ["--user", f"{name}:{password if password is not None else secret(credential or '')}"]
    for key, value in (headers or {}).items():
        command += ["--header", f"{key}: {value}"]
    if certificate is not None:
        command += ["--cert", str(certificate), "--key", str(certificate).replace(".crt.pem", ".key.pem")]
    if http2:
        command += ["--http2"]
    if include_headers:
        command += ["--include"]
    if params:
        command += ["--get"] if method == "GET" else []
        for key, value in params.items():
            command += ["--data-urlencode", f"{key}={value}"]
    if data is not None:
        command += ["--data-binary", "@-"]
    result = subprocess.run(
        command + [f"https://{host}:{published or port}{path}"], input=data, capture_output=True, timeout=60,
    )
    body, _, status = result.stdout.decode("utf-8", "replace").rpartition("\n")
    return int(status or 0), body


def push_log(credential: str, line: str, headers: dict[str, str] | None = None, **options) -> int:
    body = json.dumps({"streams": [{"stream": {"job": "e2e"}, "values": [[str(time.time_ns()), line]]}]})
    status, _ = call(
        "/logs/loki/api/v1/push", credential, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})}, data=body.encode("utf-8"), **options,
    )
    return status


def query_logs(credential: str, needle: str, **options) -> tuple[int, list[str]]:
    status, body = call(
        "/logs/loki/api/v1/query_range", credential, params={"query": f'{{service_name=~".+"}} |= "{needle}"', "limit": "100"},
        **options,
    )
    if status != 200:
        return status, []
    return status, [value[1] for stream in json.loads(body)["data"]["result"] for value in stream["values"]]


def eventually(check, timeout: float = 90, interval: float = 3):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def emit(tenant: str, datastream: str, run_id: str) -> dict:
    output = compose(
        "run", "--rm", "--no-deps", "fixtures", "--tenant", tenant, "--datastream", datastream, "--run-id", run_id,
    ).stdout
    return json.loads(output.strip().splitlines()[-1])


def setUpModule() -> None:
    if not ENABLED:
        raise unittest.SkipTest("set NIGHTHAWK_E2E=1 to run against a live stack")
    WORK.mkdir(parents=True, exist_ok=True)
    document = yaml.safe_load((ROOT / "tests" / "e2e" / "platform.yaml").read_text(encoding="utf-8"))
    document["vault"]["address"] = VAULT_ADDRESS
    CONFIG.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    start_vault()
    bring_up()


def tearDownModule() -> None:
    if ENABLED and os.environ.get("NIGHTHAWK_E2E_KEEP") != "1":
        quickstart.teardown(options(), purge=True, confirmed=True, out=lambda line: None)
        subprocess.run([options().container, "rm", "--force", VAULT_CONTAINER], capture_output=True, timeout=60)


class StackTests(unittest.TestCase):
    run_id = f"e2e{int(time.time())}"
    sent: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.sent = emit("acme", "web", cls.run_id)

    def test_00_provisioning_again_straight_after_the_first_start_changes_nothing(self) -> None:
        # Grafana makes an organization's first data source its default by itself; the first apply
        # must already have put that right.
        again = compose("run", "--rm", "--no-deps", "grafana-init").stdout
        self.assertEqual(again.strip().splitlines()[-1], "no changes", again)

    # ---- signals through the collector -------------------------------------------------

    def metrics(self) -> list[dict]:
        status, body = call(
            "/metrics/prometheus/api/v1/query", "acme-web-query",
            params={"query": f'{fixtures.METRIC}{{run_id="{self.run_id}"}}'},
        )
        self.assertEqual(status, 200, body)
        return json.loads(body)["data"]["result"]

    def trace(self) -> str:
        status, body = call(f"/traces/api/traces/{self.sent['trace_id']}", "acme-web-query")
        return body if status == 200 else ""

    def profile(self) -> dict:
        now = int(time.time())
        request = {
            "start": (now - 3600) * 1000, "end": (now + 60) * 1000, "profileTypeID": PROFILE_TYPE,
            "labelSelector": f'{{run_id="{self.run_id}"}}',
        }
        status, body = call(
            "/profiles/querier.v1.QuerierService/SelectMergeStacktraces", "acme-web-query", method="POST",
            headers={"Content-Type": "application/json"}, data=json.dumps(request).encode("utf-8"),
        )
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_01_all_four_signals_round_trip_through_the_collector(self) -> None:
        self.assertEqual(self.sent["signals"], ["metrics", "logs", "traces", "profiles"])
        series = eventually(self.metrics)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["value"][1], "42")
        lines = eventually(lambda: query_logs("acme-web-query", f"fixture {self.run_id}")[1])
        self.assertEqual(len(lines), 4)
        trace = eventually(self.trace)
        for name in fixtures.SPAN_NAMES:
            self.assertIn(name, trace)
        self.assertIn("fixture_work", eventually(lambda: self.profile().get("flamegraph", {}).get("names", [])))
        # Another run identifier returns nothing.
        self.assertEqual(query_logs("acme-web-query", "e2e0000000000")[1], [])

    def test_02_sensitive_markers_are_absent_and_free_text_is_retained(self) -> None:
        eventually(lambda: len(query_logs("acme-web-query", f"fixture {self.run_id}")[1]) == 4)
        eventually(self.trace)
        status, labels = call(
            "/metrics/prometheus/api/v1/series", "acme-web-query",
            params={"match[]": f'{fixtures.METRIC}{{run_id="{self.run_id}"}}'},
        )
        self.assertEqual(status, 200)
        # The fixture series itself came back, so the absence of markers below means something.
        (series,) = json.loads(labels)["data"]
        self.assertEqual((series["__name__"], series["run_id"]), (fixtures.METRIC, self.run_id))
        # Resource attributes become labels of target_info; the resource markers must not be among them.
        status, resource_labels = eventually(lambda: (lambda result: result if fixtures.SERVICE in result[1] else None)(
            call("/metrics/prometheus/api/v1/series", "acme-web-query", params={"match[]": "target_info"})
        ))
        self.assertEqual(status, 200)
        self.assertIn(fixtures.SERVICE, resource_labels)
        status, streams = call(
            "/logs/loki/api/v1/query_range", "acme-web-query",
            params={"query": f'{{service_name="{fixtures.SERVICE}"}} |= "{self.run_id}"'},
        )
        self.assertEqual(status, 200)
        status, profile_labels = call(
            "/profiles/querier.v1.QuerierService/Series", "acme-web-query", method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "matchers": [f'{{run_id="{self.run_id}"}}'], "labelNames": [],
                "start": (int(time.time()) - 3600) * 1000, "end": (int(time.time()) + 60) * 1000,
            }).encode(),
        )
        self.assertEqual(status, 200, profile_labels)
        self.assertIn(self.run_id, profile_labels)
        everything = "\n".join([labels, resource_labels, streams, self.trace(), profile_labels])
        self.assertIn(self.run_id, everything)
        for position, by_field in self.sent["markers"].items():
            for field, value in by_field.items():
                self.assertNotIn(value, everything, f"{position}/{field} survived redaction")
        self.assertIn("[REDACTED]", streams)
        # Documented limit: a value with no key pattern is not recognized.
        self.assertIn(self.sent["free_text_marker"], streams)

    # ---- tenant isolation at the gateway -----------------------------------------------

    def test_03_each_credential_reads_only_its_own_backend(self) -> None:
        """All four pairs write every signal they enable; every reader then tries every writer's data."""
        platform = load_platform(CONFIG)
        streams = {f"{stream.tenant}-{stream.datastream}": stream for stream in platform.streams}
        self.assertEqual(sorted(streams), ["acme-batch", "acme-web", "globex-edge", "globex-web"])
        # globex/edge ingests only with its certificate.
        issued = SECRETS / "runtime" / "e2e-certificates-isolation"
        if not (issued / "globex-edge-ingest.crt.pem").exists():
            trust.issue_certificate(platform, issued, 2, vault.connect(platform), credential_id="globex-edge-ingest")
        certificates = {"globex-edge": issued / "globex-edge-ingest.crt.pem"}
        runs: dict[str, str] = {}
        for index, (pair, stream) in enumerate(sorted(streams.items())):
            runs[pair] = f"iso{index}{self.run_id}"

            def post(url: str, headers: dict[str, str], body: bytes, pair: str = pair) -> int:
                return call(url, f"{pair}-ingest", method="POST", headers=headers, data=body,
                            certificate=certificates.get(pair))[0]

            # Straight through the gateway: OTLP at /v1/<signal>, profiles at /profiles/ingest.
            sent = fixtures.emit(stream, runs[pair], "", "/profiles", post=post)
            self.assertEqual(sorted(sent["signals"]), sorted(stream.signals), pair)

        def read(reader: str, signal: str, run: str) -> tuple[int, bool]:
            credential = f"{reader}-query"
            if signal == "metrics":
                status, body = call("/metrics/prometheus/api/v1/query", credential,
                                    params={"query": f'{fixtures.METRIC}{{run_id="{run}"}}'})
                return status, status == 200 and bool(json.loads(body)["data"]["result"])
            if signal == "logs":
                status, lines = query_logs(credential, run)
                return status, bool(lines)
            if signal == "traces":
                status, body = call(f"/traces/api/traces/{fixtures.trace_id(run)}", credential)
                return status, status == 200 and "fixture-parent" in body
            now = int(time.time())
            status, body = call(
                "/profiles/querier.v1.QuerierService/Series", credential, method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps({
                    "matchers": [f'{{run_id="{run}"}}'], "labelNames": [],
                    "start": (now - 3600) * 1000, "end": (now + 60) * 1000,
                }).encode(),
            )
            return status, status == 200 and run in body

        # First every owner sees its own data, so a later miss is not just ingestion delay.
        for pair, stream in sorted(streams.items()):
            for signal in sorted(stream.signals):
                with self.subTest(owner=pair, signal=signal):
                    self.assertTrue(eventually(lambda: read(pair, signal, runs[pair])[1], timeout=180))
        checked = 0
        for reader, reader_stream in sorted(streams.items()):
            for writer, writer_stream in sorted(streams.items()):
                if reader == writer:
                    continue
                for signal in sorted(writer_stream.signals):
                    with self.subTest(reader=reader, writer=writer, signal=signal):
                        status, found = read(reader, signal, runs[writer])
                        self.assertFalse(found, "another datastream's data was returned")
                        if signal not in reader_stream.signals:
                            self.assertEqual(status, 403)
                        checked += 1
        # 4 readers x 3 other writers, over each writer's enabled signals (4 + 2 + 4 + 4).
        self.assertEqual(checked, 3 * (4 + 2 + 4 + 4))

    def test_04_permissions_and_tenant_headers_are_enforced(self) -> None:
        self.assertEqual(query_logs("acme-web-ingest", "x")[0], 403)
        self.assertEqual(push_log("acme-web-query", "x"), 403)
        self.assertEqual(call("/logs/loki/api/v1/labels")[0], 401)
        self.assertEqual(call("/logs/loki/api/v1/labels", "acme-web-query", password="wrong" * 8)[0], 401)
        self.assertEqual(call("/logs/loki/api/v1/labels", user="nobody", password="wrong" * 8)[0], 401)
        self.assertEqual(call("/logs/loki/api/v1/labels", "acme-web-query")[0], 200)
        for header in ("globex-web", "acme-web|globex-web", "acme-batch"):
            with self.subTest(header=header):
                status, _ = call("/logs/loki/api/v1/labels", "acme-web-query", headers={"X-Scope-OrgID": header})
                self.assertEqual(status, 403)
        self.assertEqual(call("/logs/loki/api/v1/labels", "acme-web-query", headers={"X-Scope-OrgID": "acme-web"})[0], 200)
        # A spoofed header on a write must not land in the other tenant.
        needle = f"spoof-{self.run_id}"
        self.assertEqual(push_log("acme-web-ingest", needle, headers={"X-Scope-OrgID": "globex-web"}), 403)
        self.assertEqual(query_logs("globex-web-query", needle)[1], [])

    def test_05_disabled_signals_and_unrouted_paths_are_refused(self) -> None:
        # acme/batch enables only metrics and logs.
        self.assertEqual(call("/traces/api/search", "acme-batch-query")[0], 403)
        self.assertEqual(call("/v1/traces", "acme-batch-ingest", method="POST", data=b"{}",
                              headers={"Content-Type": "application/json"})[0], 403)
        for path in ("/logs/loki/api/v1/delete", "/metrics/prometheus/config/v1/rules", "/logs/ready", "/metrics/config",
                     "/traces/api/overrides", "/"):
            with self.subTest(path=path):
                self.assertEqual(call(path, "acme-web-query")[0], 404)

    def test_06_certificate_bound_credential_needs_its_certificate(self) -> None:
        issued = SECRETS / "runtime" / "e2e-certificates"
        if not (issued / "globex-edge-ingest.crt.pem").exists():
            platform = load_platform(CONFIG)
            trust.issue_certificate(platform, issued, 2, vault.connect(platform), credential_id="globex-edge-ingest")
        certificate = issued / "globex-edge-ingest.crt.pem"
        self.assertEqual(push_log("globex-edge-ingest", "with-certificate", certificate=certificate), 204)
        self.assertEqual(push_log("globex-edge-ingest", "without-certificate"), 403)
        # A forged forwarded-certificate header without a TLS client certificate must be ignored.
        der = base64.b64encode(
            x509.load_pem_x509_certificate(certificate.read_bytes()).public_bytes(serialization.Encoding.DER)
        ).decode("ascii")
        for name in ("X-Forwarded-Tls-Client-Cert", "X_Forwarded_Tls_Client_Cert", "x-forwarded-tls-client-cert"):
            with self.subTest(header=name):
                self.assertEqual(push_log("globex-edge-ingest", "forged", headers={name: der}), 403)
        # A certificate for one identity does not unlock another certificate-free credential's tenant.
        self.assertEqual(push_log("globex-edge-ingest", "x", certificate=certificate, headers={"X-Scope-OrgID": "acme-web"}), 403)

    def test_07_revoked_certificate_is_refused_after_a_policy_reload(self) -> None:
        self.test_06_certificate_bound_credential_needs_its_certificate()
        issued = SECRETS / "runtime" / "e2e-certificates"
        second = SECRETS / "runtime" / "e2e-certificates-second"
        if not (second / "globex-edge-ingest.crt.pem").exists():
            platform = load_platform(CONFIG)
            trust.issue_certificate(platform, second, 2, vault.connect(platform), credential_id="globex-edge-ingest")
        # Revoked in Vault, and enforced at the gateway from the fingerprint that command reports.
        platform = load_platform(CONFIG)
        fingerprint = trust.revoke_certificate(vault.connect(platform), issued / "globex-edge-ingest.crt.pem")
        document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        document["gateway"]["revoked_certificate_fingerprints"] = [fingerprint]
        CONFIG.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        bring_up()
        revoked = lambda: push_log("globex-edge-ingest", "revoked", certificate=issued / "globex-edge-ingest.crt.pem")
        self.assertEqual(eventually(lambda: revoked() == 403 and 403, timeout=30), 403)
        self.assertEqual(push_log("globex-edge-ingest", "sibling", certificate=second / "globex-edge-ingest.crt.pem"), 204)

    def test_08_otlp_grpc_trace_export_through_the_gateway(self) -> None:
        trace_id = hashlib.sha256(f"grpc-{self.run_id}".encode()).digest()[:16]
        now = time.time_ns()
        span = (
            fixtures._field(1, trace_id) + fixtures._field(2, trace_id[:8]) + fixtures._field(5, b"grpc-fixture")
            + fixtures._field(6, 1) + b"\x39" + (now - 1_000_000).to_bytes(8, "little") + b"\x41" + now.to_bytes(8, "little")
        )
        attribute = fixtures._field(1, b"service.name") + fixtures._field(2, fixtures._field(1, b"grpc-fixture"))
        resource_spans = fixtures._field(1, fixtures._field(1, attribute)) + fixtures._field(2, fixtures._field(2, span))
        message = fixtures._field(1, resource_spans)
        frame = b"\x00" + len(message).to_bytes(4, "big") + message
        path = "/opentelemetry.proto.collector.trace.v1.TraceService/Export"
        headers = {"Content-Type": "application/grpc", "TE": "trailers"}
        status, body = call(path, "acme-web-ingest", method="POST", headers=headers, data=frame, http2=True, include_headers=True)
        self.assertEqual(status, 200, body)
        self.assertIn("grpc-status: 0", body.lower())
        found = eventually(lambda: call(f"/traces/api/traces/{trace_id.hex()}", "acme-web-query")[0] == 200)
        self.assertTrue(found)
        # Denials on the gRPC path are plain HTTP statuses.
        self.assertEqual(call(path, "acme-web-query", method="POST", headers=headers, data=frame, http2=True)[0], 403)
        self.assertEqual(call(path, None, method="POST", headers=headers, data=frame, http2=True)[0], 401)

    # ---- backends and exposure ---------------------------------------------------------

    def test_09_backends_require_a_tenant_and_report_the_rendered_overrides(self) -> None:
        platform = load_platform(CONFIG)
        tenants = [stream.backend_id for stream in platform.streams]
        output = in_network(
            "import urllib.request, urllib.error, json\n"
            "def get(url, tenant=None, body=None):\n"
            "    headers = {'X-Scope-OrgID': tenant} if tenant else {}\n"
            "    if body is not None:\n"
            "        headers['Content-Type'] = 'application/json'\n"
            "    request = urllib.request.Request(url, headers=headers, data=body)\n"
            "    try:\n"
            "        with urllib.request.urlopen(request, timeout=10) as response:\n"
            "            return response.status, response.read().decode()\n"
            "    except urllib.error.HTTPError as error:\n"
            "        return error.code, error.read().decode()\n"
            "import time\n"
            "profiles = 'http://pyroscope:4040/querier.v1.QuerierService/LabelNames'\n"
            "window = json.dumps({'start': int(time.time() - 3600) * 1000, 'end': int(time.time()) * 1000}).encode()\n"
            "result = {\n"
            " 'no_tenant': {'mimir': get('http://mimir:8080/prometheus/api/v1/labels')[0],\n"
            "               'loki': get('http://loki:3100/loki/api/v1/labels')[0],\n"
            "               'tempo': get('http://tempo:3200/api/search')[0],\n"
            "               'pyroscope': get(profiles, body=window)[0]},\n"
            " 'with_tenant': {'pyroscope': get(profiles, 'acme-web', window)[0]},\n"
            " 'mimir': get('http://mimir:8080/runtime_config')[1],\n"
            " 'pyroscope': get('http://pyroscope:4040/runtime_config')[1],\n"
            f" 'tempo': {{tenant: get('http://tempo:3200/status/overrides/' + tenant)[1] for tenant in {tenants!r}}},\n"
            "}\n"
            "print(json.dumps(result))\n"
        )
        result = json.loads(output.strip().splitlines()[-1])
        # Addressed directly without a tenant, every backend refuses; with one, Pyroscope answers.
        self.assertEqual(result["no_tenant"], {"mimir": 401, "loki": 401, "tempo": 401, "pyroscope": 401})
        self.assertEqual(result["with_tenant"], {"pyroscope": 200})
        mimir = yaml.safe_load(result["mimir"])["overrides"]
        pyroscope = yaml.safe_load(result["pyroscope"])["overrides"]
        with_signal = lambda signal: sorted(stream.backend_id for stream in platform.streams if signal in stream.signals)
        self.assertEqual(sorted(mimir), with_signal("metrics"))
        self.assertEqual(sorted(pyroscope), with_signal("profiles"))
        # Every pair's own retention, per signal, as each backend loaded it. Loki has no endpoint
        # that reports loaded overrides, so its value is not observable here.
        checked = 0
        for stream in platform.streams:
            wanted = {signal: policy.retention_hours for signal, policy in stream.signals.items()}
            with self.subTest(pair=stream.backend_id):
                if "metrics" in wanted:
                    self.assertEqual(hours(mimir[stream.backend_id]["compactor_blocks_retention_period"]), wanted["metrics"])
                    checked += 1
                if "profiles" in wanted:
                    self.assertEqual(hours(pyroscope[stream.backend_id]["retention_period"]), wanted["profiles"])
                    checked += 1
                if "traces" in wanted:
                    (reported,) = re.findall(r"block_retention:\s*(\S+)", result["tempo"][stream.backend_id])
                    self.assertEqual(hours(reported), wanted["traces"])
                    checked += 1
        self.assertEqual(checked, 4 + 3 + 3)
        # acme/web declares a different retention for each signal, and each backend has its own.
        web = {signal: policy.retention_hours for signal, policy in platform.streams[1].signals.items()}
        self.assertEqual(platform.streams[1].backend_id, "acme-web")
        self.assertEqual(len(set(web.values())), 4)
        self.assertEqual(mimir["acme-web"]["ingestion_rate"], 10000)
        self.assertIn("burst_size_bytes: 1048576", result["tempo"]["globex-edge"])

    def published(self) -> list[tuple[str, str, int]]:
        output = compose("ps", "--format", "json").stdout
        published = []
        for line in output.splitlines():
            entries = json.loads(line)
            for entry in entries if isinstance(entries, list) else [entries]:
                for port in entry.get("Publishers") or []:
                    if port.get("PublishedPort"):
                        published.append((entry["Service"], port.get("URL"), port["PublishedPort"]))
        return sorted(published)

    def test_10_only_the_gateway_is_published_and_on_loopback(self) -> None:
        # The external entry point is selected in the document and still not published: only
        # the override a deployment for another machine adds can publish it.
        self.assertEqual(self.published(), [("traefik", "127.0.0.1", PORT)])

    def test_11_grafana_is_provisioned_reaches_data_through_the_gateway_and_reconciles_idempotently(self) -> None:
        platform = load_platform(CONFIG)
        ui = platform.grafana.hostname
        status, body = call("/api/health", host=ui)
        self.assertEqual(status, 200, body)
        admin = secret(platform.grafana.admin_secret_ref)
        status, body = call("/api/orgs", host=ui, user="admin", password=admin)
        self.assertEqual(status, 200, body)
        organizations = {item["name"]: item["id"] for item in json.loads(body)}
        self.assertLessEqual({"acme", "globex"}, set(organizations))
        self.assertEqual(call("/api/orgs", host=ui)[0], 401)
        # Grafana -> gateway -> Loki, with the organization's own data source.
        eventually(lambda: len(query_logs("acme-web-query", f"fixture {self.run_id}")[1]) == 4)
        uid = datasource_uid("acme-web", "logs")
        status, body = call(
            f"/api/datasources/proxy/uid/{uid}/loki/api/v1/query_range", host=ui, user="admin", password=admin,
            headers={"X-Grafana-Org-Id": str(organizations["acme"])},
            params={"query": f'{{service_name="{fixtures.SERVICE}"}} |= "{self.run_id}"'},
        )
        if status != 200:
            body += "\n--- grafana log ---\n" + compose("logs", "--tail", "60", "grafana", check=False).stdout[-6000:]
        self.assertEqual(status, 200, body)
        self.assertIn(self.run_id, body)
        # The other organization does not have that data source.
        status, _ = call(
            f"/api/datasources/uid/{uid}", host=ui, user="admin", password=admin,
            headers={"X-Grafana-Org-Id": str(organizations["globex"])},
        )
        self.assertEqual(status, 404)
        # The UI host name never reaches a signal backend.
        status, body = call("/metrics/prometheus/api/v1/labels", "acme-web-query", host=ui)
        self.assertNotIn('"status":"success"', body)
        self.assertNotEqual(status, 200)
        second = compose("run", "--rm", "--no-deps", "grafana-init").stdout
        self.assertEqual(second.strip().splitlines()[-1], "no changes")

    def test_11b_every_correlation_targets_a_data_source_of_the_same_organization(self) -> None:
        platform = load_platform(CONFIG)
        ui = platform.grafana.hostname
        admin = secret(platform.grafana.admin_secret_ref)
        organizations = {
            item["name"]: item["id"]
            for item in json.loads(call("/api/orgs", host=ui, user="admin", password=admin)[1])
        }

        def targets(value: object) -> list[str]:
            if isinstance(value, dict):
                return [item for key, child in value.items() for item in (
                    [child] if key == "datasourceUid" else targets(child)
                )]
            if isinstance(value, list):
                return [item for child in value for item in targets(child)]
            return []

        links = 0
        for tenant in ("acme", "globex"):
            header = {"X-Grafana-Org-Id": str(organizations[tenant])}
            status, body = call("/api/datasources", host=ui, user="admin", password=admin, headers=header)
            self.assertEqual(status, 200, body)
            sources = json.loads(body)
            own = {source["uid"] for source in sources}
            for source in sources:
                # The list omits nothing we need, but read each one as Grafana stores it.
                status, body = call(f"/api/datasources/uid/{source['uid']}", host=ui, user="admin", password=admin, headers=header)
                self.assertEqual(status, 200, body)
                for target in targets(json.loads(body).get("jsonData") or {}):
                    with self.subTest(tenant=tenant, source=source["name"], target=target):
                        self.assertIn(target, own)
                        links += 1
        # acme/web and both globex streams: metrics->traces 1, logs->traces 1, traces->logs, metrics x2, profiles = 6 each.
        # acme/batch has metrics and logs only, so it has no trace links.
        self.assertEqual(links, 3 * 6)
        # A link never crosses datastreams: the trace link of acme/web logs is acme/web traces.
        status, body = call(
            f"/api/datasources/uid/{datasource_uid('acme-web', 'logs')}", host=ui, user="admin", password=admin,
            headers={"X-Grafana-Org-Id": str(organizations["acme"])},
        )
        self.assertEqual(json.loads(body)["jsonData"]["derivedFields"][0]["datasourceUid"], datasource_uid("acme-web", "traces"))

    # ---- changes and restarts (last: they disturb the stack) ----------------------------

    def test_12_changed_override_is_picked_up_without_a_restart(self) -> None:
        document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        document["tenants"][0]["datastreams"][0]["signals"]["metrics"]["ingestion_rate_samples_per_second"] = 12345
        CONFIG.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        started = compose("ps", "--format", "{{.Service}} {{.CreatedAt}}").stdout
        bring_up()
        code = (
            "import urllib.request, time\n"
            "deadline = time.time() + 60\n"
            "while True:\n"
            "    text = urllib.request.urlopen('http://mimir:8080/runtime_config', timeout=10).read().decode()\n"
            "    if 'ingestion_rate: 12345' in text or time.time() > deadline:\n"
            "        break\n"
            "    time.sleep(3)\n"
            "print('FOUND' if 'ingestion_rate: 12345' in text else 'MISSING')\n"
        )
        self.assertEqual(in_network(code).strip().splitlines()[-1], "FOUND")
        self.assertEqual(compose("ps", "--format", "{{.Service}} {{.CreatedAt}}").stdout, started)

    def test_13_data_survives_a_backend_restart_and_a_full_stop_and_start(self) -> None:
        eventually(lambda: len(query_logs("acme-web-query", f"fixture {self.run_id}")[1]) == 4)
        compose("restart", "loki")
        lines = eventually(lambda: query_logs("acme-web-query", f"fixture {self.run_id}")[1], timeout=180)
        self.assertEqual(len(lines), 4)
        compose("stop", timeout=300)
        bring_up()
        self.assertEqual(len(eventually(lambda: query_logs("acme-web-query", f"fixture {self.run_id}")[1], timeout=180)), 4)
        self.assertEqual(len(eventually(self.metrics, timeout=180)), 1)
        self.assertIn("fixture-parent", eventually(self.trace, timeout=180))
        self.assertIn("fixture_work", eventually(lambda: self.profile().get("flamegraph", {}).get("names", []), timeout=180))

    def test_14_each_backend_keeps_its_data_across_its_own_restart(self) -> None:
        def tolerant(check):
            # While a backend restarts, the query helpers assert on its error replies; that is "not yet".
            def attempt() -> bool:
                try:
                    return bool(check())
                except AssertionError:
                    return False
            return attempt

        checks = {
            "mimir": tolerant(lambda: len(self.metrics()) == 1),
            "tempo": tolerant(lambda: "fixture-parent" in self.trace()),
            "pyroscope": tolerant(lambda: "fixture_work" in self.profile().get("flamegraph", {}).get("names", [])),
        }
        for backend, check in checks.items():
            with self.subTest(backend=backend):
                self.assertTrue(eventually(check, timeout=180), "fixture missing before the restart")
                compose("restart", backend)
                self.assertTrue(eventually(check, timeout=240), "fixture missing after the restart")

    # ---- credential rotation, as the runbook in docs/05-gateway.md describes it ----------

    def grafana_query(self) -> int:
        """A Loki query through Grafana's acme/web data source: Grafana -> gateway -> Loki."""
        platform = load_platform(CONFIG)
        ui, admin = platform.grafana.hostname, secret(platform.grafana.admin_secret_ref)
        organizations = {item["name"]: item["id"] for item in json.loads(call("/api/orgs", host=ui, user="admin", password=admin)[1])}
        status, _ = call(
            f"/api/datasources/proxy/uid/{datasource_uid('acme-web', 'logs')}/loki/api/v1/labels", host=ui,
            user="admin", password=admin, headers={"X-Grafana-Org-Id": str(organizations["acme"])},
        )
        return status

    def datasource_user(self) -> str:
        platform = load_platform(CONFIG)
        ui, admin = platform.grafana.hostname, secret(platform.grafana.admin_secret_ref)
        organizations = {item["name"]: item["id"] for item in json.loads(call("/api/orgs", host=ui, user="admin", password=admin)[1])}
        _, body = call(
            f"/api/datasources/uid/{datasource_uid('acme-web', 'logs')}", host=ui, user="admin", password=admin,
            headers={"X-Grafana-Org-Id": str(organizations["acme"])},
        )
        return json.loads(body)["basicAuthUser"]

    def declare(self, credential_id: str, permission: str) -> None:
        document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        document["secrets"][credential_id] = {"path": "nighthawk/e2e", "key": credential_id}
        document["credentials"].append({
            "id": credential_id, "secret_ref": credential_id, "tenant": "acme", "datastream": "web",
            "permission": permission,
        })
        CONFIG.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def retire(self, credential_id: str) -> None:
        document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        document["credentials"] = [item for item in document["credentials"] if item["id"] != credential_id]
        del document["secrets"][credential_id]
        CONFIG.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def test_15_ingestion_credential_rotates_without_a_gap(self) -> None:
        self.declare("acme-web-ingest-2", "ingest")
        # Two ingestion credentials and no choice: refused before anything changes.
        with self.assertRaisesRegex(Exception, "several ingestion credentials qualify"):
            bring_up()
        self.assertEqual(push_log("acme-web-ingest", "before-switch"), 204)
        bring_up("acme-web-ingest-2")
        collector = (WORK / "rendered" / "collector" / "datastream.alloy").read_text(encoding="utf-8")
        self.assertIn("credential acme-web-ingest-2", collector)
        # Both are accepted during the overlap.
        self.assertEqual(eventually(lambda: push_log("acme-web-ingest-2", "overlap-new") == 204 and 204, timeout=30), 204)
        self.assertEqual(push_log("acme-web-ingest", "overlap-old"), 204)
        # The collector delivers with the new credential.
        run = f"rot{self.run_id}"
        emit("acme", "web", run)
        self.assertEqual(len(eventually(lambda: query_logs("acme-web-query", f"fixture {run}")[1], timeout=120)), 4)
        retired_secret = secret("acme-web-ingest")
        self.retire("acme-web-ingest")
        bring_up()
        retired = lambda: push_log(None, "retired", user="acme-web-ingest", password=retired_secret)
        self.assertEqual(eventually(lambda: retired() == 401 and 401, timeout=30), 401)
        self.assertEqual(push_log("acme-web-ingest-2", "after-retire"), 204)
        run = f"rtd{self.run_id}"
        emit("acme", "web", run)
        self.assertEqual(len(eventually(lambda: query_logs("acme-web-query", f"fixture {run}")[1], timeout=120)), 4)

    def test_16_query_credential_rotates_without_a_gap_and_in_place(self) -> None:
        self.assertEqual(self.grafana_query(), 200)
        self.declare("acme-web-query-2", "query")
        with self.assertRaisesRegex(Exception, "several query credentials are declared"):
            bring_up()
        self.assertEqual(self.grafana_query(), 200)
        bring_up("acme-web-query-2")
        self.assertEqual(self.datasource_user(), "acme-web-query-2")
        self.assertEqual(self.grafana_query(), 200)
        self.assertEqual(call("/logs/loki/api/v1/labels", "acme-web-query")[0], 200)
        retired_secret = secret("acme-web-query")
        self.retire("acme-web-query")
        bring_up()
        self.assertEqual(self.grafana_query(), 200)
        self.assertEqual(self.datasource_user(), "acme-web-query-2")
        # The retired credential's real secret is refused, not merely a wrong one.
        retired = lambda: call("/logs/loki/api/v1/labels", user="acme-web-query", password=retired_secret)[0]
        self.assertEqual(eventually(lambda: retired() == 401 and 401, timeout=30), 401)
        # In place: a new value under the same credential. Grafana must be sent the new password.
        old = secret("acme-web-query-2")
        platform = load_platform(CONFIG)
        new = "rotated-in-place-" + hashlib.sha256(self.run_id.encode()).hexdigest()
        secrets.rotate_secret(vault.connect(platform), platform, "acme-web-query-2", new)
        bring_up()
        self.assertEqual(secret("acme-web-query-2"), new)
        self.assertEqual(eventually(lambda: self.grafana_query() == 200 and 200, timeout=60), 200)
        self.assertEqual(call("/logs/loki/api/v1/labels", user="acme-web-query-2", password=old)[0], 401)
        # And an unchanged re-run does not touch Grafana again.
        again = compose("run", "--rm", "--no-deps", "grafana-init").stdout
        self.assertEqual(again.strip().splitlines()[-1], "no changes")

    def test_17_external_entry_point_is_published_by_the_override_and_requires_a_client_certificate(self) -> None:
        def restore() -> None:
            EXTERNAL.clear()
            bring_up()

        self.addCleanup(restore)
        EXTERNAL.update(external_bind_address=EXTERNAL_ADDRESS, external_published_port=EXTERNAL_PORT)
        bring_up()
        self.assertEqual(
            self.published(), [("traefik", "127.0.0.1", PORT), ("traefik", EXTERNAL_ADDRESS, EXTERNAL_PORT)],
        )
        platform = load_platform(CONFIG)
        external = quickstart.external_entry_point(platform)
        # Issued the way a playbook does it, so a second request is not sent.
        issued = SECRETS / "runtime" / "e2e-external-certificates"
        client = vault.connect(platform)
        first = trust.ensure_certificate(platform, issued, 2, client, 1, credential_id="globex-edge-ingest")
        self.assertIsNotNone(first)
        self.assertIsNone(trust.ensure_certificate(platform, issued, 2, client, 1, credential_id="globex-edge-ingest"))
        certificate = issued / "globex-edge-ingest.crt.pem"
        there = {"address": EXTERNAL_ADDRESS, "port": external.port, "published": EXTERNAL_PORT}
        ingest = next(
            item.id for item in platform.credentials
            if (item.tenant, item.datastream, item.permission) == ("acme", "web", "ingest")
        )

        def accepted() -> int:
            return push_log("globex-edge-ingest", "external-with-certificate", certificate=certificate, **there)

        # With its certificate the credential delivers through the external entry point.
        self.assertEqual(eventually(lambda: accepted() == 204 and 204, timeout=60), 204)
        # Without one it is refused there, whatever the credential.
        self.assertEqual(push_log("globex-edge-ingest", "external-without-certificate", **there), 403)
        self.assertEqual(push_log(ingest, "external-certificate-free-credential", **there), 403)
        # The same certificate-free credential still delivers on the loopback entry point.
        self.assertEqual(push_log(ingest, "loopback-certificate-free-credential"), 204)
        # And what arrived through the external entry point landed in its own datastream only.
        self.assertTrue(eventually(lambda: query_logs("globex-edge-query", "external-with-certificate")[1], timeout=60))
        own = next(
            item.id for item in platform.credentials
            if (item.tenant, item.datastream, item.permission) == ("acme", "web", "query")
        )
        self.assertEqual(query_logs(own, "external-with-certificate")[1], [])


if __name__ == "__main__":
    unittest.main()
