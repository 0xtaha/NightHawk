"""Acceptance tests for the development profile of self-hosted Kubernetes, on a local cluster.

Opt-in: set NIGHTHAWK_K8S_E2E=1. The suite creates a single-node minikube cluster under
rootless Podman, runs a dev-mode Vault in it as a fixture, deploys the platform with the
same playbooks an operator uses, and deletes the cluster afterwards. It needs the tools from
`python -m nighthawk fetch-tools`, the Ansible environment of ansible/, minikube, and Podman.

    NIGHTHAWK_K8S_REUSE=1   use the cluster that is already running, and keep it
    NIGHTHAWK_K8S_KEEP=1    keep the cluster afterwards

The cluster runs minikube's Kubernetes, not the pinned k3s, and Calico rather than Cilium:
what is exercised is the platform on Kubernetes, not the cluster of docs/08-ansible.md.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as token_source
import shutil
import subprocess
import time
import unittest
from pathlib import Path

import yaml

from nighthawk import fixtures, trust, vault
from nighthawk.config import ROOT, load_platform
from nighthawk.grafana import datasource_uid

ENABLED = os.environ.get("NIGHTHAWK_K8S_E2E") == "1"
REUSE = os.environ.get("NIGHTHAWK_K8S_REUSE") == "1"
KEEP = REUSE or os.environ.get("NIGHTHAWK_K8S_KEEP") == "1"
PROFILE = "nighthawk-e2e"
WORK = ROOT / ".generated" / "k8s-e2e"
CONFIG = WORK / "platform.yaml"
CONTRACTS = WORK / "contracts"
KUBECONFIG = WORK / "kubeconfig"
TOOLS = ROOT / ".tools"
IMAGE = "localhost/nighthawk/nighthawk:k8s-e2e"
NAMESPACE, GATEWAY_NAMESPACE = "nighthawk", "nighthawk-gateway"
VAULT_PORT, GATEWAY_PORT, CLUSTER_PORT = 8220, 9443, 9444
# A throwaway dev-mode server in the test cluster; this token protects nothing. See vault-dev.yaml.
VAULT_DEV_TOKEN = "nighthawk-k8s-e2e-dev-root"
ENVIRONMENT = {
    **os.environ,
    "MINIKUBE_HOME": str(ROOT / ".generated" / "minikube"),
    "KUBECONFIG": str(KUBECONFIG),
    "NIGHTHAWK_K8S_RENDERED_DIR": str(CONTRACTS / "kubernetes"),
    "NIGHTHAWK_HELM": str(TOOLS / "helm"),
    "NIGHTHAWK_IMAGE": IMAGE,
    "VAULT_TOKEN": VAULT_DEV_TOKEN,
    "PATH": f"{ROOT / 'ansible' / '.venv' / 'bin'}:{TOOLS}:{os.environ.get('PATH', '')}",
}
FORWARDS: list[subprocess.Popen] = []


def run(
    *command: str, check: bool = True, timeout: int = 900, cwd: Path = ROOT, stdin: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(command), capture_output=True, text=True, timeout=timeout, cwd=cwd, input=stdin,
        env={**ENVIRONMENT, **(environment or {})},
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(command[:6])} failed: {(result.stderr or result.stdout)[-1500:]}")
    return result


def kubectl(*args: str, **options) -> subprocess.CompletedProcess:
    return run(str(TOOLS / "kubectl"), *args, **options)


def nighthawk(*args: str, **options) -> subprocess.CompletedProcess:
    return run(str(ROOT / ".venv" / "bin" / "python"), "-m", "nighthawk", *args, **options)


def playbook(name: str, *extra: str, check: bool = True, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # Ansible refuses a non-blocking standard error, so both streams are captured.
    return run(
        "ansible-playbook", "-i", str(ROOT / "tests" / "k8s" / "inventory"), f"playbooks/{name}", *extra,
        cwd=ROOT / "ansible", check=check, timeout=3600, environment=environment,
    )


def recap(result: subprocess.CompletedProcess) -> dict[str, int]:
    line = next(line for line in reversed(result.stdout.splitlines()) if "changed=" in line)
    return {key: int(value) for key, value in (part.split("=") for part in line.split() if "=" in part)}


def forward(namespace: str, target: str, *ports: str) -> None:
    FORWARDS.append(subprocess.Popen(
        [str(TOOLS / "kubectl"), "-n", namespace, "port-forward", target, *ports],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENVIRONMENT,
    ))


def stop_forwards() -> None:
    while FORWARDS:
        process = FORWARDS.pop()
        process.terminate()
        process.wait(timeout=10)


def eventually(check, timeout: float = 120, interval: float = 4):
    deadline = time.time() + timeout
    while True:
        value = check()
        if value or time.time() > deadline:
            return value
        time.sleep(interval)


def secret_value(name: str, key: str = "value", namespace: str = NAMESPACE) -> str:
    """A synchronized secret as the cluster holds it."""
    encoded = kubectl("-n", namespace, "get", "secret", name, "-o", f"jsonpath={{.data.{key.replace('.', chr(92) + '.')}}}").stdout
    return base64.b64decode(encoded).decode("utf-8")


def credential(credential_id: str) -> str:
    return secret_value(f"vault-authz-{credential_id}")


def authority() -> Path:
    path = WORK / "gateway-ca.pem"
    path.write_text(secret_value("gateway-trust", "ca.crt"), encoding="utf-8")
    return path


def call(
    path: str, credential_id: str | None = None, *, port: int = GATEWAY_PORT, host: str | None = None, method: str = "GET",
    headers: dict[str, str] | None = None, data: bytes | None = None, params: dict[str, str] | None = None,
    certificate: Path | None = None, user: str | None = None, password: str | None = None,
) -> tuple[int, str]:
    """One request to the gateway through a forwarded port, by the name its certificate carries."""
    platform = load_platform(CONFIG)
    host = host or platform.gateway.hostname
    command = [
        "curl", "--silent", "--max-time", "30", "--cacert", str(authority()),
        "--resolve", f"{host}:{port}:127.0.0.1", "--request", method, "--write-out", "\n%{http_code}",
    ]
    if credential_id is not None or user is not None:
        command += ["--user", f"{user or credential_id}:{password if password is not None else credential(credential_id or '')}"]
    for key, value in (headers or {}).items():
        command += ["--header", f"{key}: {value}"]
    if certificate is not None:
        command += ["--cert", str(certificate), "--key", str(certificate).replace(".crt.pem", ".key.pem")]
    if params:
        command += ["--get"] if method == "GET" else []
        for key, value in params.items():
            command += ["--data-urlencode", f"{key}={value}"]
    if data is not None:
        command += ["--data-binary", "@-"]
    result = subprocess.run(command + [f"https://{host}:{port}{path}"], input=data, capture_output=True, timeout=60)
    body, _, status = result.stdout.decode("utf-8", "replace").rpartition("\n")
    return int(status or 0), body


def push_log(credential_id: str, line: str, *, port: int = CLUSTER_PORT, **options) -> int:
    body = json.dumps({"streams": [{"stream": {"job": "k8s-e2e"}, "values": [[str(time.time_ns()), line]]}]})
    headers = {"Content-Type": "application/json", **options.pop("headers", {})}
    return call("/logs/loki/api/v1/push", credential_id, port=port, method="POST", headers=headers, data=body.encode(), **options)[0]


def query_logs(credential_id: str, needle: str) -> tuple[int, list[str]]:
    # Pod logs are collected too, and some pods log the requests that carry the needle, so the
    # query is limited to what was pushed: streams without a pod label.
    status, body = call(
        "/logs/loki/api/v1/query_range", credential_id,
        params={"query": f'{{service_name=~".+", pod=""}} |= "{needle}"', "limit": "100"},
    )
    if status != 200:
        return status, []
    return status, [value[1] for stream in json.loads(body)["data"]["result"] for value in stream["values"]]


def pods(namespace: str = NAMESPACE) -> dict[str, str]:
    """Pod name to start time, for the pods that keep running."""
    items = json.loads(kubectl("-n", namespace, "get", "pods", "-o", "json").stdout)["items"]
    return {item["metadata"]["name"]: item["status"].get("startTime", "") for item in items if item["status"].get("phase") == "Running"}


def probe(name: str, namespace: str, labels: str) -> None:
    kubectl("-n", namespace, "delete", "pod", name, "--ignore-not-found", "--wait=true")
    kubectl("-n", namespace, "run", name, "--restart=Never", "--image=docker.io/library/busybox:1.37", f"--labels={labels}", "--", "sleep", "900")
    kubectl("-n", namespace, "wait", "--for=condition=Ready", f"pod/{name}", "--timeout=180s")


def reaches(name: str, namespace: str, host: str, port: int) -> bool:
    return kubectl("-n", namespace, "exec", name, "--", "nc", "-w", "4", "-z", host, str(port), check=False).returncode == 0


def render() -> None:
    shutil.rmtree(CONTRACTS, ignore_errors=True)
    nighthawk(
        "render-contracts", "--config", str(CONFIG), "--output", str(CONTRACTS),
        "--collector-tenant", "acme", "--collector-datastream", "web",
    )


def deploy() -> subprocess.CompletedProcess:
    return playbook("k8s-platform.yml")


def emit(run_id: str) -> dict:
    """Send the fixtures from a pod outside the platform's namespaces, as an application would."""
    manifest = {
        "apiVersion": "v1", "kind": "Pod", "metadata": {"name": f"fixtures-{run_id}", "namespace": "default"},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "fixtures", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                "args": [
                    "emit-fixtures", f"--otlp=http://alloy.{NAMESPACE}.svc:4318", f"--profiles=http://alloy.{NAMESPACE}.svc:4040",
                    "--config=/etc/nighthawk/platform.yaml", "--tenant=acme", "--datastream=web", f"--run-id={run_id}",
                ],
                "volumeMounts": [{"name": "platform", "mountPath": "/etc/nighthawk"}],
            }],
            "volumes": [{"name": "platform", "configMap": {"name": "fixture-platform"}}],
        },
    }
    document = yaml.safe_dump({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "fixture-platform", "namespace": "default"},
        "data": {"platform.yaml": CONFIG.read_text(encoding="utf-8")},
    }) + "---\n" + yaml.safe_dump(manifest)
    kubectl("apply", "-f", "-", stdin=document)
    kubectl("-n", "default", "wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/fixtures-{run_id}", "--timeout=240s")
    return json.loads(kubectl("-n", "default", "logs", f"fixtures-{run_id}").stdout.strip().splitlines()[-1])


