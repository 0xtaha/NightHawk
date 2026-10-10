# Proposal

## Why

Phases 1–4 of `Plan.md` are implemented and archived, but an audit of the plan
text against the repository found commitments that were never built, defects
in what was built, runtime checks that are thinner than the documents claim,
and documents that still describe an earlier state. Phases 5–10 build on
these foundations (Ansible and Helm consume the matrix, EKS workloads consume
the Terraform outputs, CI runs the test suite), so the gaps should be closed
or explicitly recorded before that work starts.

The audit covered plan sections 1–4, the shared conventions, and the
acceptance criteria that apply to the Docker tier. Phases 5–10 are not
started and their absence is not a finding.

## What Changes

**AWS compute (plan phase 2, last paragraph: not built)**

- EKS cluster access is configured explicitly: authentication mode, access
  entries for operator-supplied principals, and API endpoint exposure with an
  operator-supplied CIDR list. Today the cluster takes AWS defaults (public
  endpoint, implicit admin for the creating principal). **BREAKING** for the
  `aws-eks` module and `environments/aws` root: new required inputs.
- Encryption at rest: Kubernetes secrets envelope encryption with a KMS key,
  and encrypted node root volumes.
- The contract-derived security group is attached to the node groups. Today
  it is created and exported but attached to nothing, so the rule has no
  effect.
- The EBS CSI driver moves from the shared node role to its own IRSA role.
- IRSA roles for the DNS and autoscaling controllers the plan names
  (external-dns, cert-manager DNS-01, Cluster Autoscaler), and the subnet
  role tags load balancers discover subnets by.
- The network-rule parity check compares every field of a mirrored rule, not
  only its ID.
- A `fetch-tools` command installs the pinned Terraform, and the Terraform contract
  tests are collected by the documented test command.

**Secrets workflow (closed elsewhere)**

- The audit also found two defects in the SOPS workflow (`encrypt-secret`
  rewrote the whole target file, and secret values passed through a plaintext
  temporary file and the `sops` command line). `adopt-vault-secrets`,
  archived on 2026-10-10, replaced that workflow with HashiCorp Vault and
  removed the code, so nothing remains to do here.

**Docker quickstart and collection (defects)**

- A non-loopback `--bind-address` is refused. Today it publishes an entry
  point whose policy scope is `loopback`, which accepts ingestion without a
  client certificate.
- Credential rotation works on the Docker tier: the quickstart and
  `provision-grafana` take an explicit credential choice when two overlap.
  Today the quickstart fails once a second ingestion credential is declared,
  and Grafana keeps using the old query credential until it is removed.
  **BREAKING** for `provision-grafana`: overlapping query credentials without
  a choice now fail instead of silently taking the first by ID.
- A secret rotated in place reaches Grafana on the next quickstart run.
- Drop fields containing `.` or `-` are also removed in their sanitized label
  form (`user_email` for `user.email`).
- The `host-collection` Compose profile gets a rendered configuration and
  host filesystem paths. Today it mounts a directory nothing writes.
- The published gateway port follows the platform document instead of a
  hard-coded `8443`.

**Compatibility matrix**

- A chart whose application version differs from the backend pin fails
  validation unless the difference is recorded with a reason (the Tempo chart
  currently declares 3.1.0 against a 3.0.3 backend pin, unremarked).

**Runtime evidence at the Docker tier (tests only)**

- The end-to-end suite covers what the acceptance criteria and documents
  already claim: cross-tenant reads for all four signals, retention values
  per signal and pair, correlation targets, tenant enforcement on Pyroscope,
  a non-vacuous metrics redaction check, and a restart of each backend.

**Documents**

- The Compose readiness requirement is reworded to the design that was
  built and recorded (services whose image has no probe binary are gated by
  a waiting one-shot).
- `Plan.md` gains amendments for the remaining MinIO wording, its stale
  "current state" section, and each plan item that is deliberately deferred
  to a later phase.
- Stale and contradictory passages in `docs/01`, `02`, `04`, `05`, `06`,
  `07` and the Terraform backend example are corrected.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `aws-compute-infrastructure`: security groups carry the contract's rules in
  their declared direction and are attached to nodes; the EBS CSI driver uses
  IRSA; new requirements for explicit cluster access, encryption at rest, and
  controller identities.
- `docker-quickstart`: loopback-only publication, explicit credential choice
  during rotation, rotated secrets reach Grafana, optional host collection
  configuration.
- `grafana-tenant-provisioning`: explicit query credential choice when
  several are declared for a datastream.
- `alloy-collection`: redaction also matches the sanitized form of a drop
  field name.
- `docker-compose-stack`: the readiness requirement allows a waiting one-shot
  where an image has no probe binary.
- `compatibility-matrix`: chart and backend pins agree or the difference is
  recorded; checksummed Terraform download.

## Impact

- **Terraform**: `terraform/modules/aws-eks`, `terraform/modules/aws-vpc-network`,
  `terraform/environments/aws` (new required variables, new resources;
  existing plans will show additions and a node-group replacement), the
  `aws-storage` backend example. No resource has been applied to any account,
  so there is no live migration.
- **Python**: `nighthawk/quickstart.py`, `collector.py`, `grafana.py`,
  `config.py`, `tools.py`, `__main__.py`.
- **Compose and Alloy**: `docker-compose/docker-compose.yaml`,
  `alloy-configs/docker/metrics.alloy`.
- **Config**: `config/versions.yaml` and its schema.
- **Tests**: unit tests beside each change; `tests/e2e/test_stack.py` and
  `tests/e2e/platform.yaml`; `tests/terraform/`.
- **Documents**: `Plan.md`, `docs/00`–`07`, module and root READMEs.
- **Verification limits**: Terraform checks are plan-only with mocked
  providers; nothing here is applied to AWS. The end-to-end suite needs the
  container runtime it has run under so far (rootless Podman) and starts its
  own dev-mode Vault; the tasks that start the stack by hand need a
  bootstrapped dev-mode Vault as `docs/00-quickstart.md` describes. The
  `host-collection` profile needs Docker Engine and stays unexercised at
  runtime.
