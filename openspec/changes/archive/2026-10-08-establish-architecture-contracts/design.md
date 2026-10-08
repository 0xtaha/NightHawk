# Design

## Context

`nighthawk/config.py` already validates that every `secret_ref` in a platform
document resolves to a known `file`/`key` pair and that `secrets/*.sops.yaml`
/ `secrets/*.sops.json` are the only files git will track under `secrets/`
(see `.gitignore`). No code reads, writes, encrypts, or decrypts those files
yet. `config/versions.yaml` is a flat, schema-less YAML document covering
five components; `docs/01-architecture.md` already states SOPS + age as the
chosen secret-management baseline, so this design implements that choice
rather than re-litigating it. See `proposal.md` - Why for the full gap
analysis.

The existing Python code follows a consistent style: frozen dataclasses,
strict JSON-Schema (`Draft202012Validator`) validation with
`additionalProperties: false`, explicit `ConfigurationError` on any invalid
input, no silent fallbacks, and `argparse` subcommands wired through
`nighthawk/__main__.py`. This design extends that style rather than
introducing a new one.

## Goals / Non-Goals

**Goals:**
- Give the platform a working, tested SOPS + age secret lifecycle (generate
  recipients, encrypt, rotate, materialize, clean up) that CLI commands and
  CI can call.
- Make `config/versions.yaml` a schema-validated, complete pin inventory for
  every component phases 2-9 will need, with a lightweight drift check
  against files that reference a pinned version.
- Keep the existing non-secret `validate`/`render-contracts` commands
  decryption-free, exactly as `docs/02-configuration.md` already documents.

**Non-Goals:**
- Implementing the gateway authentication policy, Grafana organization
  provisioning, or any runtime consumer of materialized secrets (Phase 3).
- Writing the actual Ansible roles, Helm values, or Terraform modules that
  will *consume* the compatibility matrix pins (Phases 2, 5, 6) - this change
  only adds the matrix entries and the drift check contract; wiring every
  future consumer file is out of scope until those files exist.
- Choosing or implementing a secrets manager other than SOPS + age (already
  an architecture decision, not reopened here).
- A GUI, web UI, or remote secret service - this is local/CI tooling only,
  invoked like the existing `nighthawk validate` command.

## Decisions

### Secrets workflow as a new `nighthawk.secrets` module + CLI subcommands

Add `nighthawk/secrets.py` alongside `nighthawk/config.py` and
`nighthawk/retention.py`, exposing `generate_recipient`, `encrypt_secret`,
`rotate_secret`, `materialize`, and `cleanup` functions. Wire them as new
`nighthawk` subcommands (`generate-recipient`, `encrypt-secret`,
`rotate-secret`, `materialize-secrets`, `clean-secrets`) rather than a
separate CLI entry point, so prerequisite checks, error handling, and the
existing `ConfigurationError` convention are reused.

Alternative considered: a standalone shell wrapper around the `sops`/`age`
binaries. Rejected because it would duplicate the platform-document parsing
`nighthawk/config.py` already implements (to resolve which `secrets/` files a
given platform document needs) and would not share the project's test
harness or error-handling conventions.

### Shell out to `sops` and `age`/`age-keygen` binaries rather than reimplementing the formats

The module invokes the pinned `sops` and `age`/`age-keygen` executables as
subprocesses (captured output, explicit exit-code checks, no shell
interpolation of untrusted input) instead of reimplementing the age
encryption format or SOPS' file format in Python.

Alternative considered: a pure-Python age/SOPS implementation. Rejected: it
would duplicate security-sensitive cryptographic code the upstream,
independently audited tools already provide, and would drift from the exact
tool behavior the compatibility matrix pins.

### Prerequisite check resolves expected versions from `config/versions.yaml`

`nighthawk doctor` (new subcommand) runs `sops --version` / `age --version`,
parses the reported version, and compares it against the pin recorded for
that tool in the (now schema-validated) compatibility matrix - the same
matrix this change also extends. Every secrets-workflow command calls this
check first and fails before touching any file if it does not pass.

