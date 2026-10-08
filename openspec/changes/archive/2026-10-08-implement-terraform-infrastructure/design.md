# Design

## Context

See `proposal.md` - Why/What Changes for motivation and scope. Relevant
current state this design builds on:

- `terraform/modules/object-storage` and `terraform/modules/aws-s3-backends`
  already exist and are pinned to Terraform `1.13.5` and AWS provider
  `6.12.0` (`terraform { required_version = "= 1.13.5" }`,
  `aws = { version = "= 6.12.0" }`). `terraform/environments/aws-storage`'s
  README explicitly states it "requires existing EKS/OIDC and state-bootstrap
  resources" - neither exists yet. This design adds both, without touching
  the already-implemented storage facade's interface.
- `aws-s3-backends`'s `aws_identity` variable is
  `{ oidc_provider_arn: string, oidc_issuer_url: string }`. This is the exact
  shape the new `aws-eks` module must output.
- `config/network.yaml` (schema-validated, `id`/`source`/`destination`/
  `protocol`/`port`/`scope` per rule) is the single network contract. Only
  one rule is AWS-relevant today: `aws-object-storage`
  (`signal-backend -> aws-s3`, tcp/443, scope `private`, purpose "S3 via
  private service connectivity"). The other three rules (`local-gateway`,
  `remote-gateway`, `backend-object-storage`) describe Docker/self-hosted
  traffic Terraform does not provision.
- `docs/01-architecture.md`'s "Storage amendments" section confirms MinIO was
  dropped for SeaweedFS, and that Docker/self-hosted SeaweedFS and k3s
  bootstrap are Ansible/Helm-owned; "Terraform never competes to manage
  these local resources." This is why `environments/self-hosted-k8s` and
  `environments/docker` apply zero resources rather than reintroducing a
  `minio-backend`-style module (see user clarification recorded in this
  change's history: scope = "AWS modules/root, plus minimal placeholder
  roots documenting the Ansible/Helm boundary").
- No `terraform`/AWS binary is available in this sandbox (confirmed during
  the prior `establish-architecture-contracts` change). Validation here is
  limited to `terraform validate`/`terraform test` with mocked providers,
  the same evidence standard the existing storage facade already uses, plus
  ordinary Python unit tests where a Terraform CLI isn't required at all.

## Goals / Non-Goals

**Goals:**
- Make the existing storage root's documented prerequisites (EKS, OIDC,
  state-bootstrap) actually exist and independently applicable.
- Derive AWS security-group rules from `config/network.yaml` so the network
  contract stays the single source of truth for ports (per the compatibility
  matrix's existing "Consumers resolve pins from the single matrix" pattern,
  extended here to network rules, not version pins).
- Formally close the gap between Plan.md's literal task list (which still
  names a superseded `minio-backend` module) and the already-decided
  SeaweedFS/Ansible-ownership architecture, so a future reader isn't misled
  by Plan.md's original wording.