def setUpModule() -> None:
    if not ENABLED:
        raise unittest.SkipTest("set NIGHTHAWK_K8S_E2E=1 to create a local cluster and deploy to it")
    WORK.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text((ROOT / "tests" / "k8s" / "platform.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    running = run("minikube", "status", "-p", PROFILE, check=False).returncode == 0
    if not (REUSE and running):
        run("minikube", "delete", "-p", PROFILE, check=False)
        # Rootless Podman needs the containerd runtime. Calico enforces NetworkPolicy here.
        run("minikube", "config", "set", "rootless", "true")
        run(
            "minikube", "start", "-p", PROFILE, "--driver=podman", "--container-runtime=containerd", "--cni=calico",
            "--cpus=4", "--memory=9g", timeout=1800,
        )
        # Podman caps a container at 2048 processes, and this container is the whole cluster.
        run("podman", "update", "--pids-limit", "-1", PROFILE)
    kubectl("-n", "kube-system", "wait", "--for=condition=Ready", "pod", "--all", "--timeout=600s")
    run("podman", "build", "--quiet", "-f", "docker-compose/nighthawk.Dockerfile", "-t", IMAGE, ".")
    archive = WORK / "image.tar"
    archive.unlink(missing_ok=True)
    run("podman", "save", "--quiet", "-o", str(archive), IMAGE)
    run("minikube", "-p", PROFILE, "image", "load", str(archive))
    archive.unlink()
    kubectl("apply", "-f", str(ROOT / "tests" / "k8s" / "vault-dev.yaml"))
    kubectl("-n", "vault-dev", "rollout", "status", "deploy/vault", "--timeout=300s")
    forward("vault-dev", "svc/vault", f"{VAULT_PORT}:8200")
    time.sleep(3)
    render()
    nighthawk(
        "bootstrap-dev-vault", "--config", str(CONFIG), "--confirm-disposable-vault", "--ca-valid-days", "365",
        "--cluster-requirements", str(CONTRACTS / "vault" / "kubernetes-auth.json"),
        "--kubernetes-host", "https://kubernetes.default.svc",
    )
    document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for item in document["credentials"]:
        nighthawk("generate-credential", "--config", str(CONFIG), "--credential", item["id"], check=False)
    for reference in sorted({binding["identity"]["ref"] for binding in document["storage"]["bindings"].values()}):
        nighthawk("generate-storage-identity", "--config", str(CONFIG), "--identity", reference, check=False)
    nighthawk("store-secret", "--config", str(CONFIG), "--secret", "grafana-admin", check=False, stdin=token_source.token_urlsafe(24))
    playbook("k8s-addons.yml")
    deploy()
    forward(GATEWAY_NAMESPACE, "svc/traefik-internal", f"{GATEWAY_PORT}:443", f"{CLUSTER_PORT}:8444")
    time.sleep(3)


def tearDownModule() -> None:
    stop_forwards()
    if ENABLED and not KEEP:
        run("minikube", "delete", "-p", PROFILE, check=False)


class ClusterTests(unittest.TestCase):
    run_id = f"k8s{int(time.time())}"
    sent: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.sent = emit(cls.run_id)

    # ---- deployment ----------------------------------------------------------------------

    def test_00_deploying_again_upgrades_nothing_and_restarts_no_pod(self) -> None:
        before = {namespace: pods(namespace) for namespace in (NAMESPACE, GATEWAY_NAMESPACE)}
        again = deploy()
        self.assertEqual(recap(again)["changed"], 0, again.stdout[-3000:])
        self.assertIn("no changes", again.stdout)
        self.assertIn("single node without high availability", again.stdout)
        self.assertEqual({namespace: pods(namespace) for namespace in before}, before)
        self.assertEqual(recap(playbook("k8s-addons.yml"))["changed"], 0)

    def test_01_all_four_signals_round_trip_through_the_cluster_collector(self) -> None:
        self.assertEqual(self.sent["signals"], ["metrics", "logs", "traces", "profiles"])

        def metrics() -> list:
            status, body = call(
                "/metrics/prometheus/api/v1/query", "acme-web-query", params={"query": f'{fixtures.METRIC}{{run_id="{self.run_id}"}}'},
            )
            return json.loads(body)["data"]["result"] if status == 200 else []

        def profile() -> list:
            now = int(time.time())
            request = {
                "start": (now - 3600) * 1000, "end": (now + 60) * 1000, "profileTypeID": fixtures.PROFILE_TYPE,
                "labelSelector": f'{{run_id="{self.run_id}"}}',
            }
            status, body = call(
                "/profiles/querier.v1.QuerierService/SelectMergeStacktraces", "acme-web-query", method="POST",
                headers={"Content-Type": "application/json"}, data=json.dumps(request).encode(),
            )
            return json.loads(body).get("flamegraph", {}).get("names", []) if status == 200 else []

        series = eventually(metrics)
        self.assertEqual([item["value"][1] for item in series], ["42"])
        self.assertEqual(len(eventually(lambda: query_logs("acme-web-query", f"fixture {self.run_id}")[1])), 4)
        trace = eventually(lambda: (lambda status, body: body if status == 200 else "")(*call(f"/traces/api/traces/{self.sent['trace_id']}", "acme-web-query")))
        for name in fixtures.SPAN_NAMES:
            self.assertIn(name, trace)
        self.assertIn("fixture_work", eventually(profile))

    def test_02_grafana_is_provisioned_and_reads_through_the_gateway(self) -> None:
        platform = load_platform(CONFIG)
        ui, admin = platform.grafana.hostname, secret_value("vault-grafana-grafana-admin", "admin-password")
        status, body = call("/api/orgs", host=ui, user="admin", password=admin)
        self.assertEqual(status, 200, body)
        organizations = {item["name"]: item["id"] for item in json.loads(body)}
        self.assertLessEqual({"acme", "globex"}, set(organizations))
        status, _ = call(
            f"/api/datasources/proxy/uid/{datasource_uid('acme-web', 'logs')}/loki/api/v1/labels", host=ui,
            user="admin", password=admin, headers={"X-Grafana-Org-Id": str(organizations["acme"])},
        )
        self.assertEqual(status, 200)

    def test_03_node_and_cluster_collectors_do_not_duplicate(self) -> None:
        def scraped() -> dict:
            status, body = call("/metrics/prometheus/api/v1/query", "acme-web-query", params={"query": "count(up) by (job)"})
            return {item["metric"]["job"]: item["value"][1] for item in json.loads(body)["data"]["result"]} if status == 200 else {}

        jobs = eventually(lambda: scraped() if "kubelet" in scraped() else {})
        # One node, so exactly one series per node-scoped job: the two collectors do not overlap.
        self.assertEqual((jobs.get("kubelet"), jobs.get("cadvisor")), ("1", "1"), jobs)
        for backend in ("mimir", "loki", "tempo", "pyroscope", "nighthawk-authz", "traefik"):
            self.assertEqual(jobs.get(backend), "1", jobs)
        privileged = json.loads(kubectl("-n", NAMESPACE, "get", "pods", "-o", "json").stdout)["items"]
        for item in privileged:
            for container in item["spec"]["containers"]:
                self.assertFalse((container.get("securityContext") or {}).get("privileged"), item["metadata"]["name"])

    # ---- tenant boundaries ---------------------------------------------------------------

    def test_04_each_datastream_reads_and_writes_only_its_own(self) -> None:
        pairs = ("acme-web", "acme-batch", "globex-web")
        for pair in pairs:
            self.assertEqual(push_log(f"{pair}-ingest", f"own-{pair}-{self.run_id}"), 204, pair)
        for reader in pairs:
            for writer in pairs:
                expected = 1 if reader == writer else 0
                found = eventually(lambda: len(query_logs(f"{reader}-query", f"own-{writer}-{self.run_id}")[1]) == expected, timeout=60)
                self.assertTrue(found, (reader, writer))

    def test_05_spoofed_headers_wrong_permissions_and_disabled_signals_are_refused(self) -> None:
        self.assertEqual(push_log("acme-web-ingest", "spoof", headers={"X-Scope-OrgID": "globex-web"}), 403)
        self.assertEqual(push_log("acme-web-query", "query credential cannot write"), 403)
        self.assertEqual(call("/logs/loki/api/v1/labels", "acme-web-ingest")[0], 403)
        self.assertEqual(call("/logs/loki/api/v1/labels")[0], 401)
        self.assertEqual(call("/logs/loki/api/v1/labels", user="acme-web-query", password="wrong")[0], 401)
        # acme/batch enables only metrics and logs.
        self.assertEqual(call("/logs/loki/api/v1/labels", "acme-batch-query")[0], 200)
        self.assertIn(call("/traces/api/search", "acme-batch-query")[0], (403, 404))

    def test_06_external_entry_point_requires_the_client_certificate(self) -> None:
        platform = load_platform(CONFIG)
        issued = WORK / "client-certificates"
        client = vault.connect(platform, vault.Auth(), ENVIRONMENT)
        trust.ensure_certificate(platform, issued, 2, client, 1, credential_id="globex-edge-ingest")
        certificate = issued / "globex-edge-ingest.crt.pem"
        external = {"port": GATEWAY_PORT}
        self.assertEqual(eventually(lambda: push_log("globex-edge-ingest", "with-certificate", certificate=certificate, **external) == 204 and 204), 204)
        self.assertEqual(push_log("globex-edge-ingest", "without-certificate", **external), 403)
        self.assertEqual(push_log("acme-web-ingest", "certificate-free credential", **external), 403)
        # The in-cluster entry point is for collectors inside the cluster and needs no certificate.
        self.assertEqual(push_log("acme-web-ingest", "in-cluster"), 204)

    # ---- network isolation ---------------------------------------------------------------

    def test_07_only_contract_flows_are_possible(self) -> None:
        probe("intruder", NAMESPACE, "app=intruder")
        for host, port in (("mimir", 8080), ("loki", 3100), ("tempo", 3200), ("pyroscope", 4040), ("seaweedfs", 8333), ("authz", 9180), ("grafana", 3000)):
            self.assertFalse(reaches("intruder", NAMESPACE, f"{host}.{NAMESPACE}.svc.cluster.local", port), host)
        # A pod with the gateway's label reaches what the gateway may, and nothing else.
        probe("as-gateway", GATEWAY_NAMESPACE, "nighthawk.io/component=gateway")
        self.assertTrue(reaches("as-gateway", GATEWAY_NAMESPACE, f"authz.{NAMESPACE}.svc.cluster.local", 9180))
        self.assertTrue(reaches("as-gateway", GATEWAY_NAMESPACE, f"mimir.{NAMESPACE}.svc.cluster.local", 8080))
        self.assertFalse(reaches("as-gateway", GATEWAY_NAMESPACE, f"seaweedfs.{NAMESPACE}.svc.cluster.local", 8333))
        # A backend reaches object storage, not another backend's port and not the world outside.
        probe("as-mimir", NAMESPACE, "nighthawk.io/component=mimir")
        self.assertTrue(reaches("as-mimir", NAMESPACE, f"seaweedfs.{NAMESPACE}.svc.cluster.local", 8333))
        self.assertFalse(reaches("as-mimir", NAMESPACE, f"loki.{NAMESPACE}.svc.cluster.local", 3100))
        self.assertFalse(reaches("as-mimir", NAMESPACE, "vault.vault-dev.svc.cluster.local", 8200))
        self.assertFalse(reaches("as-mimir", NAMESPACE, "1.1.1.1", 443))
        for name, namespace in (("intruder", NAMESPACE), ("as-gateway", GATEWAY_NAMESPACE), ("as-mimir", NAMESPACE)):
            kubectl("-n", namespace, "delete", "pod", name, "--wait=false")
        services = json.loads(kubectl("get", "services", "-A", "-o", "json").stdout)["items"]
        external = [
            (item["metadata"]["namespace"], item["metadata"]["name"], [port["port"] for port in item["spec"]["ports"]])
            for item in services if item["spec"]["type"] in ("LoadBalancer", "NodePort")
        ]
        self.assertEqual(external, [(GATEWAY_NAMESPACE, "traefik", [443])])
        address = next(item for item in services if item["metadata"]["name"] == "traefik")["status"]["loadBalancer"]["ingress"][0]["ip"]
        self.assertTrue(address.startswith("192.168.49.2"), address)

    # ---- secrets and certificates --------------------------------------------------------

    def test_08_no_vault_credential_is_stored_in_the_cluster(self) -> None:
        for kind in ("secrets", "configmaps"):
            for item in json.loads(kubectl("get", kind, "-A", "-o", "json").stdout)["items"]:
                if item["metadata"]["namespace"] == "vault-dev":
                    continue
                values = [str(value) for value in (item.get("data") or {}).values()]
                if kind == "secrets":
                    values = [base64.b64decode(value).decode("utf-8", "replace") for value in values]
                for value in values:
                    self.assertNotIn(VAULT_DEV_TOKEN, value, item["metadata"]["name"])
                    self.assertNotRegex(value, r"\bhvs\.[A-Za-z0-9_-]{20,}", item["metadata"]["name"])
        # Everything rendered, except the downloaded chart packages kept beside it.
        rendered = "".join(
            path.read_text(encoding="utf-8") for path in CONTRACTS.rglob("*") if path.is_file() and ".charts" not in path.parts
        )
        self.assertNotIn(VAULT_DEV_TOKEN, rendered)
        for name in ("acme-web-ingest", "acme-web-query", "globex-edge-ingest"):
            self.assertNotIn(credential(name), rendered)

    def test_09_a_workload_cannot_read_another_workloads_secret(self) -> None:
        platform = load_platform(CONFIG)
        manifest = {
            "apiVersion": "secrets.hashicorp.com/v1beta1", "kind": "VaultStaticSecret",
            "metadata": {"name": "overreach", "namespace": NAMESPACE},
            "spec": {
                "vaultAuthRef": "mimir", "mount": platform.vault.kv_mount, "type": "kv-v2",
                "path": platform.secrets["grafana-admin"].path, "destination": {"name": "overreach", "create": True},
            },
        }
        kubectl("apply", "-f", "-", stdin=yaml.safe_dump(manifest))
        try:
            time.sleep(20)
            self.assertNotEqual(kubectl("-n", NAMESPACE, "get", "secret", "overreach", check=False).returncode, 0)
            events = kubectl("-n", NAMESPACE, "get", "events", "--field-selector", "involvedObject.name=overreach", "-o", "json").stdout
            self.assertIn("permission denied", events)
        finally:
            kubectl("-n", NAMESPACE, "delete", "vaultstaticsecret", "overreach", "--ignore-not-found")

    def test_10_certificates_are_signed_by_vault_from_keys_made_in_the_cluster(self) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        issuers = json.loads(kubectl("get", "issuers", "-A", "-o", "json").stdout)["items"]
        self.assertTrue(issuers)
        for issuer in issuers:
            # The signing endpoint takes a request; the issuing endpoint, which makes keys in Vault, is never used.
            self.assertRegex(issuer["spec"]["vault"]["path"], r"/sign/")
            self.assertNotIn("tokenSecretRef", issuer["spec"]["vault"]["auth"])
        certificate = x509.load_pem_x509_certificate(secret_value("gateway-server-tls", "tls.crt", GATEWAY_NAMESPACE).encode())
        key = serialization.load_pem_private_key(secret_value("gateway-server-tls", "tls.key", GATEWAY_NAMESPACE).encode(), None)
        public = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        self.assertEqual(certificate.public_key().public_bytes(*public), key.public_key().public_bytes(*public))
        platform = load_platform(CONFIG)
        ca = x509.load_pem_x509_certificate(trust.authority_certificate(vault.connect(platform, vault.Auth(), ENVIRONMENT)).encode())
        certificate.verify_directly_issued_by(ca)
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        self.assertEqual(sorted(names), sorted([platform.gateway.hostname, platform.grafana.hostname]))
        requests = json.loads(kubectl("-n", GATEWAY_NAMESPACE, "get", "certificaterequests", "-o", "json").stdout)["items"]
        self.assertTrue(all(item["spec"].get("request") for item in requests))

    def test_11_a_rotated_credential_reaches_the_gateway(self) -> None:
        platform = load_platform(CONFIG)
        old = credential("globex-web-ingest")
        new = "rotated-" + token_source.token_urlsafe(24)
        self.assertEqual(push_log("globex-web-ingest", "before rotation"), 204)
        from nighthawk import secrets
        secrets.rotate_secret(vault.connect(platform, vault.Auth(), ENVIRONMENT), platform, "globex-web-ingest", new)
        accepted = eventually(lambda: push_log(None, "after rotation", user="globex-web-ingest", password=new) == 204, timeout=240, interval=8)
        self.assertTrue(accepted, "the rotated credential was not accepted within the stated period")
        self.assertEqual(eventually(lambda: push_log(None, "old", user="globex-web-ingest", password=old) == 401 and 401, timeout=60), 401)

    def test_11b_a_chart_that_is_not_the_locked_one_is_not_installed(self) -> None:
        tampered = WORK / "tampered"
        shutil.rmtree(tampered, ignore_errors=True)
        shutil.copytree(CONTRACTS / "kubernetes", tampered, ignore=shutil.ignore_patterns(".charts"))
        releases = yaml.safe_load((tampered / "releases.yaml").read_text(encoding="utf-8"))
        releases["charts"]["metallb"]["sha256"] = "0" * 64
        (tampered / "releases.yaml").write_text(yaml.safe_dump(releases), encoding="utf-8")
        before = run("helm", "list", "-A", "-o", "json").stdout
        refused = playbook("k8s-addons.yml", check=False, environment={"NIGHTHAWK_K8S_RENDERED_DIR": str(tampered)})
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Nothing was installed from it", refused.stdout)
        self.assertIn("metallb", refused.stdout)
        # Nothing after the refused chart was touched either.
        self.assertEqual(run("helm", "list", "-A", "-o", "json").stdout, before)

    # ---- configuration and data ----------------------------------------------------------

    def test_12_a_changed_override_is_picked_up_without_a_restart(self) -> None:
        document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        document["tenants"][0]["datastreams"][0]["signals"]["metrics"]["ingestion_rate_samples_per_second"] = 12345
        CONFIG.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        before = pods()
        render()
        deploy()
        forward(NAMESPACE, "pod/mimir-0", "18080:8080")
        time.sleep(3)

        def loaded() -> bool:
            result = subprocess.run(["curl", "--silent", "--max-time", "10", "http://127.0.0.1:18080/runtime_config"], capture_output=True, text=True)
            return "ingestion_rate: 12345" in result.stdout

        self.assertTrue(eventually(loaded, timeout=180))
        after = pods()
        for name in ("mimir-0", "loki-0", "tempo-0", "pyroscope-0", "seaweedfs-0"):
            self.assertEqual(after[name], before[name], name)

    def test_13_data_survives_a_deleted_pod(self) -> None:
        self.assertEqual(len(eventually(lambda: query_logs("acme-web-query", f"fixture {self.run_id}")[1])), 4)
        kubectl("-n", NAMESPACE, "delete", "pod", "loki-0", "--wait=true")
        kubectl("-n", NAMESPACE, "wait", "--for=condition=Ready", "pod/loki-0", "--timeout=300s")
        self.assertEqual(len(eventually(lambda: query_logs("acme-web-query", f"fixture {self.run_id}")[1], timeout=180)), 4)

    def test_14_teardown_keeps_data_and_a_purge_needs_the_clusters_name(self) -> None:
        refused = playbook("k8s-teardown.yml", "-e", "k8s_platform_purge=true", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Nothing was deleted", refused.stdout)
        self.assertIn("loki-0", pods())
        playbook("k8s-teardown.yml")
        self.assertEqual(pods(), {})
        claims = kubectl("-n", NAMESPACE, "get", "pvc", "-o", "name").stdout.split()
        self.assertGreaterEqual(len(claims), 6, claims)
        stop_forwards()
        deploy()
        forward("vault-dev", "svc/vault", f"{VAULT_PORT}:8200")
        forward(GATEWAY_NAMESPACE, "svc/traefik-internal", f"{GATEWAY_PORT}:443", f"{CLUSTER_PORT}:8444")
        time.sleep(3)
        self.assertEqual(len(eventually(lambda: query_logs("acme-web-query", f"fixture {self.run_id}")[1], timeout=240)), 4)


if __name__ == "__main__":
    unittest.main()
