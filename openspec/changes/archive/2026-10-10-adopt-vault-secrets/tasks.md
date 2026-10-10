# Tasks

The unit test command throughout is
`python -m unittest discover -s tests -p "test_*.py"`.

## 1. Vault version and test server

- [x] 1.1 Resolve the current stable HashiCorp Vault release and its container image digest from the upstream registry; add a `vault` entry to `config/versions.yaml` with the supported range, the tested version, the image digest, and source references, and extend `config/versions.schema.json`; verify matrix validation passes
- [x] 1.2 Add matrix validation that the tested version lies within the supported range, with unit tests in `tests/test_versions.py` for inside and outside the range, and verify they pass
- [x] 1.3 Start the pinned image in dev mode under the local container runtime on a loopback port and verify its health endpoint reports initialized and unsealed; record the exact command for the documents

## 2. Vault client

- [x] 2.1 Create a Vault client module using the standard library with an injectable transport: health and seal status, token self-lookup, AppRole login, mount and PKI role reads, KV version 2 read and check-and-set write, PKI sign, PKI revoke, and CA certificate fetch; errors name the path and operation and never include a credential
- [x] 2.2 Implement credential resolution (environment token, token file, AppRole identifier files) with the owner-only file check and the no-credential error; no command accepts a credential as an argument
- [x] 2.3 Add a fake transport to `tests/fakes.py` that models KV versions and check-and-set conflicts, PKI signing with role limits, token lookup, and denial; add unit tests for every client call, each credential source, a group-readable credential file, and a rejected credential, and verify they pass
- [x] 2.4 Add integration tests against the dev-mode server from 1.3, gated by an environment variable and reported as skipped with a reason when it is unset, covering a real check-and-set conflict and a policy denial; verify they pass with the server running and skip without it

## 3. Platform document

- [x] 3.1 Change `config/platform.schema.json` to schema version 2: add the `vault` section, change `secrets` entries to `{path, key}`, remove the gateway CA references, and express a storage binding's trust as system roots, the platform PKI authority, or a stored CA certificate
- [x] 3.2 Update `nighthawk/config.py` for the new shape: reject a version 1 document with a message naming the changes, reject a plaintext non-loopback Vault address, reject a plaintext or loopback address for `profile: production`, and reject any CA private key reference
- [x] 3.3 Convert `config/tenants.example.yaml` and `tests/e2e/platform.yaml` to version 2
- [x] 3.4 Update `tests/test_config.py` for the new shape and add tests for each rejection in 3.2; verify the unit test command passes for that file and `python -m nighthawk validate --config config/tenants.example.yaml` succeeds with no Vault running
- [x] 3.5 Rewrite the platform document reference in `docs/02-configuration.md` for the `vault` section and the secret reference shape; verify every field shown exists in the schema

## 4. Secrets workflow on Vault

- [x] 4.1 Rewrite `nighthawk/secrets.py`: remove recipient generation, the recipients registry, SOPS encryption, and the recipient guard; implement store (refuses an existing key, keeps other keys), rotate (refuses an absent key), and materialize (fails naming every absent reference, leaves nothing partial) on the client, with values read from standard input or a file
- [x] 4.2 Implement the production safeguard that refuses a token carrying the root policy before any read or write for a `production` document
- [x] 4.3 Rewrite `doctor` and `ensure_doctor_ok` to check Vault: reachable, unsealed, version within the matrix range, credential accepted, and declared mounts and roles present, naming each failure
- [x] 4.4 Update `nighthawk/__main__.py`: remove `generate-recipient` and `encrypt-secret`; add `store-secret`; change `rotate-secret`, `materialize-secrets`, and `doctor`; add the Vault credential options
- [x] 4.5 Rewrite `tests/test_secrets.py` against the fake transport for every scenario in the `secrets-workflow` delta that these tasks implement, including that no call carries a secret value or credential in process arguments; verify the unit test command passes for that file
- [x] 4.6 Against the dev-mode server, store two keys at one path, rotate one, and materialize; verify both keys are present, the rotated key has the new value, and the other is unchanged
- [x] 4.7 Rewrite the secrets section of `docs/02-configuration.md` for the new commands, authentication, the production safeguards, and that rotated values remain as older versions in Vault; verify each documented command runs as written against the dev-mode server

## 5. Rendered access requirements and development bootstrap

- [x] 5.1 Make `render-contracts` write the Vault policy and the two PKI role definitions from the platform document; add unit tests that the policy names exactly the declared paths and roles, that the roles allow exactly the declared hostnames and identities, and that output is byte-identical across runs; verify they pass
- [x] 5.2 Implement `bootstrap-dev-vault`: enable the mounts, create a root authority inside Vault, write the roles and the policy from 5.1; require the confirmation flag, refuse a `production` document, and change nothing on a second run; add unit tests for each refusal and for idempotence, and verify they pass
- [x] 5.3 Run `bootstrap-dev-vault` against an empty dev-mode server, then `doctor`, and verify `doctor` succeeds; run the bootstrap again and verify the authority's certificate is unchanged
- [x] 5.4 Create a token limited to the rendered policy on the dev-mode server and verify it can read a declared path and is denied an undeclared one
- [x] 5.5 Document the rendered files and how an operator applies them to their own Vault, and the bootstrap as development-only, in `docs/02-configuration.md`; verify the documented commands run as written

## 6. Certificates through Vault PKI

