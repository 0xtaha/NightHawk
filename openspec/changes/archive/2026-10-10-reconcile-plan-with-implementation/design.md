# Design

## Context

See `proposal.md` for motivation. This change comes from reading `Plan.md`
sections 1–4, its conventions, and its Docker-tier acceptance criteria
against the repository. Each finding below was confirmed in the code before
it was given a disposition.

Constraints that shape the approach:

- No Terraform binary is installed. The matrix already records Linux
  checksums for Terraform 1.13.5, but nothing downloads it: the `fetch-tools`
  command went away with the `sops` and `age` downloads when
  `adopt-vault-secrets` landed. `tests/terraform/` has no `__init__.py`, so
  the documented command (`python -m unittest discover -s tests -p
  "test_*.py"`) never collects it.
- Secrets and certificates now come from an operator-provided Vault. Any
  task that starts the stack needs a bootstrapped dev-mode Vault; the
  end-to-end suite starts its own.
- Nothing has been applied to an AWS account. Terraform changes are verified
  by `terraform validate` and plan-only tests with mocked providers.
- The end-to-end suite has only run under rootless Podman, where every
  service runs as container UID 0 and the `host-collection` profile cannot
  work.
- The baseline is green: 316 unit tests pass, 9 skipped (the gated e2e
  suite, and 8 Vault integration tests that run only against a dev-mode
  Vault).

### Findings and dispositions

| # | Finding | Evidence | Disposition |
|---|---|---|---|
| 1 | EKS has no access configuration; endpoint and admin are AWS defaults | `terraform/modules/aws-eks/main.tf:29-40` | Build (D2) |
| 2 | No encryption for Kubernetes secrets or node volumes | same file; no `encryption_config`, no launch template | Build (D3) |
| 3 | Contract-derived security group is attached to nothing; `var.vpc_id` unused | `aws-vpc-network/main.tf:103-119`, `environments/aws/main.tf:66-69` | Build (D3) |
| 4 | EBS CSI policy sits on the shared node role | `aws-eks/main.tf:77-80` | Build (D4) |
| 5 | No DNS or controller IAM, no subnet role tags | no match in `terraform/` | Build, except the load balancer controller (D4) |
| 6 | Rule parity test compares IDs and count only | `tests/test_terraform_boundaries.py:40-59` | Build (D5) |
| 7 | Storage root's backend example names a KMS key the bootstrap never creates | `aws-storage/backend.hcl.example:5` | Fix the example |
| 8 | Terraform tests are not collected and fail rather than skip without Terraform | `tests/terraform/test_storage_contract.py:25-29` | Build (D6) |
| 9 | `encrypt-secret` rewrites the whole file; writes a plaintext temp file | code since removed | Closed by `adopt-vault-secrets` (D7) |
| 10 | `rotate-secret` puts the value in argv | code since removed | Closed by `adopt-vault-secrets` (D7) |
| 11 | `--bind-address` accepts any address for a `loopback`-scoped entry point | `__main__.py:161`, `authz.py:183-185` | Build (D8) |
| 12 | Published port hard-coded to 8443 | `docker-compose.yaml:190,217` | Build (D8) |
| 13 | Quickstart fails with two ingestion credentials; Grafana takes the first query credential by ID | `quickstart.py:288`, `collector.py:82-86`, `grafana.py:76-81` | Build (D9) |
| 14 | Quickstart never resends a rotated password to Grafana | `docker-compose.yaml:266-275` | Build (D10) |
| 15 | Dotted drop fields are not matched in sanitized label form | `collector.py:55-57`, `fixtures.py:143` | Build (D11) |
| 16 | `host-collection` mounts a directory nothing renders; node exporter reads the container's filesystem | `docker-compose.yaml:324`, `alloy-configs/docker/metrics.alloy:3` | Build (D12) |
| 17 | Tempo chart app version 3.1.0 against backend pin 3.0.3, unrecorded | `config/versions.yaml:20,90` | Build (D13) |
| 18 | Readiness spec text requires a health check on every service; four backends and Alloy deliberately have none | `openspec/specs/docker-compose-stack/spec.md:49-51`, `tests/test_compose.py:134` | Reword the spec to the recorded design |
| 19 | e2e isolation is logs only; retention asserted for two values; correlations, Pyroscope tenant enforcement, per-backend restart untested; metrics redaction check can pass vacuously | `tests/e2e/test_stack.py:200-241,343-362,430-440` | Add tests (D14) |
| 20 | Documents contradict themselves or the code | listed in tasks group 8 | Correct |
| 21 | Plan items with no implementation and no recorded deferral | listed under Non-Goals | Record in `Plan.md` |

## Goals / Non-Goals

**Goals:**

