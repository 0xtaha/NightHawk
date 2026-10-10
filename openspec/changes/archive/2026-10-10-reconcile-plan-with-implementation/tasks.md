# Tasks

The unit test command throughout is
`python -m unittest discover -s tests -p "test_*.py"`.

## 1. Terraform tooling and test collection

- [x] 1.1 Add a `fetch-tools` command (`nighthawk/tools.py` and `nighthawk/__main__.py`; the earlier one was removed with the `sops` and `age` downloads) that downloads the pinned Terraform archive for Linux, verify it against `validation_tools.terraform.sha256` in `config/versions.yaml`, and install it into `.tools/`; add unit tests in `tests/test_tools.py` for a verified download and a checksum mismatch that installs nothing, and verify they pass
- [x] 1.2 Add `tests/terraform/__init__.py` and replace the `RuntimeError` in `tests/terraform/test_storage_contract.py` with a skip that states the pinned Terraform is absent; verify the unit test command lists the Terraform tests as skipped with that reason when `terraform` is not on `PATH`
- [x] 1.3 Run `fetch-tools`, then run `terraform init -backend=false`, `terraform validate`, and `terraform test` in each existing module and root; record which pass before any change; if the contract test fails because the example platform document is now schema version 2, fix how the test builds its input; and verify the Terraform tests now run instead of skipping
- [x] 1.4 Document `fetch-tools` for Terraform and the skip behaviour in the prerequisites section of `docs/02-configuration.md`; verify the documented commands run as written

## 2. AWS compute: access, encryption, security groups

- [x] 2.1 Add `endpoint_public_access`, `public_access_cidrs`, and `cluster_admin_principal_arns` to `terraform/modules/aws-eks` with validation that rejects a public endpoint with no CIDRs or with `0.0.0.0/0`, and an empty administrator list; configure API authentication mode with the creator's bootstrap admin permission disabled and one access entry per principal; add `tftest` runs for private-only, public with an allowlist, public without one, and no administrators, and verify they pass
- [x] 2.2 Add Kubernetes secrets encryption with an operator-supplied or module-created KMS key (rotation enabled); add `tftest` runs for both cases and verify they pass
- [x] 2.3 Give each node group a launch template with an encrypted root volume and the cluster security group plus the contract-derived group passed in from `aws-vpc-network`; remove the unused `vpc_id` variable; wire the group IDs in `terraform/environments/aws/main.tf`; add `tftest` assertions for encryption and attached groups and verify they pass
- [x] 2.4 Add `kubernetes.io/role/elb` and `kubernetes.io/role/internal-elb` tags to the public and private subnets in `aws-vpc-network`; add a `tftest` assertion and verify it passes
- [x] 2.5 Rewrite the parity tests in `tests/test_terraform_boundaries.py` to compare every declared field of each mirrored rule with `config/network.yaml` and report the rule ID and field; verify by temporarily changing one mirrored port that the test fails naming it, then restore
- [x] 2.6 Update the `aws-eks`, `aws-vpc-network`, and `environments/aws` READMEs and `terraform.tfvars.example` for the new inputs, the node group replacement, and what remains plan-only; verify `terraform validate` passes in the root with the example values

## 3. AWS compute: controller identities

- [x] 3.1 Move the EBS CSI driver to its own IRSA role bound through the addon's service account role, and remove `AmazonEBSCSIDriverPolicy` from the node role; add a `tftest` assertion that the node role has no volume-management policy and verify it passes
- [x] 3.2 Add optional IRSA roles for DNS record management and certificate DNS-01 validation, each taking a service account subject and a non-empty list of hosted zone IDs, with validation that rejects an enabled controller with no zones; add `tftest` runs for scoped, unscoped, and disabled cases and verify they pass
- [x] 3.3 Add an optional IRSA role for the node autoscaler whose write actions are conditioned on this cluster's node group tags; add a `tftest` run and verify it passes
- [x] 3.4 Output each controller role ARN from the module and the root, document them in the READMEs, and correct `docs/01-architecture.md` so its AWS ownership sentence lists what is built and names the load balancer controller's IAM as deferred to phase 6; verify the outputs appear in `terraform test` output

