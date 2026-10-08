# Configuration contracts

## Status

The Python CLI validates and renders **non-secret intermediate contracts**.
It also emits a version-gated Pyroscope v2 retention override fragment.
It implements the full SOPS + age secrets lifecycle (recipient generation,
encryption, rotation, materialization, cleanup) gated on a `doctor`
prerequisite check, and a `check-pins` drift check against the compatibility
matrix. It does not yet deploy the platform, generate complete backend-native
configuration, enforce gateway authentication, or implement redaction.
The initial network contract contains shared entry points, not a complete
Kubernetes or host firewall. Do not deploy it as a complete allowlist.

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
`bin/python` executable. No cloud credentials, daemon, or secret files are needed
for these non-secret contract checks.

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
  byte-rate budget, and a query concurrency budget. These are platform policy
  inputs, not yet translated into backend-native units or runtime limits.
- Zero/unbounded retention is not accepted. Loki requires at least 24 hours;
  all durations must fit Go's signed duration representation. Version-specific
  runtime validation and deletion tests remain necessary.
- Every datastream has distinct ingestion and query credentials. References
  cannot alias the same encrypted file/key or cross signal backend boundaries.
- Optional ingestion certificate identities use
  `spiffe://nighthawk/<tenant>/<datastream>/<collector>`. The contract checks
  mapping consistency, not actual certificate validity or revocation.
- Collection policy approval and an explicit field-drop list are required.
  A field list alone is not evidence that telemetry has been sanitized.

Secret references point to SOPS-encrypted files under `secrets/`; no decryption
occurs during contract validation. Actual encrypted files, age recipients,
trust establishment, rotation, and runtime materialization are later deployment
prerequisites. Do not put plaintext values in this contract.

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
| `tls.enabled`, `tls.ca_secret_ref` | TLS required; optional private-CA reference |
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

This produces `platform.json`, `network.json`, `ports.md`, and
`pyroscope-overrides.yaml`. The latter uses the approved Pyroscope 2.3.1 v2
`retention_period` field, without a default retention or overrides for disabled
profiling streams. A version/storage-mode/field change fails until its
compatibility is explicitly reviewed. The fragment still requires a configured
Pyroscope runtime override loader and metastore deletion acceptance tests;
rendering it does not prove retention enforcement.

`config/versions.yaml` records source-verified candidates, not a complete image
digest lock or a runtime-tested matrix. Remaining component/chart/tool pins
must be resolved before deployment.

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

Every secrets subcommand first runs the same prerequisite check as
`nighthawk doctor`: it shells out to `sops --version` and `age --version` and
compares the result to the pins in `config/versions.yaml`. If either binary is
missing or mismatched, the command exits nonzero and writes no file:

```console
$ python -m nighthawk doctor --versions config/versions.yaml
FAIL: sops is not available: [Errno 2] No such file or directory: 'sops'
FAIL: age is not available: [Errno 2] No such file or directory: 'age'
```

With `sops`/`age` installed and matching their pinned versions:

```console
# Generate a local age key pair (non-production use; refuses to overwrite
# an existing file without --overwrite).
python -m nighthawk generate-recipient --output secrets/dev.agekey

# Encrypt a new secret value under secrets/<file> for one or more recipients.
python -m nighthawk encrypt-secret --file local.sops.yaml --key mimir-storage \
    --recipient age1exampleexampleexampleexampleexampleexampleexampleexamplex \
    --value "s3-secret-key"

# Rotate an existing key's value in place, keeping the same recipients.
python -m nighthawk rotate-secret --file local.sops.yaml --key mimir-storage \
    --value "new-s3-secret-key"

# Decrypt every secret referenced by a validated platform document into
# .materialized-secrets/ (created with owner-only 0700/0600 permissions).
python -m nighthawk materialize-secrets --config config/tenants.example.yaml

# Remove the decrypted material once it is no longer needed.
python -m nighthawk clean-secrets
```

`config/tenants.example.yaml` declares `profile: development`, so its
secret references may resolve to locally auto-generated recipients.
Documents with `profile: production` reject locally auto-generated age
recipients recorded in `.generated/age-recipients.local.json` unless
`--confirm-production-recipients` is passed explicitly, and require an
explicit, non-empty recipient list.

`nighthawk check-pins` compares each `tracked_consumers` entry in
`config/versions.yaml` (currently both Terraform `versions.tf` files) against
the matrix pins and reports any divergence:

```console
$ python -m nighthawk check-pins
All 4 tracked consumer(s) match the compatibility matrix.
```

## Troubleshooting and cleanup

- Unknown secret: add the intended reference, not a plaintext fallback value.
- Unsupported storage provider: choose the adapter matching the deployment.
- Backend-ID conflict: retain the stable mapping or plan a data migration.
- Existing output directory: choose a new directory or explicitly remove only
  the generated artifacts after reviewing them.
- Dependency errors: use the isolated interpreter and install the pinned
  requirements; do not substitute system packages silently.
- Missing or mismatched `sops`/`age`: run `nighthawk doctor` to see which
  binary is absent or out of date; every secrets subcommand fails the same way
  before writing any file.

Validation and rendering create no remote resources and need no infrastructure
rollback. Generated files are ignored by Git. A POSIX directory mode is requested
for output, but it is not a substitute for Windows ACLs.