- Every commitment in plan phases 1–4 is implemented, or `Plan.md` names the
  later phase that owns it.
- Documents state only what the code does and what was observed.

**Non-Goals:**

These are recorded as deferred by this change, not built:

- Ansible collection pins and verification of the OS matrix (phase 5, where
  the first consumer exists).
- Chart lockfiles and Kubernetes image digests (phase 6; there is no chart to
  lock yet).
- IAM for the AWS load balancer controller (phase 6; its policy is published
  per controller release and no release is pinned yet).
- Firewall-format outputs such as UFW or iptables rules (phase 8 already owns
  them; `docs/01` is corrected to say only the port table and JSON exist).
- Prerequisite checks beyond Vault and Terraform (phase 9).
- Modelled backups and a state-bucket version expiry (phase 10 backup and
  restore work).

Also out of scope: phases 5–10 themselves; retention deletion tests and
production single-node Compose, which are already recorded as deferred; and
the zero-resource `docker` and `self-hosted-k8s` Terraform roots, which stay
as the accepted spec defines them.

## Decisions

### D1. One reconciliation change, grouped by area

Findings are closed in one change with independent task groups rather than
one change per area. They share a single audit and a single baseline, and
most are small. The groups do not depend on each other, so they can be
applied and reviewed separately. *Alternative:* separate changes for
Terraform, secrets, and Docker. Rejected as overhead for work that shares no
design questions across areas.

### D2. Cluster access is stated by the operator, with no default

`aws-eks` gains required inputs: `endpoint_public_access` (bool),
`public_access_cidrs` (list), and `cluster_admin_principal_arns` (list). The
cluster uses API authentication mode with the creator's bootstrap admin
permission turned off, and one access entry with the cluster-admin policy per
listed principal. Private endpoint access is always on. Validation rejects a
public endpoint with an empty list or with `0.0.0.0/0`, and an empty
administrator list.

No default follows the project's rule against silent fallbacks.
*Alternative:* default to private-only. Rejected because a private-only
cluster is unreachable without a network path the operator has to arrange, so
a default would only move the surprise.

### D3. One launch template per node group carries security groups and volume encryption

Managed node groups take security groups and root volume settings only
through a launch template. Each node group gets a template that sets an
encrypted root volume and attaches the cluster security group plus the
contract-derived group for its source. `aws-eks` takes the group IDs as an
input from `aws-vpc-network`; the unused `vpc_id` variable is removed.

Kubernetes secrets use `encryption_config` with a KMS key: the operator's if
supplied, otherwise one the module creates with rotation enabled.

*Alternative for volumes:* account-level EBS encryption by default. Rejected:
it changes every volume in the account, outside this module's ownership.

The spec said "ingress rules" while the only AWS-scoped contract rule is
egress to S3. The requirement now says rules follow the direction the
contract declares.

### D4. Controller roles follow the existing IRSA pattern

`aws-s3-backends` already builds roles from an OIDC provider and
operator-named service account subjects. `aws-eks` uses the same shape for:

- **EBS CSI**, always on, bound to the addon through its service account role
  input; the node role loses `AmazonEBSCSIDriverPolicy`.
- **DNS records** and **certificate DNS-01**, each optional, scoped to
  operator-listed Route 53 hosted zones.
- **Cluster Autoscaler**, optional, with write actions conditioned on this
  cluster's node group tags.

Subnets get `kubernetes.io/role/elb` and `kubernetes.io/role/internal-elb`
tags now, since they cost nothing and any load balancer integration needs
them. The load balancer controller's own IAM is deferred (see Non-Goals).

### D5. Parity is checked field by field, still by hand-mirroring

The recorded decision to mirror AWS-scoped rules into the root as a literal
stands. The test parses each mirrored rule's fields and compares them with
the contract, reporting rule ID and field. *Alternative:* generate a tfvars
file from the renderer. It was considered and rejected in the phase 2 design;
nothing found here changes that.

### D6. Terraform is fetched like the other pinned tools, and its tests skip visibly

A `fetch-tools` command is added again, for Terraform only: it downloads the
pinned archive, verifies it against the matrix checksum, and installs it into
`.tools/`. `tests/terraform/` becomes a package so discovery collects it, and
its tests skip with a stated reason when the pinned Terraform is absent. A
skip is visible in the test summary; an omission is not. This matches what
the phase 1 design said would happen.

The Terraform contract test builds its input from the example platform
document. It was never collected, so it has not run since that document
moved to schema version 2 with a `vault` section and `tls.trust`. Whatever it
needs for the new shape is fixed with the first run.

### D7. The SOPS defects are closed by the Vault change

