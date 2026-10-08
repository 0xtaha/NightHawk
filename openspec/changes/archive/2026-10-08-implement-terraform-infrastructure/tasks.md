# Tasks

## 1. Compatibility matrix additions

- [x] 1.1 Add `kubernetes_platform.eks` (`version: "1.37"`, source:
      https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html,
      `runtime_verified: false`) and `kubernetes_platform.ebs_csi_driver`
      (`version: 1.66.0`, source:
      https://github.com/kubernetes-sigs/aws-ebs-csi-driver/releases/tag/v1.66.0,
      `runtime_verified: false`) to `config/versions.yaml`, and verify
      `python3 -m nighthawk check-pins` (or the equivalent loader call) still
      parses the file against `config/versions.schema.json` with no errors.
- [x] 1.2 Extend `config/versions.schema.json` if the new fields aren't
      already permitted by the existing `kubernetes_platform` object schema,
      and verify `tests/test_versions.py`'s schema-validation test still
      passes with the new fields present.
- [x] 1.3 Update `tests/test_versions.py` (or add a new test) asserting both
      new components exist with non-empty `version`/`source` fields, and
      verify the test fails if either field is removed (run it once against
      a deliberately-reverted copy to confirm it actually catches the
      regression, then restore).

## 2. State-bootstrap root

