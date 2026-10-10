# Design

## Context

See `proposal.md` for motivation. The scope was fixed before drafting: Vault
replaces SOPS + age in every environment, the Vault server is provided by the
operator, and Vault covers both secrets and certificate issuance.

What exists today, from reading the code:

- A platform document declares `secrets: {id: {file, key}}`, where `file`
  must match `secrets/*.sops.yaml`. Credentials, storage identities, the
  Grafana admin password, and the CA certificate and key are all such
  references (`config/platform.schema.json:22-35`,
  `config/tenants.example.yaml:68-78`).
- `nighthawk/secrets.py` shells out to `sops` and `age` for recipient
  generation, encryption, rotation, and materialization. `nighthawk/trust.py`
  generates credentials and storage identities, creates a local CA whose
  private key is stored as a secret, and signs certificates with it using the
  `cryptography` package.
- Only the command-line tool on the host touches secrets. It materializes
  them into `.materialized-secrets/`, and the quickstart turns them into
  runtime files that containers mount read-only. The auth service reads a
  policy bundle holding digests, not secrets.
- The gateway enforces certificate revocation from a fingerprint list in that
  policy (`nighthawk/authz.py:193`).
- `validate` and `render-contracts` never read a secret.
- SOPS is referenced in 30 files, six of them accepted specs.
- Kubernetes and AWS workloads, Ansible, and remote collector tooling do not
  exist yet, so nothing outside the Docker tier consumes secrets today.

Constraints:

- The project's rule is no silent fallbacks and no new dependency without a
  pin.
- A rootless Podman runtime is available locally; no Vault server is.
- Nothing is deployed, so there is no production secret to migrate.

## Goals / Non-Goals

**Goals:**

- One secret store and one certificate authority, both in Vault, for every
  profile the platform document can describe.
- The platform holds no CA private key and no long-lived Vault credential.
- The host-side materialization model and everything downstream of it
  (runtime files, Compose mounts, the policy bundle) stay as they are.

**Non-Goals:**

- Deploying, initializing, unsealing, backing up, or upgrading Vault.
- Delivering secrets inside Kubernetes (Vault Agent, the Secrets Operator,
  External Secrets, or Kubernetes auth). Phase 6 chooses that when the
  workloads exist; this change only keeps the reference shape usable by any
  of them.
- Dynamic secrets, such as short-lived storage or database credentials.
- The gateway checking Vault's revocation list at request time.
- Vault Enterprise features beyond passing a namespace through.
- Carrying existing SOPS values over. Local development values are generated
  again.
- Testing against OpenBao. Its API is compatible in the parts used here, but
  only HashiCorp Vault is in the matrix.

## Decisions

### D1. Talk to Vault's HTTP API through the standard library

A small client module wraps the handful of endpoints used: token lookup,
AppRole login, seal and health status, mount and role reads, KV version 2
read and write, PKI sign and revoke, and the CA certificate. It takes an
injectable transport, the way the current code takes an injectable process
runner, so unit tests need no server.

*Alternatives:* the `hvac` package, or shelling out to the `vault` binary.
`hvac` adds a pinned dependency tree for about ten calls. The binary would
repeat the current pattern of downloading and checksumming a tool, and put
values back on command lines. Both rejected.

### D2. The platform document names where, never who

A new required `vault` section:

- `address`, and optionally `namespace` and a CA file for Vault's own TLS;
- `kv_mount`;
- `pki`: `mount`, `server_role`, `client_role`.

Each `secrets` entry becomes `{path, key}` within `kv_mount`. The gateway's
`client_ca_secret_ref` and `client_ca_key_secret_ref` are removed.
`schema_version` goes to 2, and a version 1 document is rejected with a
message naming what changed, rather than half-read.

A plaintext address is accepted only for loopback, which is what a dev-mode
server uses. Credentials are never part of the document.

*Alternative:* keep version 1 valid and accept both shapes. Rejected: two
secret stores is the "alongside" option that was declined, and it doubles
every code path.

### D3. A storage binding's trust has three explicit sources