Findings 9 and 10 were deliberately left for `adopt-vault-secrets`, which is
now archived. It replaced the SOPS workflow with Vault's key-value engine:
storing never replaces an existing value, writes use check-and-set, and
values are read from standard input or a file and never placed in an
argument. `encrypt-secret` no longer exists. Nothing about them remains in
this change.

### D8. Non-loopback publication is refused until the production profile exists

The Compose file publishes only the `local-gateway` entry point, whose policy
scope is `loopback` and therefore admits ingestion without a client
certificate. Production single-node use is already deferred. Until it exists,
`quickstart-docker` rejects a bind address that is not loopback, in the
prerequisite check before anything is generated. The flag stays, for other
loopback addresses. The published port and Grafana root URL come from the
platform document through the environment file the quickstart already writes.

*Alternative:* remove the flag. Rejected: binding to `::1` or another
loopback address is a legitimate use.

### D9. Overlapping credentials need a named choice everywhere

The collector renderer already fails when several ingestion credentials
qualify and none is named. Grafana provisioning adopts the same rule in place
of "first by ID": a repeatable `--credential <id>` option on
`provision-grafana` selects the query credential for the datastream that
credential belongs to. `quickstart-docker` takes the same option, accepts
ingestion and query credential IDs, and passes each to its consumer.

This makes the documented rotation runbook work: declare the second
credential, re-run with the new one named, then remove the old one.
*Alternative:* pick the newest, or the last by ID. Rejected: any implicit
order makes one direction of rotation silently wrong, which is the current
defect.

### D10. The quickstart resends passwords only when a query secret changed

The quickstart already tracks which rendered and materialized files changed
in a run. When a query credential's materialized secret is among them, it
runs Grafana provisioning with `--update-secrets`; otherwise without. An
unconditional resend would make every run report changes and break the
"second run reports no changes" behaviour.

### D11. Label pipelines match both spellings of a drop field

Prometheus-style label names cannot hold `.` or `-`, so `user.email` arrives
as `user_email`. The label-drop expression in the metrics, log-label, and
profile pipelines includes each drop field and its sanitized form. OTLP
attribute rules are unchanged, since attributes keep their original names.
The e2e platform document gains one dotted drop field so the path runs.

### D12. Host collection is opt-in and refused where it cannot work

`quickstart-docker --host-collection` renders the full `docker` collector
profile into the directory the `alloy-host` service mounts and activates that
Compose profile. The node exporter is configured with the mounted host root
and its `proc` and `sys` paths. Under rootless Podman the quickstart refuses
the option, as the profile needs the Docker socket and host mounts.

Runtime verification needs Docker Engine and stays recorded as not done.

### D13. Chart exceptions are data in the matrix

A chart entry may carry an `app_version_exception` with a `reason`. Matrix
validation compares each backend chart's `app_version` with the backend pin
and fails on an unrecorded difference or on an exception that no longer
applies. The Mimir chart's weekly-build comment becomes such an exception.
For Tempo the implementer checks upstream for a chart release whose
application version is 3.0.3 and pins it; if none exists, records the
exception with the reason.

### D14. The e2e suite proves what the documents already say

No new behaviour, only assertions, reusing the existing 2×2 document:

- Cross-tenant reads attempted for metrics, traces, and profiles as well as
  logs.
- Retention made different per signal within one pair in
  `tests/e2e/platform.yaml`, then asserted per signal for every pair on each
  backend that exposes its loaded overrides. Loki has no such endpoint; the
  unused capture is removed and `docs/06` stops claiming it was observed.
- Each provisioned correlation target resolved to an existing data source UID
  in the same organization.
- A request without a tenant refused by Pyroscope.
- The metrics redaction check asserts that fixture series were returned
  before asserting markers are absent.
- Each backend restarted alone, with its signal still queryable afterwards.

## Risks / Trade-offs

- [Terraform changes cannot be applied anywhere] → Plan-only mocked tests for
  every new scenario; READMEs and `docs/01` keep stating that nothing is
  applied or runtime-verified.
- [Launch templates replace both node groups] → No cluster exists, so there
  is nothing to migrate; the READMEs note the replacement for anyone who
  applied an earlier revision.
- [`provision-grafana` now fails where it used to pick silently] → Only when
  two query credentials overlap; the error lists the candidates and the
  option. The rotation runbook in `docs/05` is rewritten around it.
- [The e2e additions lengthen a slow suite] → Reuse the running stack and the
  existing fixtures; per-backend restarts are one test.
- [Disabling the creator's admin permission can lock an operator out] →
  Validation requires at least one administrator principal.

## Migration Plan

Nothing is deployed. Operators with a local checkout re-run
`quickstart-docker`; existing secrets, certificates, and volumes are reused.
`terraform/environments/aws` needs the new required variables added to its
`tfvars` before the next plan.