**Non-Goals:**
- No Helm installation, Kubernetes manifest, or Kafka/Strimzi deployment
  (Phase 3/5/6 per `Plan.md`'s execution order).
- No automated cross-root wiring (e.g. remote-state data sources) between
  `environments/aws` and `environments/aws-storage`; both remain
  independently applied roots, consistent with the existing storage root's
  "operator-supplied input" pattern.
- No real `terraform apply` against AWS in this change; evidence is static
  validation and mocked plan-time tests only, exactly as Phase 1 delivered
  for the storage facade.
- No VM/firewall provisioning for self-hosted/Docker profiles - those stay
  entirely out of Terraform's scope, now and later.

## Decisions

**State-bootstrap root uses its own local state, not a special-cased remote
backend.** There is no earlier root to bootstrap it from, so
`terraform/environments/aws-state-bootstrap` has no `backend "s3" {}` block
and is initialized/applied with local state once, by design. Its README
documents this explicitly as the one deliberate exception to "never use
local state for a real deployment." Alternative considered: a Terraform
module invoked via a wrapper script that writes bootstrap state to a
well-known local path and warns on every run - rejected as unnecessary
ceremony for a root that is applied once per account and then left alone.

**Security groups are generated from a `network_rules` variable whose
default value is read from `config/network.yaml` at the environment root,
filtered to AWS-relevant scopes.** The `aws-vpc-network` module takes a
typed `list(object({ id, source, destination, protocol, port, scope }))`
variable instead of reading the YAML file itself (modules stay
provider/file-system-input-free, consistent with how `object-storage`
receives typed variables rather than reading `config/*.yaml` directly). The
`environments/aws` root is responsible for mapping `config/network.yaml`'s
`aws-object-storage` rule into that variable (literally, since there is
exactly one applicable rule today) and documents that a future rule must be
added to that mapping by hand, with a unit test (see Decisions below on
testing) asserting the mapping's rule count matches the network contract's
AWS-scoped rule count - so a newly added AWS-relevant rule in
`config/network.yaml` that the Terraform mapping forgets to mirror is
caught, not silently dropped.

**AWS S3 access from the cluster uses a VPC Gateway Endpoint for S3, not a
public-internet security-group rule to `0.0.0.0/0:443`.** This matches the
network contract's own purpose text ("S3 via private service connectivity")
and avoids opening broad internet egress just to reach one AWS service.
Security-group egress for the `signal-backend` identity is scoped to the S3
prefix list associated with that VPC endpoint.

**EKS module creates generically-named node groups (`stateful`,
`stateless`), not one node group per backend.** Per-backend/per-namespace
workload placement is a Helm/Kubernetes-manifest concern (Phase 3/6), not a
Terraform infrastructure concern; the infrastructure only needs to provide
the two capacity classes Plan.md distinguishes (on-demand for stateful,
optional spot for stateless). The `stateless` node group's capacity type is
an operator-supplied variable defaulting to on-demand, so spot is opt-in
rather than assumed.

**`environments/self-hosted-k8s` and `environments/docker` are enforced
empty by a Python test, not a Terraform-native mechanism.** Terraform has no
built-in "this root must declare zero resources" assertion, and installing
`terraform-compliance` or a similar policy tool is a new dependency this
change doesn't otherwise need. Instead, a unit test (parallel to the
existing `tests/test_versions.py` style, no Terraform binary required) scans
each root's `.tf` files for `resource "..." "..." {` blocks and fails the
suite if one is found, with a comment in each root's `main.tf` pointing back
at this test and the architecture doc section it enforces. This keeps the
guardrail inside the same `python -m unittest discover` gate every other
contract in this repository is verified against.

**`environments/aws` is a thin composition root, not a module.** It
instantiates `aws-vpc-network` and `aws-eks` as modules (matching
`aws-storage`'s existing pattern of a thin root wrapping the
`object-storage` module) and declares its own `backend "s3" {}` block
pointed at the bootstrapped backend, exactly like `aws-storage` does today.

## Risks / Trade-offs

- [Risk] Manual copy of `oidc_provider_arn`/`oidc_issuer_url`/subnet outputs
  from `environments/aws` into `environments/aws-storage`'s tfvars can drift
  or be forgotten. → Mitigation: document the exact `terraform output -json`
  command and target variable names in both READMEs; this mirrors the
  already-accepted risk/pattern for the storage root's existing inputs, so
  it adds no new category of operator error.
- [Risk] No real Terraform binary or AWS account in this sandbox means
  `terraform validate`/`terraform test` here only proves internal
  consistency, not real provisioning. → Mitigation: state this limitation
  explicitly in each new README, matching the existing storage root's own
  disclosed limitation.
- [Risk] The EKS-managed Kubernetes control-plane version and EBS CSI addon
  version are new pins with their own deprecation/EOL cadence, independent
  of the other matrix entries. → Mitigation: the existing pin-change review
  gate (`runtime_verified` reset requirement) and `check-pins` drift
  detection already generalize to these new fields without code changes;
  only data entries and `tracked_consumers` rows are added.
- [Risk] The hand-maintained mapping from `config/network.yaml` to the
  Terraform `network_rules` variable can fall out of sync as new AWS-scoped
  rules are added. → Mitigation: the rule-count-parity unit test described
  above fails loudly rather than silently under-provisioning or
  over-provisioning security-group rules.

## Migration Plan

This is new infrastructure, not a migration of existing resources. Intended
one-time operator sequence (documented in each root's README, not automated
across roots):

1. Apply `aws-state-bootstrap` once per AWS account (local state).
2. Initialize and apply `environments/aws` against the bootstrapped backend.
3. Copy its OIDC/subnet outputs into `environments/aws-storage`'s tfvars and
   apply that root (already-existing behavior, unchanged by this change).
4. `environments/self-hosted-k8s` and `environments/docker` can be validated
   at any time; they have no ordering dependency since they apply nothing.

Rollback: ordinary teardown of `environments/aws` must not affect
`aws-state-bootstrap` (destroy-protected, separate root/state); teardown
order and limitations are documented per root, matching the existing
storage root's teardown documentation.