Today a binding's `tls.ca_secret_ref` holds a copy of the local CA's
certificate. With the CA in Vault that copy has no reason to exist. A binding
states its trust as one of: the system roots (AWS S3), the platform's PKI
authority (local SeaweedFS, whose server certificate that authority signs),
or a CA certificate stored as a secret (an external S3 endpoint with a
private CA). The first and third exist today; the second replaces the copy.

### D4. Authentication is a token or an AppRole, from the environment or a file

Resolution order is explicit, not a fallback chain that hides which one was
used: `VAULT_TOKEN` in the environment; else a token file option; else role
and secret identifier file options. Files must be owner-only. An AppRole
login token is held in memory for the one command. Nothing accepts a
credential as an argument, and nothing writes one.

Kubernetes and cloud auth methods are left for phase 6 with the in-cluster
delivery question.

### D5. Writes use check-and-set so nothing is ever replaced by accident

KV version 2 versions a whole path, and several keys share a path. To add or
rotate one key the client reads the path with its version, changes the one
key, and writes with `cas` set to that version (`cas=0` for a new path).
A concurrent change makes Vault reject the write, and the command fails
asking for a retry instead of overwriting.

- Store: refuses a key that already holds a value.
- Rotate: refuses a key that holds none.
- Generate (credential, storage identity, Grafana admin password): store with
  a generated value.

This removes by construction the two SOPS defects recorded in
`reconcile-plan-with-implementation` (whole-file overwrite, values in
arguments).

### D6. Certificates are signed from a local request; the CA key stays in Vault

The existing checks stay in front: a collector certificate needs a declared
identity, a server certificate gets exactly the declared hostnames, and a
validity period is required. The command then generates the private key
locally with the `cryptography` package, builds a signing request, and sends
it to the PKI role's sign endpoint with the names and the validity. Before
writing anything it checks that the returned certificate carries exactly the
requested names, identity, usage, and a validity no longer than requested.

The gateway's client trust is the PKI mount's public CA certificate, fetched
at render time.

*Alternative:* let Vault generate the key and return it. Rejected: the key
would cross the network and pass through Vault's response handling for no
benefit.

Two roles are enough: a server role limited to the declared hostnames with
server usage, and a client role limited to the declared collector identities
as URI names with client usage.

### D7. Revocation is recorded in Vault and enforced at the gateway

A `revoke-certificate` command revokes the certificate in Vault by serial
number and prints its fingerprint. Enforcement stays where it is: the
fingerprint goes into the platform document and the rendered policy. The
gateway therefore keeps working when Vault is down, and Vault's revocation
list stays accurate for anyone else who consults it.

*Alternative:* have the auth service fetch Vault's revocation list. Rejected
for now: it adds a runtime dependency on Vault to every request path.

### D8. The platform renders what it needs from Vault; the operator applies it

`render-contracts` gains a Vault output: a policy granting read and write on
exactly the declared secret paths and use of the two PKI roles, and the two
role definitions with their allowed names. This is non-secret and
deterministic, and it is how an operator-provided Vault gets configured
without the platform holding administrative access.

For local use and tests, `bootstrap-dev-vault` applies that output to a
disposable Vault: enables the mounts, creates a root authority inside Vault,
writes the roles and the policy. It needs an explicit confirmation flag and
refuses a `production` document, following the existing pattern for the
local CA confirmation it replaces. It is idempotent and never replaces an
existing authority.

### D9. Production safeguards are checks on the address and the token

For `profile: production`: validation rejects a plaintext or loopback Vault
address, and any command that reaches Vault looks up its own token and
refuses one carrying the root policy. These replace the age recipient guard,
which protected against using a locally generated development key in
production. The equivalent mistake now is pointing production at a dev-mode
Vault with its root token.

### D10. The quickstart needs a Vault and says so first

The prerequisite check covers Vault before anything is created. With no
Vault or no credential, the quickstart stops and prints the documented two
steps: start a dev-mode Vault container, run `bootstrap-dev-vault`.

A dev-mode Vault keeps everything in memory. If it is recreated while Compose
volumes survive, the stored data is protected by storage identities that no
longer exist. The quickstart detects this (volumes present, storage
identities absent from Vault) and stops with the teardown command that clears
the volumes, instead of generating new identities that cannot read the old
data.

The Vault credential is never written to the Compose environment file or the
materialization directory. Containers do not receive it.