## 4. Quickstart publication and credential rotation

- [x] 4.1 Reject a non-loopback `--bind-address` in the quickstart prerequisite check with a message naming the certificate-free local entry point; add unit tests in `tests/test_quickstart.py` for `127.0.0.1`, `::1`, and `0.0.0.0`, and verify they pass
- [x] 4.2 Publish the gateway on the local entry point's declared port: write the port to the Compose environment file, use it in the `traefik` port mapping and `GF_SERVER_ROOT_URL` in `docker-compose/docker-compose.yaml`, and update `.env.example`; extend `tests/test_compose.py` and `tests/test_quickstart.py` and verify they pass
- [x] 4.3 Add a repeatable `--credential` option to `provision-grafana`; make `grafana.desired_state` fail listing the datastream and candidates when a datastream has several query credentials and none is named, and fail on an undeclared or non-query credential; add unit tests in `tests/test_grafana.py` for the four scenarios and verify they pass
- [x] 4.4 Add a repeatable `--credential` option to `quickstart-docker` that routes ingestion credential IDs to the collector render and query credential IDs to Grafana provisioning, failing before any change when an overlap has no choice or a named credential is not the sample datastream's; add unit tests and verify they pass
- [x] 4.5 Make the quickstart pass `--update-secrets` to Grafana provisioning only when a query credential's materialized secret changed in this run; add unit tests for the changed and unchanged cases and verify they pass
- [x] 4.6 Rewrite the rotation runbook in `docs/05-gateway.md` and the `--update-secrets` passage in `docs/06-tenant-provisioning.md` around the new options, and document the loopback restriction in `docs/00-quickstart.md` and `docs/07-docker-compose.md`; verify every documented command and flag exists in `python -m nighthawk --help` output
- [x] 4.7 With a bootstrapped dev-mode Vault and the stack running, walk the runbook once for an ingestion credential and once for a query credential (declare second, re-run with it named, remove first, re-run) and verify ingestion and a Grafana-proxied query succeed after each step; then rotate a query secret in place, re-run, and verify the Grafana-proxied query still succeeds

## 5. Collection: redaction and host collection

- [x] 5.1 Make the label-drop expression in the metrics, log-label, and profile pipelines of `nighthawk/collector.py` match each drop field and its sanitized form; add a unit test in `tests/test_collector.py` for `user.email` and verify it passes
- [x] 5.2 Add a dotted drop field to one datastream in `tests/e2e/platform.yaml` and to the fixture expectations, and verify `tests/test_fixtures.py` passes
- [x] 5.3 Configure the node exporter in `alloy-configs/docker/metrics.alloy` with the mounted host root, `proc`, and `sys` paths matching the `alloy-host` mounts, and verify `alloy fmt` and `alloy validate` from `.tools/` accept a rendered `docker` profile
- [x] 5.4 Add `--host-collection` to `quickstart-docker`: render the full `docker` profile into the directory `alloy-host` mounts, activate the Compose profile, and refuse the option under rootless Podman before any change; add unit tests for requested, not requested, and refused, and verify they pass
- [x] 5.5 Update `docs/04-collection.md` (sanitized matching, removal of the "no collector has been run" statement, the `otelcol.auth.basic` table row) and the `host-collection` row in `docs/07-docker-compose.md`, keeping host collection listed as not runtime-verified; verify the statements against the code and the recorded observations

## 6. Compatibility matrix

