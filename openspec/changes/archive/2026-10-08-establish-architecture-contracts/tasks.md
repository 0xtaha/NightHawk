# Tasks

## 1. Compatibility matrix schema and completion

- [x] 1.1 Add `config/versions.schema.json` (schema_version, per-component `version`/`source`/`runtime_verified` fields, OS distribution/architecture matrix section, `tracked_consumers` list) and verify it validates against the JSON Schema meta-schema
- [x] 1.2 Extend `nighthawk/config.py` (or a small new helper) to load and validate `config/versions.yaml` against the new schema, reusing `load_document`, and verify a unit test in `tests/test_config.py` (or a new `tests/test_versions.py`) covers a rejected unknown field and a rejected malformed version string
- [x] 1.3 Update `config/versions.yaml` with pinned, source-referenced entries for k3s, Cilium, MetalLB, Longhorn, Traefik, cert-manager, Strimzi + its Kafka version, and the Grafana Helm charts in use, each with `runtime_verified: false`, and verify the file validates against the new schema
- [x] 1.4 Add SOPS and age pins (version + source reference) to `config/versions.yaml` and verify they validate against the new schema
- [x] 1.5 Add the supported OS distribution/version/architecture matrix (Docker hosts vs. k3s/Longhorn nodes, declared separately) to `config/versions.yaml` and verify a unit test confirms an undeclared target is reported unsupported
- [x] 1.6 Implement the pin-change review gate (reject a version change on a `runtime_verified: true` component unless the same update resets that flag to `false`, mirroring `validate_migration`) and verify unit tests cover both the rejected and accepted cases
- [x] 1.7 Implement `nighthawk check-pins` to compare each `tracked_consumers` entry (starting with `terraform/modules/object-storage/versions.tf` and `terraform/modules/aws-s3-backends/versions.tf` against the Terraform/AWS-provider pins) against the matrix and verify a unit test covers both a matching and a deliberately diverged consumer file

## 2. Secrets workflow prerequisites

- [x] 2.1 Add `nighthawk doctor` that runs `sops --version` / `age --version`, parses the result, and compares it to the matrix pins from task 1.4, and verify a unit test covers the matching, missing-binary, and version-mismatch cases using a stubbed subprocess
- [x] 2.2 Wire every new secrets-workflow subcommand (added in section 3) to call the doctor check first and verify a unit test confirms a failing doctor check prevents any file from being written

## 3. Secrets workflow implementation

- [x] 3.1 Add `nighthawk/secrets.py` with `generate_recipient` (age key pair generation, refuses overwrite without an explicit flag) and wire a `generate-recipient` CLI subcommand; verify unit tests cover fresh generation and the overwrite-refusal case
- [x] 3.2 Implement `encrypt_secret` (writes a SOPS-encrypted file under `secrets/` for a given `file`/`key` pair and recipients) and wire an `encrypt-secret` subcommand; verify unit tests cover successful encryption and the empty-recipient-list rejection
- [x] 3.3 Implement `rotate_secret` (re-encrypts an existing `file`/`key` pair with a new value, same recipients, same reference) and wire a `rotate-secret` subcommand; verify unit tests cover successful rotation and rejection when the target secret does not exist
- [x] 3.4 Implement `materialize` (decrypts all secrets referenced by a validated platform document into `.materialized-secrets/`, created with owner-only permissions) and wire a `materialize-secrets` subcommand; verify unit tests cover materialization against a valid document and refusal against an invalid one
- [x] 3.5 Implement `cleanup` (removes decrypted contents of `.materialized-secrets/`) and wire a `clean-secrets` subcommand; verify a unit test confirms no residual decrypted material remains after cleanup
- [x] 3.6 Add the production-profile guard rejecting locally auto-generated age recipients for `profile: production` documents unless explicitly confirmed, and verify unit tests cover the rejection and the explicit-confirmation acceptance paths
- [x] 3.7 Confirm `validate` and `render-contracts` perform no decryption by adding a regression test that runs them against a platform document whose `secrets/` files are absent and asserts success/failure depends only on reference consistency
- [x] 3.8 Add `.materialized-secrets/` to `.gitignore` and verify `git status` shows no tracked files after running `materialize-secrets` in a scratch checkout

## 4. Documentation

- [x] 4.1 Update `docs/01-architecture.md`'s "Shared configuration" section to describe the implemented SOPS + age workflow in place of the current "planned secret lifecycle" / "separate implementation gate" language, and verify the documented commands match the implemented CLI subcommands
- [x] 4.2 Update `docs/02-configuration.md` to document the new `generate-recipient`, `encrypt-secret`, `rotate-secret`, `materialize-secrets`, `clean-secrets`, `doctor`, and `check-pins` commands (usage, prerequisites, failure modes) and verify each documented command runs as written against the example tenant configuration
- [x] 4.3 Update `docs/03-diagrams.md`'s "Control" subgraph label for `Secrets` from "planned secret lifecycle" to reflect implemented status, consistent with the `implemented` styling already used for `Config`/`Renderer`/`Terraform`

## 5. Integration verification

- [x] 5.1 Run the full test suite (`python -m unittest discover -s tests -p "test_*.py"`) and verify all tests pass, including the new secrets, versions, and doctor tests
- [x] 5.2 Run `openspec validate establish-architecture-contracts --strict` and verify it reports the change as valid