*Alternative:* have the quickstart start the dev-mode Vault itself. That was
the "dev server locally" option and was declined in favour of an
operator-provided Vault.

### D11. The matrix records a supported range and a tested version

Vault is not something the platform installs, so an exact pin would be
untrue. The matrix records the minimum and maximum supported server versions
and the exact version and image digest the tests run against. The
prerequisite check compares the server's reported version with the range.
The implementer resolves the current stable release and its digest from the
upstream registry when applying, with a source reference, as the matrix
requires for every other entry.

The `sops` and `age` pins, their checksums, and their part of the tool
download command are removed. If the Terraform download from
`reconcile-plan-with-implementation` is already present, the command keeps
it; if nothing remains for it to download, the command is removed.

### D12. Tests at three levels

- Unit tests use a fake transport that models KV versioning and
  check-and-set, PKI signing, and token lookup, replacing the `sops` fake in
  `tests/fakes.py`.
- Integration tests run against a real dev-mode Vault container, gated by an
  environment variable like the end-to-end suite, and are reported as
  skipped with a reason when it is absent. They cover what a fake cannot:
  the real check-and-set conflict, real PKI role enforcement, policy denial.
- The end-to-end suite starts a dev-mode Vault, bootstraps it, and runs the
  existing cases unchanged, since nothing downstream of materialization
  changes.

### D13. Commands

| Command | Change |
|---|---|
| `generate-recipient`, `encrypt-secret`, `init-ca` | removed |
| `store-secret` | new; replaces `encrypt-secret`; value from stdin or file |
| `rotate-secret` | value from stdin or file; `--value` removed |
| `generate-credential`, `generate-storage-identity` | store in Vault; recipient options removed |
| `issue-certificate` | signs through Vault PKI |
| `revoke-certificate` | new |
| `materialize-secrets`, `clean-secrets` | read from Vault; otherwise unchanged |
| `doctor` | checks Vault |
| `render-contracts` | also renders Vault access requirements |
| `bootstrap-dev-vault` | new, development only |
| `quickstart-docker` | age key options removed; Vault credential options added |

## Risks / Trade-offs

- [The local quickstart is no longer one command in a clean checkout] → It
  is two documented commands before it, and the quickstart prints them when
  Vault is missing. This is the accepted cost of an operator-provided Vault.
- [A dev-mode Vault loses everything on restart] → Detected and reported
  (D10). The documents state that a dev-mode Vault is for throwaway use.
- [Vault becomes a hard dependency for rendering and issuing] → Only at
  render and issue time. The running stack and the gateway's decisions do
  not depend on it. `validate` and `render-contracts` stay offline.
- [The rendered policy or roles are applied wrongly by an operator] → The
  prerequisite check names each missing mount and role; issuance verifies the
  returned certificate before using it.
- [Vault is under the Business Source License] → Recorded in the
  architecture document. The client uses only the HTTP API, so moving to a
  compatible server later means adding it to the matrix and testing it.
- [Only a dev-mode Vault is exercised] → Stated in the documents. Namespaces,
  high availability, and seal behaviour under failure are not verified.
- [KV version 2 keeps old versions of rotated secrets] → Documented in the
  rotation runbook, with the mount setting that limits retained versions;
  the platform does not destroy versions itself.
- [Version 1 platform documents stop working] → Nothing is deployed. The
  rejection message names the changes, and the example documents are
  converted in this change.

## Migration Plan

Nothing is deployed, so migration applies to local checkouts:

1. Tear down the local stack with its volumes, since the storage identities
   are generated again.
2. Start a dev-mode Vault and run `bootstrap-dev-vault`.
3. Convert the platform document to version 2 (the example is converted).
4. Run the quickstart.
5. Delete `secrets/*.sops.yaml`, the age key, and
   `.generated/age-recipients.local.json`.

Rollback is reverting the change; the SOPS files are untouched until step 5.

## Open Questions

- Which mechanism delivers secrets inside Kubernetes. Decided in phase 6;
  the reference shape here does not constrain it.
- Whether the supported range should start at the tested version or reach
  back to older releases. It starts at the tested version; widening it later
  needs only a matrix change and a test run.
