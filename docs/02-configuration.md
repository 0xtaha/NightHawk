# Configuration contracts

## Status

The Python CLI validates and renders **non-secret intermediate contracts**:
the platform and network manifests, version-gated per-tenant runtime overrides
for all four backends, the Traefik gateway configuration, and the Grafana
desired state. Separate commands render a collector configuration with
collection-time redaction, render the gateway auth policy from materialized
secrets, run the auth service, manage gateway credentials and certificates,
and provision Grafana. Secrets are stored in, and certificates are signed by,
a HashiCorp Vault that you provide; every command that touches it is gated on
the `doctor` prerequisite check. A `check-pins` drift check compares consumers
with the compatibility matrix.

`quickstart-docker` starts the whole platform with Docker Compose
([quickstart](00-quickstart.md)). Gateway enforcement, redaction, override
loading, and persistence have been observed on that stack under rootless
Podman; retention deletion has not. The development profile of self-hosted Kubernetes
is implemented ([Kubernetes](09-kubernetes.md)); its production profile and AWS are not. See
[collection](04-collection.md), [gateway](05-gateway.md), and
[tenant provisioning](06-tenant-provisioning.md).
`render-docker-deployment`, `generate-cluster-token`, and
`check-cluster-layout` serve the [host and cluster automation](08-ansible.md).
The network contract covers the gateway entry points, the flows behind the
gateway, SSH administration, the node-to-node flows of a k3s cluster, and
the flows between workloads inside a cluster. The host firewall role opens
exactly its inbound rules per host role, and the Kubernetes NetworkPolicies
are generated from its in-cluster rules ([Kubernetes](09-kubernetes.md#network-isolation)).

## Prerequisites

Use Python 3.12 and an isolated environment. Direct and transitive Python
dependencies are version-pinned in `requirements.txt`. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.\.venv\Scripts\python.exe -m nighthawk validate --config config\tenants.example.yaml
```

On Linux, invoke the same Python commands using the virtual environment's
`bin/python` executable. No cloud credentials, daemon, Vault, or secret files
are needed for these non-secret contract checks: `validate` and
`render-contracts` never contact Vault.

The unit tests use an in-memory fake of Vault and need no server. Eight
integration tests run against a real one and are reported as skipped, with
the reason, unless you point them at a disposable dev-mode Vault
([development Vault](#development-vault)) and give them its root token:

```console
$ NIGHTHAWK_VAULT_TEST_ADDR=http://127.0.0.1:8200 VAULT_TOKEN="$(cat .generated/vault-token)" \
    python -m unittest discover -s tests -p "test_*.py"
```

They create mounts and a policy of their own with a random `nhtest-` prefix
and remove them afterwards. One of them enables the `approle` auth method if
it is absent and disables it again. Do not point them at a Vault you care
about.

The Terraform contract tests need the pinned Terraform. They are reported as
skipped, with the reason, when it is absent. To download it into the ignored
`.tools/` directory, checked against the checksum in `config/versions.yaml`
(Linux on x86-64 or arm64 only):

```console
$ python -m nighthawk fetch-tools
Installed terraform, helm, kubectl, kubeconform into /path/to/NightHawk/.tools
```

The same command fetches the pinned Helm, kubectl, and kubeconform that the
[Kubernetes deployment](09-kubernetes.md) and its tests use, each kept only
if its checksum matches the matrix.

The tests look for `$NIGHTHAWK_TERRAFORM`, then `.tools/terraform`, then
`terraform` on `PATH`. They run `terraform test` in the two storage modules,
so each must have been initialized once with
`terraform init -backend=false`, which downloads the pinned AWS provider.

Expected validation output reports the datastream and network-rule counts.
Errors identify the invalid field or cross-resource relationship and return a
nonzero exit code.

## Tenant and credential contract

`config/platform.schema.json` rejects unknown fields and missing production
policy values. `config/tenants.example.yaml` is a complete example with **example
retention**, not production defaults.

- IDs use lowercase ASCII letters, digits, and hyphens. Backend IDs are globally
  unique; do not derive ambiguous concatenations at runtime.
- Each enabled signal explicitly supplies whole-hour retention, an ingestion
  budget, and a query concurrency budget. Logs, traces, and profiles declare
  `ingestion_rate_bytes_per_second`. Metrics declare
  `ingestion_rate_samples_per_second` instead, because Mimir enforces a
  per-tenant sample rate and has no per-tenant byte rate; a byte-based metrics
  budget is rejected. See [tenant provisioning](06-tenant-provisioning.md) for
  how each value becomes a backend override.
- Zero/unbounded retention is not accepted. Loki requires at least 24 hours;
  all durations must fit Go's signed duration representation. Version-specific
  runtime validation and deletion tests remain necessary.
- Every datastream has distinct ingestion and query credentials. References
  cannot alias the same Vault path and key or cross signal backend boundaries.
- Optional ingestion certificate identities use
  `spiffe://nighthawk/<tenant>/<datastream>/<collector>`. The contract checks
  mapping consistency, not actual certificate validity or revocation.
- Collection policy approval and an explicit field-drop list are required.
  Drop-field names are limited to ASCII letters, digits, `_`, `.`, and `-`
  because they are placed in collector regular expressions. A field list alone
  is not evidence that telemetry has been sanitized.
- Several credentials may be declared for the same tenant/datastream and
  permission. That is how a gateway credential is rotated without a gap.

### Vault block

The required `vault` object says where Vault is and which mounts and roles the
platform uses. It never holds a credential; see
[authentication](#authenticating-to-vault).

| Field | Meaning |
| --- | --- |
| `address` | Origin of the Vault server. `http://` is accepted only for a loopback address, which is what a dev-mode server uses |
| `namespace` | Optional. Sent as the `X-Vault-Namespace` header. Not exercised against a real namespace |
| `ca_file` | Optional absolute path to the CA bundle that verifies Vault's own TLS certificate. The system roots are used when it is absent |
| `kv_mount` | Mount path of a key-value **version 2** secrets engine |
| `pki.mount` | Mount path of the PKI secrets engine whose authority signs every certificate |
| `pki.server_role`, `pki.client_role` | PKI roles for the gateway and storage server certificates, and for collector client certificates |

A document with `profile: production` is rejected when `address` is plaintext
or loopback.

### Secret references

Each entry of the `secrets` map names a key at a path inside `kv_mount`:

```yaml
secrets:
  example-ingest: {path: nighthawk/local, key: example-ingest}
```

Several references may share a path; two references may not name the same
path and key. Everything else in the document refers to a secret by its name
in this map (`secret_ref`, `admin_secret_ref`, `identity.ref`,
`tls.ca_secret_ref`), so rotating a value never changes the document.

A document with `schema_version: 1` (secrets as `{file, key}` under
`secrets/`, and CA references on the gateway) is rejected with a message that
lists what changed.

### Gateway block

The required `gateway` object describes the single authenticated entry point.
Every address is explicit; nothing is derived from `deployment`.

| Field | Meaning |
| --- | --- |
| `hostname` | DNS name collectors and Grafana data sources use; also the gateway server certificate's name |
| `entry_points` | IDs of `config/network.yaml` rules whose destination is `gateway`. The gateway listens on exactly their ports. Rules sharing a port share one listener, which takes the most exposed scope among them |
| `grafana_entry_point` | The selected entry point Grafana data sources connect to |
| `auth_service` | Private address of the NightHawk auth service that Traefik calls |
| `upstreams` | Backend addresses per signal: `metrics`, `logs`, `profiles`, and for `traces` the `query`, `otlp_grpc`, and `otlp_http` addresses. Required for every signal any datastream enables. `grafana` is always required: the address the Grafana UI is forwarded to |
| `revoked_certificate_fingerprints` | Lowercase hex SHA-256 fingerprints of collector certificates the gateway must refuse |

Upstream and auth-service ports must match a network rule from `gateway` to
that component (`mimir`, `loki`, `tempo`, `pyroscope`, `grafana`, `auth-service`), so the
network contract stays the single owner of ports. `http://` upstreams are
plaintext links and must stay on a private network; see
[gateway](05-gateway.md).

The gateway verifies collector certificates against the public certificate of
the authority in `vault.pki.mount`. The document cannot reference a CA
certificate or a CA private key: the platform never holds the key.

### Grafana block

The required `grafana` object has two fields:

| Field | Meaning |
| --- | --- |
| `hostname` | DNS name of the Grafana UI. It must differ from the gateway hostname; the gateway serves both names on the same listeners and the gateway server certificate carries both |
| `admin_secret_ref` | Secret holding the Grafana administrator password. It must not be reused for anything else |

### Vault access for a cluster

Only a `self-hosted-k8s` document may have `vault.kubernetes_auth`. Its
`mount` is the Vault mount where the cluster's service accounts
authenticate; it must differ from the two other mounts. The optional
`cluster_address` is the address the cluster reaches Vault at when that
differs from `address`; a production document refuses a plaintext one. No
credential is declared. What Vault must allow at that mount is rendered to
`vault/kubernetes-auth.json`; see
[Kubernetes](09-kubernetes.md#what-vault-must-allow).

### Cluster block

Only a `self-hosted-k8s` document may have a `cluster` object. Its one field,
`join_token_secret_ref`, names the secret that holds the token k3s nodes join
with. It must be a declared secret that nothing else uses.
`config/self-hosted.example.yaml` is an example. Create the token once:

```console
$ python -m nighthawk generate-cluster-token --config config/self-hosted.example.yaml
Generated the cluster join token in Vault at nighthawk/cluster key join-token
```

Run again, it says the token already exists and writes nothing: nodes hold
the token, so it is never replaced. See
[k3s cluster](08-ansible.md#k3s-cluster-self-hosted-profile).

### Storage identity secrets

For local SeaweedFS storage, each binding's `identity.ref` names a secret whose
value is a JSON object with exactly `access_key` and `secret_key`. Create it
with `generate-storage-identity`; any other shape is rejected when the storage
configuration is rendered.

```console
$ python -m nighthawk generate-storage-identity --config config/tenants.example.yaml --identity mimir-storage
Generated the storage keys for mimir-storage in Vault at nighthawk/local key mimir-storage
```

No secret is read during contract validation. Do not put plaintext values in
this contract.

## Storage interface

Docker and self-hosted Kubernetes require `storage.provider: seaweedfs`.
Cloud requires `storage.provider: aws`. No Azure/GCP adapter is implemented.
Direct backend filesystem storage and cloud-backed self-hosted storage are
outside the approved architecture.

Each logical bucket binding contains:

| Field | Meaning |
| --- | --- |
| `protocol` | `s3` |
| `endpoint`, `region`, `bucket` | HTTPS origin and explicit storage location |
| `force_path_style` | Explicit S3 addressing choice |
| `tls.enabled` | TLS is required |
| `tls.trust`, `tls.ca_secret_ref` | What verifies the endpoint's certificate: `system` roots, the platform `pki` authority, or a CA certificate stored as a `secret` named by `ca_secret_ref`. Local SeaweedFS must use `pki`, because its server certificate is signed by that authority, and nothing else may. When `trust` is absent, as in the Terraform output, it is `system` for a null `ca_secret_ref` and `secret` otherwise |
| `identity.type`, `identity.ref` | Local secret reference or AWS IRSA role ARN |
| `capabilities` | Versioning, lifecycle, and workload-identity declarations |

Separate buckets are required for enabled signals and Mimir's blocks, ruler,
and Alertmanager data. Signal backends cannot share identities. A backend may
use its own identity across its assigned buckets. Capability declarations are
not automatic configuration or compatibility evidence.

The Terraform storage facade emits this exact `storage` object. To import its
non-secret output, set the platform YAML deployment to `aws`, export the named
output from the Terraform environment, and validate:

```powershell
terraform output -json storage | Set-Content -Encoding utf8NoBOM storage.json
.\.venv\Scripts\python.exe -m nighthawk validate --config config\aws.yaml --storage-output storage.json
```

The export command runs from the initialized Terraform root; the Python command
runs from the repository root with the exported file's actual path. The paths
above are operator-created inputs, not shipped working cloud configuration.
`--storage-output` explicitly replaces the platform file's storage object before
validation. Export the named output, not the complete Terraform state or
the wrapper from `terraform output -json`. No static AWS keys are accepted.
The imported bindings are also included when using `render-contracts`.

The implemented [cloud facade](../terraform/modules/object-storage/README.md)
and [AWS execution root](../terraform/environments/aws-storage/README.md)
document inputs, IRSA provisioning, lifecycle safeguards, and deployment
prerequisites. Cross-language tests consume actual Terraform mock-plan outputs;
they do not prove real AWS access or retention enforcement.

## Deterministic rendering and migrations

```powershell
.\.venv\Scripts\python.exe -m nighthawk render-contracts --config config\tenants.example.yaml --output .generated\contracts
```

This produces `platform.json`, `network.json`, `ports.md`, one
`<backend>-overrides.yaml` per signal backend, `unenforced-limits.json`,
`gateway/` (Traefik configuration and route table),
`grafana/desired-state.json`, `vault/` ([what the platform needs from
Vault](#what-the-platform-needs-from-vault)), `ansible/nighthawk.yml` (the one
variables file every playbook loads: firewall rules, version pins, checksums,
kernel settings, and supported systems; see [Ansible](08-ansible.md)), and, for a `docker` deployment, `backends/`
with each backend's own configuration ([Docker Compose](07-docker-compose.md)). `pyroscope-overrides.yaml` uses the approved Pyroscope 2.3.1 v2
`retention_period` field, without a default retention or overrides for disabled
profiling streams. A version/storage-mode/field change fails until its
compatibility is explicitly reviewed. The fragment still requires a configured
Pyroscope runtime override loader and metastore deletion acceptance tests;
rendering it does not prove retention enforcement.

Output ordering
and newlines are deterministic. The output directory must not already exist;
the command does not overwrite or delete operator files. Partial output from
a filesystem error is reported as failure and must not be consumed.

Before updating a deployed configuration, supply its previous YAML explicitly:

```powershell
.\.venv\Scripts\python.exe -m nighthawk validate --config config\production.yaml --previous config\previous.yaml
```

Changes to an existing pair's backend ID and reassignment of an existing ID to
another pair are rejected as migrations. Without a previous configuration, the
CLI can validate only the current mapping; it cannot infer historical IDs.
Repeat `--previous` for retained older configurations to prevent reuse of IDs
removed in a more recent snapshot. Deployment orchestration must preserve that
history, including previous resolved storage bindings. No migration execution
command is implemented yet.

## Secrets workflow

Secrets live in the Vault the platform document declares. You provide that
Vault: the platform never deploys, initializes, unseals, or backs one up, and
it is tested only against HashiCorp Vault 2.1.2 in dev mode. The supported
server range is in `config/versions.yaml` under `secrets_store.vault`.

### What the platform needs from Vault

`render-contracts` writes two non-secret files for you to apply:

- `vault/policy.hcl`: an ACL policy that allows creating, reading, and
  updating exactly the declared secret paths, signing with the two PKI roles,
  reading those roles, revoking a certificate, and looking up its own token.
  Nothing else.
- `vault/pki-roles.json`: the two role definitions. The server role allows
  only the gateway, Grafana, and local storage hostnames, for server use. The
  collector role allows only the declared certificate identities as URI names,
  for client use. Both cap validity at 9528 hours (397 days).

Before the platform can be used you need, in Vault: a key-value version 2
mount and a PKI mount with a certificate authority at the declared paths, the
two roles written from `vault/pki-roles.json`, and a token or AppRole carrying
the policy.

Apply the files again whenever the hostnames, the collector certificate
identities, or the secret paths in the document change. `doctor` names any
declared name a role in Vault would refuse.

### Authenticating to Vault

A command that needs Vault takes its credential from, in order:

1. the `VAULT_TOKEN` environment variable;
2. `--vault-token-file`, a file holding a token;
3. `--vault-role-id-file` together with `--vault-secret-id-file`, for an
   AppRole login at the `approle` auth mount. The resulting token is used for
   that one command and is not stored.

Credential files must be readable by their owner only. No command accepts a
credential as an argument, and none is ever written to the platform document,
the Compose environment file, the materialization directory, or the output.

For `profile: production`, a credential that carries Vault's `root` policy is
refused before anything is read or written.

### Prerequisite check

Every command that reads or writes a secret or requests a certificate first
runs the same check as `nighthawk doctor`, and stops before doing anything if
it fails. The examples from here on assume `VAULT_TOKEN` is set; add
`--vault-token-file` otherwise.

```console
$ python -m nighthawk doctor --config config/tenants.example.yaml
ok: Vault 2.1.2 at http://127.0.0.1:8200 is reachable and unsealed
ok: Vault 2.1.2 is supported
ok: the Vault credential is accepted
ok: key-value mount nighthawk-kv is version 2
ok: PKI mount nighthawk-pki has a certificate authority
ok: PKI role nighthawk-server is present
ok: PKI role nighthawk-collector is present
```

It fails, naming each problem, when Vault is unreachable, sealed, or not
initialized; reports a version outside the supported range; rejects the
credential; lacks a declared mount, authority, or role; or has a role that
would refuse a declared hostname or identity. Against a dev-mode Vault that
has not been bootstrapped:

```console
$ python -m nighthawk doctor --config config/tenants.example.yaml
ok: Vault 2.1.2 at http://127.0.0.1:8200 is reachable and unsealed
ok: Vault 2.1.2 is supported
ok: the Vault credential is accepted
FAIL: key-value mount nighthawk-kv does not exist or is not readable
FAIL: PKI mount nighthawk-pki does not exist or is not readable
FAIL: PKI role nighthawk-server does not exist or is not readable
FAIL: PKI role nighthawk-collector does not exist or is not readable
```

### Storing, rotating, and materializing

A value is read from standard input, or from `--value-file`. It is never an
argument.

```console
# Store a value at a reference that holds nothing yet.
$ python -m nighthawk store-secret --config config/tenants.example.yaml --secret grafana-admin < password.txt
Stored grafana-admin in Vault at nighthawk/local key grafana-admin

# Replace an existing value. The reference in the platform document does not change.
$ python -m nighthawk rotate-secret --config config/tenants.example.yaml --secret grafana-admin < new-password.txt
Rotated grafana-admin in Vault at nighthawk/local key grafana-admin

# Read every secret the document references into .materialized-secrets/.
$ python -m nighthawk materialize-secrets --config config/tenants.example.yaml
Materialized 7 secret(s) into /path/to/NightHawk/.materialized-secrets

# Remove the materialized files once they are no longer needed.
$ python -m nighthawk clean-secrets
Cleaned /path/to/NightHawk/.materialized-secrets
```

- `store-secret` keeps every other key at the same path and refuses a key
  that already holds a value. `rotate-secret` refuses a key that holds none.
- Both write with Vault's check-and-set. If the path changed between the read
  and the write, Vault rejects the write, nothing is overwritten, and the
  command asks you to retry.
- A rotated value becomes a new version. Vault keeps the earlier versions
  until the mount's `max_versions` setting or an explicit destroy removes
  them; the platform never destroys a version.
- `materialize-secrets` writes each secret to
  `.materialized-secrets/kv/<path>/<key>` with owner-only permissions (0700
  directories, 0600 files). If any referenced secret holds no value it names
  all of them and writes nothing.
- `generate-credential` and `generate-storage-identity` store a generated
  value the same way as `store-secret`; see [gateway](05-gateway.md).

### Development Vault

For local work and tests, a dev-mode Vault is enough. It keeps everything in
memory and loses it when it stops, so use it only for throwaway data.

```console
$ mkdir -p .generated && (umask 077; openssl rand -hex 16 > .generated/vault-token)
$ docker run --detach --name nighthawk-vault --cap-add IPC_LOCK \
    --publish 127.0.0.1:8200:8200 \
    --env VAULT_DEV_ROOT_TOKEN_ID="$(cat .generated/vault-token)" \
    --env VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
    docker.io/hashicorp/vault@sha256:c2f666266f383d2cf424d86b8bb8ce7d065562173ffec2b476d762943608bb55 server -dev

$ python -m nighthawk bootstrap-dev-vault --config config/tenants.example.yaml \
    --vault-token-file .generated/vault-token --confirm-disposable-vault --ca-valid-days 365
Configured the development Vault: mount nighthawk-kv, mount nighthawk-pki, certificate authority, PKI role nighthawk-server, PKI role nighthawk-collector, policy nighthawk
```

`.generated/` is ignored by Git, and the token file is created readable by
you only. The token is the dev server's root token: it protects nothing beyond
that throwaway container, and it stops working when the container stops.
`--ca-valid-days` must cover the longest certificate you will ask for; the
quickstart asks for 90 days by default.

`bootstrap-dev-vault` applies the rendered policy and roles and creates a
certificate authority inside Vault. It needs a credential with administrative
access, refuses a `profile: production` document, and refuses to run without
`--confirm-disposable-vault`. Running it again changes only what differs and
never replaces an existing authority. For your own Vault, apply the rendered
files instead.

`nighthawk check-pins` compares each `tracked_consumers` entry in
`config/versions.yaml` (the Terraform version files, the Alloy sources
README, and the Compose image references) against the matrix pins and reports
any divergence:

```console
$ python -m nighthawk check-pins
All 37 tracked consumer(s) match the compatibility matrix.
```

## Troubleshooting and cleanup

- Unknown secret: add the intended reference, not a plaintext fallback value.
- Unsupported storage provider: choose the adapter matching the deployment.
- Backend-ID conflict: retain the stable mapping or plan a data migration.
- Existing output directory: choose a new directory or explicitly remove only
  the generated artifacts after reviewing them.
- Dependency errors: use the isolated interpreter and install the pinned
  requirements; do not substitute system packages silently.
- `Vault prerequisite check failed`: run `nighthawk doctor` to see every
  failing check. Every command that touches Vault fails the same way before
  reading or writing anything.
- `changed in Vault during write secret`: someone else wrote the same path at
  the same moment. Nothing was overwritten; run the command again.
- `does not allow <name>; apply the rendered vault/pki-roles.json`: the
  document declares a hostname or collector identity the role in Vault would
  refuse. Render the contracts and apply the roles, or for a development
  Vault run `bootstrap-dev-vault` again.

Validation and rendering create no remote resources and need no infrastructure
rollback. Generated files are ignored by Git. A POSIX directory mode is requested
for output, but it is not a substitute for Windows ACLs.