- [x] 2.1 Create `terraform/environments/aws-state-bootstrap/` with
      `main.tf` (S3 bucket with versioning + encryption, DynamoDB-free S3
      native locking per Terraform 1.13's `use_lockfile` state-locking
      support), `variables.tf`, `versions.tf` pinned to Terraform `1.13.5`
      and `hashicorp/aws` `6.12.0` matching the existing modules, and a
      `README.md` documenting that this root deliberately uses local state
      (one-time, per-account bootstrap) and the exact `terraform output`
      commands an operator copies into other roots' `backend.hcl`. Verify
      with `terraform -chdir=terraform/environments/aws-state-bootstrap
      validate` (or `fmt -check` if the `validate` subcommand needs cloud
      credentials it doesn't have).
- [x] 2.2 Add a `tracked_consumers` entry in `config/versions.yaml` for this
      root's `versions.tf` (both the `terraform` and `aws_provider` regex
      patterns, matching the existing two modules' entries) and verify
      `nighthawk check-pins` reports the new file as in sync.
- [x] 2.3 Document the documented S3-lockfile migration requirement from the
      `terraform-state-bootstrap` spec (what happens if an operator must
      move from local to the bootstrapped backend, or migrate the bootstrap
      bucket itself) in the README, and verify the spec's "documented
      migration" scenario is satisfied by re-reading the written README
      against that scenario's text.

## 3. AWS VPC and network-contract-derived security groups

- [x] 3.1 Create `terraform/modules/aws-vpc-network/` (`main.tf`,
      `variables.tf`, `outputs.tf`, `versions.tf` pinned to `1.13.5`/
      `6.12.0`) provisioning a multi-AZ VPC (public + private subnets across
      at least two AZs), an S3 VPC Gateway Endpoint, and a
      `network_rules` input variable typed
      `list(object({ id = string, source = string, destination = string,
      protocol = string, port = number, scope = string }))` used to
      generate security-group rules (scope `private`/`restricted-external`
      mapped to restricted CIDR/prefix-list sources, `loopback` rejected
      with a precondition since it is never AWS-relevant).
- [x] 3.2 Add `terraform/modules/aws-vpc-network/tests/network.tftest.hcl`
      using `mock_provider "aws"` (same pattern as
      `aws-s3-backends/tests/storage.tftest.hcl`) asserting: the module
      plans successfully with the one real `aws-object-storage` rule from
      `config/network.yaml`, the S3 endpoint plan includes the expected
      prefix-list reference, and a rule with `scope = "loopback"` fails
      the plan via the precondition. Verify with
      `terraform -chdir=terraform/modules/aws-vpc-network test`.
- [x] 3.3 Add a `tracked_consumers` entry for this module's `versions.tf` in
      `config/versions.yaml` and verify `nighthawk check-pins` passes.
- [x] 3.4 Write `terraform/modules/aws-vpc-network/README.md` documenting
      the module's inputs/outputs and explicitly stating it consumes a
      typed variable, not `config/network.yaml` directly, and that the
      calling root owns mapping the YAML file to that variable.

## 4. AWS EKS cluster and OIDC/IRSA wiring

- [x] 4.1 Create `terraform/modules/aws-eks/` (`main.tf`, `variables.tf`,
      `outputs.tf`, `versions.tf` pinned to `1.13.5`/`6.12.0`) provisioning
      an EKS cluster pinned to Kubernetes `1.37`, an IAM OIDC identity
      provider for the cluster's issuer, the EBS CSI driver addon pinned to
      `v1.66.0`, and two managed node groups (`stateful`: on-demand only;
      `stateless`: capacity type an operator-supplied variable defaulting
      to on-demand).
- [x] 4.2 Expose `outputs.tf` with `oidc_provider_arn` and `oidc_issuer_url`
      whose shape exactly matches `aws-s3-backends`'s `aws_identity`
      variable (`{ oidc_provider_arn: string, oidc_issuer_url: string }`),
      and verify by comparing the output block's attribute names against
      `terraform/modules/aws-s3-backends/variables.tf`'s `aws_identity`
      type definition line by line.
- [x] 4.3 Add
      `terraform/modules/aws-eks/tests/cluster.tftest.hcl` with
      `mock_provider "aws"` asserting the cluster plans with the pinned
      Kubernetes version, the OIDC provider output matches the expected
      shape, and the EBS CSI addon version matches the pinned value. Verify
      with `terraform -chdir=terraform/modules/aws-eks test`.
- [x] 4.4 Add a `tracked_consumers` entry for this module's `versions.tf`,
      plus two more entries matching the EKS cluster `version = "1.37"` and
      EBS CSI addon `addon_version` literals against
      `kubernetes_platform.eks.version` and
      `kubernetes_platform.ebs_csi_driver.version` respectively, and verify
      `nighthawk check-pins` passes.
- [x] 4.5 Write `terraform/modules/aws-eks/README.md` documenting inputs,
      outputs, and the exact output-shape contract with `aws-s3-backends`.

## 5. `environments/aws` composition root

- [x] 5.1 Create `terraform/environments/aws/` (`main.tf` composing
      `aws-vpc-network` and `aws-eks`, `variables.tf`, `backend.hcl.example`
      pointed at the bootstrapped S3 backend, `terraform.tfvars.example`,
      `versions.tf` pinned to `1.13.5`/`6.12.0`) that maps the single
      `aws-object-storage` rule from `config/network.yaml` into the
      `network_rules` variable literally, as decided in design.md.
- [x] 5.2 Add a Python unit test (e.g. `tests/test_terraform_boundaries.py`
      or an addition to `tests/test_config.py`) asserting the count of
      AWS-scoped rules (`scope != "loopback"`) in `config/network.yaml`
      equals the count of entries mapped in
      `terraform/environments/aws/main.tf`'s `network_rules` literal, so a
      newly added AWS-relevant rule that isn't mirrored fails the suite.
      Verify by temporarily adding a rule to a copied fixture and confirming
      the test fails, then restore.
- [x] 5.3 Add a `tracked_consumers` entry for this root's `versions.tf` and
      verify `nighthawk check-pins` passes.
- [x] 5.4 Write `terraform/environments/aws/README.md` documenting the
      apply order relative to `aws-state-bootstrap` and
      `aws-storage` (copy-paste `terraform output` commands for
      `oidc_provider_arn`/`oidc_issuer_url`/subnet IDs), matching the level
      of detail in `terraform/environments/aws-storage/README.md`.

## 6. Self-hosted/Docker placeholder environment roots

- [x] 6.1 Create `terraform/environments/self-hosted-k8s/main.tf` and
      `terraform/environments/docker/main.tf`, each containing only a
      `terraform {}`/`versions.tf` block (pinned to `1.13.5`/`6.12.0` for
      consistency) and a top comment stating that SeaweedFS and k3s/Docker
      provisioning for this profile is owned by Ansible/Helm per
      `docs/01-architecture.md`'s Storage amendments section, with no
      `resource` blocks.
- [x] 6.2 Add `tests/test_terraform_boundaries.py` (or extend the file from
      task 5.2) with a test that scans both roots' `.tf` files for
      `resource "..." "..." {` blocks and fails if any are found. Verify by
      temporarily adding a dummy `resource` block to one root, confirming
      the test fails, then removing it.
- [x] 6.3 Write a `README.md` in each of the two roots documenting the
      zero-resource boundary and pointing at the guard test from 6.2 as the
      enforcement mechanism, satisfying the `terraform-environment-
      boundaries` spec's "documented ownership boundary" requirement.

## 7. Documentation and architecture alignment

- [x] 7.1 Update `docs/01-architecture.md`'s AWS provisioning paragraph and
      deployment-profile table to name the new `aws-state-bootstrap`,
      `aws` (VPC+EKS), `self-hosted-k8s`, and `docker` environment roots
      alongside the existing `aws-storage` root, and verify by re-reading
      the updated section against this change's `proposal.md` "What
      Changes" list for completeness.
- [x] 7.2 Update `docs/03-diagrams.md` to add the new Terraform roots to
      the existing architecture diagram(s) with `classDef implemented`
      styling (matching how the secrets lifecycle was marked implemented in
      the prior change), and verify the diagram still renders (no Mermaid
      syntax errors) via the project's existing diagram-check method (or
      manual Mermaid lint if no automated check exists).
- [x] 7.3 Add a short note to `Plan.md`'s Phase 2 section (or an adjacent
      changelog/amendment note, following the existing pattern used for the
      storage amendments) recording that the original `minio-backend`
      module described there was superseded by the SeaweedFS/Ansible-owned
      decision and was intentionally not built here, cross-referencing
      `docs/01-architecture.md`'s Storage amendments section.

## 8. Final validation

- [x] 8.1 Run the full Python test suite
      (`python3 -m unittest discover -s tests -p "test_*.py"`) and verify
      all tests pass, including the new compatibility-matrix and
      zero-resource-boundary tests.
- [x] 8.2 Run `terraform fmt -check -recursive terraform/` and
      `terraform validate`/`terraform test` (where credentials-free
      validation is possible) across every new module and root, and record
      any check that cannot run in this environment (e.g. missing AWS
      credentials) as an explicit, documented limitation rather than a
      silently skipped step.
- [x] 8.3 Run `openspec validate implement-terraform-infrastructure --strict`
      and verify it reports valid.
