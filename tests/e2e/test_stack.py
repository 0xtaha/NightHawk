"""End-to-end checks against the running Compose stack.

Skipped unless NIGHTHAWK_E2E=1. They start their own stack (project `nighthawk-e2e`) from
tests/e2e/platform.yaml, so the default stack must be stopped first: both publish 8443.
Set NIGHTHAWK_E2E_KEEP=1 to leave the stack running afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from nighthawk import fixtures, quickstart, trust
from nighthawk.authz import certificate_fingerprint
from nighthawk.config import ROOT, load_platform
from nighthawk.grafana import datasource_uid

ENABLED = os.environ.get("NIGHTHAWK_E2E") == "1"
WORK = ROOT / ".generated" / "e2e"
CONFIG = WORK / "platform.yaml"
SECRETS = ROOT / ".materialized-secrets-e2e"
PORT = 8443
PROFILE_TYPE = fixtures.PROFILE_TYPE


def options() -> quickstart.Options:
    compose = tuple(os.environ.get("NIGHTHAWK_COMPOSE", "docker compose").split())
    return quickstart.Options(
        config=CONFIG, compose=compose, container=compose[0], rendered_dir=WORK / "rendered",
        secrets_dir=SECRETS, project="nighthawk-e2e", build=False, timeout=600, tenant="acme", datastream="web",
    )


def bring_up() -> None:
    quickstart.quickstart(options(), out=lambda line: None)


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
    item = platform.secrets[reference]
    return (SECRETS / item.file / item.key).read_text(encoding="utf-8").rstrip("\r\n")


def call(
    path: str, credential: str | None = None, *, host: str | None = None, method: str = "GET",
    headers: dict[str, str] | None = None, data: bytes | None = None, params: dict[str, str] | None = None,
    certificate: Path | None = None, password: str | None = None, user: str | None = None, http2: bool = False,
    include_headers: bool = False,
) -> tuple[int, str]:
    platform = load_platform(CONFIG)
    host = host or platform.gateway.hostname
    command = [
        "curl", "--silent", "--max-time", "30", "--cacert", str(SECRETS / "runtime" / "grafana" / "gateway-ca.pem"),
        "--resolve", f"{host}:{PORT}:127.0.0.1", "--request", method, "--write-out", "\n%{http_code}",
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
    result = subprocess.run(command + [f"https://{host}:{PORT}{path}"], input=data, capture_output=True, timeout=60)
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
    shutil.copyfile(ROOT / "tests" / "e2e" / "platform.yaml", CONFIG)
    bring_up()


def tearDownModule() -> None:
    if ENABLED and os.environ.get("NIGHTHAWK_E2E_KEEP") != "1":
        quickstart.teardown(options(), purge=True, confirmed=True, out=lambda line: None)


class StackTests(unittest.TestCase):
    run_id = f"e2e{int(time.time())}"
    sent: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.sent = emit("acme", "web", cls.run_id)

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
        status, labels = call("/metrics/prometheus/api/v1/series", "acme-web-query", params={"match[]": fixtures.METRIC})
        self.assertEqual(status, 200)
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
        everything = "\n".join([labels, streams, self.trace(), profile_labels])
        self.assertIn(self.run_id, everything)
        for position, by_field in self.sent["markers"].items():
            for field, value in by_field.items():
                self.assertNotIn(value, everything, f"{position}/{field} survived redaction")
        self.assertIn("[REDACTED]", streams)
        # Documented limit: a value with no key pattern is not recognized.
        self.assertIn(self.sent["free_text_marker"], streams)

    # ---- tenant isolation at the gateway -----------------------------------------------

    def test_03_each_credential_reads_only_its_own_backend(self) -> None:
        pairs = ["acme-web", "acme-batch", "globex-web"]
        needles = {pair: f"isolation-{pair}-{self.run_id}" for pair in pairs}
        for pair, needle in needles.items():
            self.assertEqual(push_log(f"{pair}-ingest", needle), 204, pair)
        for reader in pairs + ["globex-edge"]:
            for writer, needle in needles.items():
                with self.subTest(reader=reader, writer=writer):
                    found = eventually(lambda: query_logs(f"{reader}-query", needle)[1]) if reader == writer \
                        else query_logs(f"{reader}-query", needle)[1]
                    self.assertEqual(bool(found), reader == writer)

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
            trust.issue_certificate(
                load_platform(CONFIG), issued, 2, credential_id="globex-edge-ingest", root=ROOT,
                runner=lambda args, **kwargs: subprocess.run(args, env=quickstart._environment(options()), **kwargs),
            )
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
            trust.issue_certificate(
                load_platform(CONFIG), second, 2, credential_id="globex-edge-ingest", root=ROOT,
                runner=lambda args, **kwargs: subprocess.run(args, env=quickstart._environment(options()), **kwargs),
            )
        first = x509.load_pem_x509_certificate((issued / "globex-edge-ingest.crt.pem").read_bytes())
        document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        document["gateway"]["revoked_certificate_fingerprints"] = [certificate_fingerprint(first)]
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
        output = in_network(
            "import urllib.request, urllib.error, json\n"
            "def get(url, tenant=None):\n"
            "    request = urllib.request.Request(url, headers={'X-Scope-OrgID': tenant} if tenant else {})\n"
            "    try:\n"
            "        with urllib.request.urlopen(request, timeout=10) as response:\n"
            "            return response.status, response.read().decode()\n"
            "    except urllib.error.HTTPError as error:\n"
            "        return error.code, error.read().decode()\n"
            "result = {\n"
            " 'no_tenant': [get('http://mimir:8080/prometheus/api/v1/labels')[0], get('http://loki:3100/loki/api/v1/labels')[0],\n"
            "               get('http://tempo:3200/api/search')[0]],\n"
            " 'mimir': get('http://mimir:8080/runtime_config')[1],\n"
            " 'pyroscope': get('http://pyroscope:4040/runtime_config')[1],\n"
            " 'tempo': get('http://tempo:3200/status/overrides/globex-edge')[1],\n"
            " 'loki': get('http://loki:3100/loki/api/v1/drilldown-limits', 'acme-batch')[1],\n"
            "}\n"
            "print(json.dumps(result))\n"
        )
        result = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(result["no_tenant"], [401, 401, 401])
        mimir = yaml.safe_load(result["mimir"])["overrides"]
        self.assertEqual(sorted(mimir), ["acme-batch", "acme-web", "globex-edge", "globex-web"])
        self.assertEqual(mimir["globex-web"]["compactor_blocks_retention_period"], "4d")
        self.assertEqual(mimir["acme-web"]["ingestion_rate"], 10000)
        pyroscope = yaml.safe_load(result["pyroscope"])["overrides"]
        self.assertEqual(sorted(pyroscope), ["acme-web", "globex-edge", "globex-web"])
        self.assertIn("block_retention: 2d", result["tempo"])
        self.assertIn("burst_size_bytes: 1048576", result["tempo"])
        self.__class__.loki_limits = result["loki"]

    def test_10_only_the_gateway_is_published_and_on_loopback(self) -> None:
        output = compose("ps", "--format", "json").stdout
        published = []
        for line in output.splitlines():
            entries = json.loads(line)
            for entry in entries if isinstance(entries, list) else [entries]:
                for port in entry.get("Publishers") or []:
                    if port.get("PublishedPort"):
                        published.append((entry["Service"], port.get("URL"), port["PublishedPort"]))
        self.assertEqual(published, [("traefik", "127.0.0.1", PORT)])

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


if __name__ == "__main__":
    unittest.main()