- [x] 6.1 Add an optional `app_version_exception` with a required `reason` to chart entries in `config/versions.schema.json`, and a validation in `nighthawk/config.py` that fails on an unrecorded chart/backend difference and on a stale exception; add unit tests in `tests/test_versions.py` for the four scenarios and verify they pass
- [x] 6.2 Record the Mimir chart's weekly-build difference as an exception; for Tempo, check upstream for a chart release whose application version is 3.0.3 and pin it with its source reference, or record an exception with the reason if none exists; verify `python -m nighthawk check-pins` and matrix validation pass
- [x] 6.3 Update the compatibility passage in `docs/01-architecture.md` to the matrix's present state (pins, Compose digests, runtime-verified flags) and add a table of licence and distribution registry for each Compose image; verify each stated pin against `config/versions.yaml` and that `docs/02-configuration.md` still quotes the real `check-pins` output

## 7. Runtime evidence at the Docker tier

- [x] 7.1 Set different retention per signal within at least one pair in `tests/e2e/platform.yaml`, and extend the retention test in `tests/e2e/test_stack.py` to assert each pair's value per signal on Mimir, Tempo, and Pyroscope; remove the unused Loki capture
- [x] 7.2 Extend the isolation test to attempt cross-tenant reads of metrics, traces, and profiles for every reader and writer pair, with all four pairs writing
- [x] 7.3 Add a test that every provisioned correlation target resolves to an existing data source UID in the same Grafana organization
- [x] 7.4 Extend the tenant-enforcement test to a request without a tenant against Pyroscope
- [x] 7.5 Make the metrics redaction check assert that the run's fixture series were returned before asserting that markers are absent, and query `target_info` for resource-attribute markers
- [x] 7.6 Add a test that restarts Mimir, Tempo, and Pyroscope one at a time and queries that backend's fixtures afterwards
- [x] 7.7 Run `NIGHTHAWK_E2E=1 python -m unittest tests.e2e.test_stack` against a freshly started stack and verify every case passes; fix any defect it exposes in the code under test, not in the assertion
- [x] 7.8 Update the observed-at-runtime sections of `docs/05-gateway.md`, `docs/06-tenant-provisioning.md`, and `docs/07-docker-compose.md` to what 7.7 showed, including the case count, removing the claim that Loki's loaded overrides were observed and the "nothing has been run behind a real Traefik" statement; verify each claim against the test that supports it

## 8. Plan and remaining document reconciliation

- [x] 8.1 Add amendment notes to `Plan.md`, in the style of the existing storage amendment: the "Objective and current state" section describes the state before implementation; the MinIO wording in the Docker profile, phase 1, phase 2, and phase 6 reads as SeaweedFS; verify no unamended MinIO mention remains with `grep -n -i minio Plan.md`
- [x] 8.2 Add a "Recorded deferrals" amendment to `Plan.md` naming each item from this change's design Non-Goals with the phase that owns it, and mirror the list in `docs/01-architecture.md`; verify every Non-Goal in `design.md` appears in both
- [x] 8.3 Correct `docs/01-architecture.md` where it says firewall inputs are generated (only the port table and JSON are) and where it calls the pins candidates; correct the Terraform node label in `docs/03-diagrams.md`; verify against `python -m nighthawk render-contracts` output
- [x] 8.4 Remove the `kms_key_id` line from `terraform/environments/aws-storage/backend.hcl.example`, or document in that root's README where the key comes from if an operator supplies one; verify the example matches what `aws-state-bootstrap` outputs
- [x] 8.5 Add a short root `README.md` that states what the repository is and links `docs/00-quickstart.md` and `Plan.md`; verify the links resolve

## 9. Integration check

- [x] 9.1 Run the unit test command and verify every test passes with no Terraform test skipped; the Vault integration tests are expected to skip unless pointed at a dev-mode Vault
- [x] 9.2 Run `openspec validate --specs --strict` and `python -m nighthawk validate --config config/tenants.example.yaml`, and verify both succeed
- [x] 9.3 With a bootstrapped dev-mode Vault, run `quickstart-docker` from a clean runtime directory, then again, and verify the second run reports no Grafana changes and both end healthy on `127.0.0.1`