### Compatibility matrix gets its own JSON Schema, mirroring `platform.schema.json` / `network.schema.json`

Add `config/versions.schema.json` and validate `config/versions.yaml` against
it (reusing `nighthawk.config.load_document`), rather than leaving it as
untyped YAML. Each component entry requires `version` and a `source`
reference field; `runtime_verified` becomes a required boolean per
component (defaulting behavior is explicit, not implied by absence).

### Pin-change review gate implemented as a required-flag-reset validation rule, not a git hook

Enforced inside the schema-validation code path (comparing a new document to
an explicitly supplied `--previous` versions file, mirroring the existing
`validate_migration` pattern for platform documents) rather than as a
pre-commit/CI-only git hook. This keeps the rule enforceable the same way
locally and in CI, consistent with how backend-ID migration checks already
work.

### Drift check is a new `nighthawk check-pins` command, not a general-purpose linter

Given only two automation files reference pinned versions directly today
(`terraform/modules/object-storage/versions.tf`,
`terraform/modules/aws-s3-backends/versions.tf`, both for the Terraform/AWS
provider pins already in the matrix), the design keeps the drift checker
narrow: it reads an explicit, version-controlled list of
`(file path, extraction pattern, matrix component key)` entries from
`config/versions.yaml` itself (a `tracked_consumers` section) and fails if the
extracted value disagrees with the matrix pin. Future Ansible/Helm consumers
register themselves by adding an entry to that list when they are written,
rather than this change attempting to anticipate their file formats now.

### Materialization directory convention: `.materialized-secrets/`

A new git-ignored, mode-0700 directory at the repository root (distinct from
`.generated/`, which already holds non-secret rendered contracts). `.gitignore`
gains a `.materialized-secrets/` entry. `materialize` refuses to run if the
directory already exists with the wrong permissions rather than silently
`chmod`-ing it.

## Risks / Trade-offs

- [Risk] Shelling out to `sops`/`age` ties tests to binaries being installed
  in CI/dev environments. → Mitigation: `doctor`/prerequisite check runs
  first and fails with an actionable message; unit tests that need the real
  binaries are isolated and skipped (not failed) when the pinned tool is
  absent, matching how Terraform-dependent tests already behave per
  `docs/02-configuration.md`.
- [Risk] A narrow, explicit `tracked_consumers` list in the drift checker can
  miss a new file that silently hardcodes a version. → Mitigation: documented
  in `docs/02-configuration.md` as a known boundary; adding a consumer entry
  becomes a checklist item in future phases' task lists.
- [Risk] Age key material generated for local/dev use could be mistaken for
  a production recipient. → Mitigation: the production-recipient requirement
  (already specified) rejects locally auto-generated recipients for
  `profile: production` documents unless explicitly confirmed by the
  operator.
- [Trade-off] Implementing the pin-change review gate as an opt-in
  `--previous` comparison (like migration checks) rather than a mandatory git
  hook means a version bump without `--previous` supplied will not trigger
  the gate. This mirrors the existing migration-check design and keeps the
  two consistent, at the cost of relying on CI always passing `--previous`.

## Migration Plan

This is new functionality with no prior behavior to migrate away from.
Rollout is additive:
1. Add `config/versions.schema.json` and validate the existing
   `config/versions.yaml` against it, fixing any gaps the schema surfaces.
2. Extend `config/versions.yaml` with the remaining component pins.
3. Add `nighthawk/secrets.py` and its subcommands, plus `nighthawk doctor`.
4. Add `.materialized-secrets/` to `.gitignore`.
5. Update `docs/01-architecture.md` and `docs/02-configuration.md` to
   describe the now-implemented workflow.

No existing stored data or committed secret exists to migrate; no rollback
beyond reverting the commit is needed.
