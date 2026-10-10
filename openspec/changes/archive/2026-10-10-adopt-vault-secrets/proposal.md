# Proposal

## Why

The platform stores every credential, storage key, and its certificate
authority in SOPS + age files inside the checkout, and issues certificates
from a CA whose private key sits in one of those files. The decision now is
to use HashiCorp Vault instead: one audited store with access policies and
version history, and a certificate authority whose key never leaves it. The
audit behind `reconcile-plan-with-implementation` also found the SOPS
workflow can destroy stored secrets and exposes values on the command line;
replacing the workflow removes that code rather than repairing it.

Doing this now, before phases 5–10, means Ansible, the Kubernetes workloads,
and the remote collector tooling are built against one secret store instead
of being migrated later.

## What Changes

- **BREAKING** Vault's key-value store (KV version 2) replaces SOPS + age as
  the only secret store in all three deployment profiles. The `secrets/`
  files, age keys, recipient handling, and the `sops` and `age` tools are
  removed.
- **BREAKING** Vault's PKI engine issues the gateway, storage, and collector
  certificates. The locally generated certificate authority and its `init-ca`
  command are removed; the CA private key is never held by the platform.
- **BREAKING** The platform document changes shape (new `schema_version`): a
  `vault` section declares the server address, the key-value mount, and the
  PKI mount and roles; each secret reference names a Vault path and key
  instead of a file and key; the CA secret references are removed.
- The Vault server is provided by the operator. The platform never deploys,
  initializes, or unseals one. It renders the exact access policy and PKI
  role definitions it needs so the operator can apply them.
- Authentication uses a token or an AppRole, supplied through the environment
  or a file, never through the platform document or a command-line argument.
- Storing a secret never replaces existing values: writes use Vault's
  check-and-set, and rotation creates a new version.
- **BREAKING** The Docker quickstart needs a reachable, unsealed Vault before
  it changes anything. A development helper configures a throwaway dev-mode
  Vault for local use and tests; it is refused for a production document.
- Production documents are refused when the Vault address is plaintext or
  loopback, or when the token carries the root policy.
- The prerequisite check verifies Vault instead of `sops` and `age`: reachable,
  unsealed, a supported version, a valid token, and the declared mounts and
  roles present.
- Revoking a certificate also revokes it in Vault. The gateway keeps
  enforcing revocation from the fingerprint list in its policy.
- The compatibility matrix drops the `sops` and `age` pins and records the
  supported Vault version and the digest of the image used for tests.
- `Plan.md` gains an amendment replacing its SOPS + age decision, and the
  documents and runbooks are rewritten for Vault.

Unchanged: secrets are still materialized into an owner-only, ignored
directory by the command-line tool, and containers still read them from
there. No container talks to Vault.

## Capabilities

### New Capabilities

None. The existing `secrets-workflow` capability keeps its name and is
rewritten around Vault.

### Modified Capabilities

- `secrets-workflow`: the store becomes Vault. Recipient generation, SOPS
  encryption, and the production recipient rule are removed; connection,
  authentication, non-destructive storage, production safeguards, the
  prerequisite check, rendered access requirements, and the development
  bootstrap are added; rotation, materialization, and offline validation are
  restated for Vault.
- `gateway-trust-lifecycle`: credentials are stored in Vault; certificates
  are signed by Vault's PKI engine; the development certificate authority is
  removed; revocation is also recorded in Vault.
- `local-object-storage`: storage identities are stored in Vault.
- `docker-quickstart`: the prerequisite check, the bring-up, and the
  secrets-stay-local rule are restated for a Vault the operator provides.
- `compatibility-matrix`: the inventory replaces SOPS and age with Vault; the
  checksummed secrets tooling requirement is removed.

## Impact

- **Python**: `nighthawk/secrets.py` and `trust.py` are largely rewritten;
  a new Vault client module; `config.py`, `quickstart.py`, `tools.py`,
  `gateway.py`, `storage.py`, and `__main__.py` change. Commands removed:
  `generate-recipient`, `encrypt-secret`, `init-ca`. Commands added or
  changed are listed in `design.md`.
- **Config**: `config/platform.schema.json`, `config/tenants.example.yaml`,
  `tests/e2e/platform.yaml`, `config/versions.yaml` and its schema.
- **Dependencies**: no new Python package; the client uses Vault's HTTP API
  through the standard library. A Vault server becomes a runtime
  prerequisite. A Vault container image is added for tests only.
- **Tests**: `tests/test_secrets.py`, `test_trust.py`, `test_quickstart.py`,
  `test_tools.py`, `test_config.py`, `test_versions.py`, `tests/fakes.py`,
  and the end-to-end suite.
- **Documents**: `Plan.md`, `docs/00`, `01`, `02`, `03`, `05`, `07`.
- **Other changes**: `reconcile-plan-with-implementation` deliberately leaves
  the SOPS defects for this change. Both touch `quickstart.py` and the
  `docker-quickstart` and `compatibility-matrix` specs, in different
  requirements; either can be applied first.
- **Licence**: Vault is distributed under the Business Source License. This
  is recorded in the architecture document next to the existing storage
  amendment.
- **Verification limits**: everything is verified against a dev-mode Vault in
  a local container. No production Vault, namespace, or high-availability
  setup is exercised.