- [x] 6.1 Rewrite certificate issuance in `nighthawk/trust.py`: keep the declared-identity, declared-hostname, and required-validity checks; generate the key locally, send a signing request to the role's sign endpoint, verify the returned certificate's names, identity, usage, and validity before writing, and write key and certificate owner-only; remove `init_ca` and all local signing
- [x] 6.2 Change credential and storage identity generation in `trust.py` to store through `secrets.py`; remove `store_new_secret`, `secret_key_exists`, and the SOPS decrypt helper
- [x] 6.3 Implement `revoke-certificate`: revoke in Vault by serial number and print the fingerprint to list in the platform document
- [x] 6.4 Make runtime rendering take the gateway's client trust and the PKI-trusted storage bindings' trust from the PKI mount's public certificate, and report that certificates must be issued again when that certificate changed since the previous render
- [x] 6.5 Update `nighthawk/__main__.py`: remove `init-ca`, add `revoke-certificate`, and drop the recipient options from the generation commands
- [x] 6.6 Rewrite `tests/test_trust.py` against the fake transport for every scenario in the `gateway-trust-lifecycle` and `local-object-storage` deltas, including that the request sent to Vault contains no private key and that a certificate with names beyond the request is refused; verify the unit test command passes for that file
- [x] 6.7 Against the bootstrapped dev-mode server, issue a gateway, a storage, and a collector certificate and verify each chains to the PKI authority with the expected names; request a collector certificate for an identity outside the role and verify Vault refuses it; revoke one and verify Vault lists it as revoked
- [x] 6.8 Rewrite the lifecycle runbooks in `docs/05-gateway.md` (first issuance, credential rotation, certificate renewal, revocation) for Vault, each step with its command and observable result; verify each command against the dev-mode server

## 7. Docker quickstart

- [x] 7.1 Replace the `sops` and `age` prerequisites in `nighthawk/quickstart.py` with the Vault check, and make a missing Vault or credential stop the quickstart before any change with the two documented commands
- [x] 7.2 Rewrite `ensure_secrets` to generate into Vault and remove the age key, the recipient handling, the CA creation, and the copying of the CA certificate into storage trust references
- [x] 7.3 Detect existing Compose volumes whose storage identities are absent from Vault and stop before starting anything, naming the teardown that clears the volumes
- [x] 7.4 Remove the age key option from `quickstart-docker`, add the Vault credential options, and ensure the credential is written to neither the Compose environment file nor the materialization directory
- [x] 7.5 Rewrite the affected tests in `tests/test_quickstart.py` for the `docker-quickstart` delta scenarios, including a search of every file the quickstart writes for the Vault credential; verify the unit test command passes for that file
- [x] 7.6 Make the end-to-end suite start a dev-mode Vault, bootstrap it, and pass its address and credential to the quickstart; run `NIGHTHAWK_E2E=1 python -m unittest tests.e2e.test_stack` and verify every existing case passes
- [x] 7.7 Rewrite `docs/00-quickstart.md` and the secrets passages of `docs/07-docker-compose.md`: the dev-mode Vault and bootstrap steps, what a recreated dev-mode Vault means for existing volumes, and a new measured cold and warm start that includes those steps; verify by following the document from a clean checkout

## 8. Removing SOPS and age

- [x] 8.1 Remove the `sops` and `age` entries from `config/versions.yaml` and its schema, and their download from `nighthawk/tools.py`; keep the command if another tool download remains, otherwise remove it and its tests; update `tests/test_tools.py` and `tests/test_versions.py` and verify the unit test command passes
- [x] 8.2 Remove the `secrets/` and age entries from `.gitignore` and `.dockerignore` that no longer apply and keep the materialization directory entries; verify `git status` shows no untracked secret material after a quickstart run
- [x] 8.3 Verify no reference to `sops`, `age` recipients, or `.sops.yaml` remains outside change folders, apart from the amendments in `Plan.md` and `docs/01-architecture.md` that name what was replaced, the main specs awaiting this change's sync, and the test that rejects the old secret shape, with `grep -rniE 'sops|agekey|age-recipients' --exclude-dir=archive --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.tools .`

## 9. Plan, architecture, and spec purpose

- [x] 9.1 Add an amendment to `Plan.md` at its SOPS + age decision, in the style of the existing storage amendment, stating that Vault replaces it for secrets and certificate issuance and that the server is operator-provided; verify the amendment sits beside the original text
- [x] 9.2 Add a secrets amendment to `docs/01-architecture.md` recording the decision, the Business Source License, that only HashiCorp Vault is tested, and the deferred in-cluster delivery question for phase 6; update `docs/03-diagrams.md` so the secret and trust flow shows Vault; verify the diagrams render
- [x] 9.3 After this change's deltas are synced, edit the Purpose of `openspec/specs/secrets-workflow/spec.md` and `openspec/specs/gateway-trust-lifecycle/spec.md` directly so they no longer describe SOPS + age or a local authority; verify `openspec validate --specs --strict` passes

## 10. Integration check

- [x] 10.1 Run the unit test command with the dev-mode server running and verify every test passes with no Vault integration test skipped
- [x] 10.2 Run the unit test command with no Vault running and verify the Vault integration tests are reported as skipped and everything else passes
- [ ] 10.3 From a clean checkout state, follow `docs/00-quickstart.md` end to end, then run the quickstart a second time, and verify both runs end healthy and the second creates no secret and no certificate
