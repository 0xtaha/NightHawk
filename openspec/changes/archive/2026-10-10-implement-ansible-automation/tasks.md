# Tasks

The unit test command throughout is
`python -m unittest discover -s tests -p "test_*.py"`. "Molecule" means
`molecule test` for the named role, which runs converge, idempotence, and a
check-mode pass on each supported system's container.

## 1. Tooling and pins

- [x] 1.1 Resolve current stable versions of `ansible-core`, `ansible-lint`, Molecule and its Podman driver, and the `ansible.posix` and `community.general` collections from upstream; add them to `config/versions.yaml` and its schema with source references; verify matrix validation passes
- [x] 1.2 Resolve from upstream and add to the matrix: the k3s binary checksums and the Alloy archive checksums for amd64 and arm64, the Docker Engine version, and the fingerprint of Docker's repository signing key; add validation that fails on a missing checksum for a supported architecture, with unit tests in `tests/test_versions.py`, and verify they pass
- [x] 1.3 Add Rocky Linux 9 and AlmaLinux 9 to `os_support.docker_hosts`, add a required `evidence` field (`container`, `host`, or `declared`) to every entry, and extend `is_os_supported` tests for the RHEL family as Docker host and not as cluster node; verify the unit tests pass
- [x] 1.4 Create `ansible/ansible.cfg`, `ansible/requirements.yml`, and `ansible/requirements-dev.txt` from the pins; add tracked consumers so `check-pins` compares them with the matrix; verify `python -m nighthawk check-pins` passes and fails when one version is changed by hand, then restore
- [x] 1.5 Install the development requirements into a separate virtual environment and the collections into the project; verify `ansible-lint --version` and `molecule --version` report the pinned versions
- [x] 1.6 Pull systemd-enabled container images for Ubuntu 22.04 and 24.04, Debian 12, Rocky Linux 9, and AlmaLinux 9 and confirm each starts systemd under rootless Podman; record any that cannot, with the reason, for task 3.6

## 2. Network contract and rendered inputs

- [x] 2.1 Add rules to `config/network.yaml` for SSH administration and for the API server, kubelet, embedded etcd, and Cilium overlay and health traffic between cluster nodes; verify `validate` still passes for both example documents, the gateway tests pass unchanged, and the AWS parity test still finds exactly one AWS-destined rule
- [x] 2.2 Create a module that renders the Ansible inputs (firewall rules per host role, pins and checksums, supported systems per role, hostnames and entry point ports) and add them to `render-contracts` under `ansible/`; add unit tests for content, determinism across reordered documents, and that no Vault call is made; verify they pass
- [x] 2.3 Add a unit test that scans `ansible/roles` and `ansible/playbooks` for literal ports, versions, and checksums that the rendered inputs provide, and one that scans tracked inventories and variable files for secret-named variables with literal values; verify both pass on an empty tree and fail on a planted example

## 3. Foundation: inventories and preflight

- [x] 3.1 Create the three example inventories (`docker-host`, `k3s-development`, `k3s-production`) with documented variables and no secret; verify `ansible-inventory --list` parses each and the secret scan from 2.3 passes
- [x] 3.2 Implement the `preflight` role: load the rendered inputs, fail when they are absent, and stop the whole run when any host's distribution, version, or architecture is not supported for its role, naming host and finding
- [x] 3.3 Add a Molecule scenario for `preflight` covering each supported system as a Docker host, a Rocky host targeted as a cluster node, and an unsupported image; verify it passes
- [x] 3.4 Add `ansible-lint` configuration (production profile) and a repository test that runs `ansible-lint` and `ansible-playbook --syntax-check` on every playbook when the tools are installed, and is reported as skipped with the reason when they are not; verify both outcomes
- [x] 3.5 Start `docs/08-ansible.md` with prerequisites, the control-machine setup, rendering the inputs, and the inventories; verify each documented command runs as written
- [x] 3.6 Set each Docker host entry's `evidence` in the matrix to what task 1.6 and the scenarios in this change support, and record the reason for any entry left `declared`; verify matrix validation passes

## 4. Node hardening

- [x] 4.1 Implement the `hardening` role: key-only SSH with no direct root password login, the documented kernel network settings, time synchronization, and opt-in automatic security updates, with handlers and check-mode support
- [x] 4.2 Make the role verify key authentication for the connecting account before disabling password authentication, and validate the new SSH configuration with the server's own check before reload, keeping the previous file on failure
- [x] 4.3 Take the kernel settings that depend on the host's role from the rendered inputs so a cluster node keeps forwarding enabled and a compatible reverse-path filter mode
- [x] 4.4 Add a Molecule scenario covering every supported system: settings applied, idempotence, check mode changing nothing, the missing-key refusal, and an invalid SSH configuration being rolled back; verify it passes
- [x] 4.5 Document the exact list of settings and the two safeguards in `docs/08-ansible.md`; verify the list matches the role's tasks

## 5. Host firewall

- [x] 5.1 Implement rule application for UFW (Debian, Ubuntu) and firewalld (Rocky, Alma) from the rendered rules and the inventory's allowlists and node addresses, default-denying inbound traffic
- [x] 5.2 Implement the refusals before any change: an empty allowlist, a range covering every address, a requested port that is not in the contract, and the role's own connection address outside the admin allowlist
- [x] 5.3 Implement the self-cancelling undo: record state, schedule a transient timer that restores it, apply (SSH allow first), open a fresh connection, and cancel the timer only on success
- [x] 5.4 On Docker hosts, restrict the published external entry point to the collector allowlist in the `DOCKER-USER` chain, persistently
- [x] 5.5 Add a Molecule scenario covering both families: generated rules equal the rendered rule set, each refusal fails before any change, idempotence, and check mode; verify it passes, and record in the scenario's notes that enforcement and the undo are not observable in a container
- [x] 5.6 Document the rule table per host role, the allowlists, the lockout protection with its timing variable, how to recover by console if all else fails, and the Docker bypass, in `docs/08-ansible.md`; verify the rule table against the rendered inputs

## 6. Docker Engine

- [x] 6.1 Implement the `docker_engine` role: fetch the repository signing key, compare its fingerprint with the matrix before trusting it, add the repository, install the pinned engine and Compose plugin for the distribution, hold the version, and enable the service
- [x] 6.2 Make a different installed version an error stating both versions unless the operator sets the explicit override
- [x] 6.3 Add a Molecule scenario covering all five systems: correct package version installed, a planted wrong fingerprint installs nothing, idempotence, and check mode; verify it passes with the daemon not started
- [x] 6.4 Document the role, the pin, and the version-change override in `docs/08-ansible.md`

## 7. Remote deployment of the stack

- [x] 7.1 Refactor `nighthawk/quickstart.py` so prerequisite checks, secret creation, and rendering are callable without Compose, with remote paths, UID, and GID as parameters; verify the existing quickstart unit tests and the end-to-end suite still pass unchanged
- [x] 7.2 Add `render-docker-deployment`: refuse a document that is not `profile: production` or does not select the external entry point, refuse a missing external bind address, and write a rendered tree and a secret tree addressed for the remote host; add unit tests for each refusal, for the remote paths in `compose.env`, for a second run changing nothing, and that no written file holds the Vault credential; verify they pass
- [x] 7.3 Add `docker-compose/docker-compose.external.yaml` publishing the external entry point on a required `NIGHTHAWK_EXTERNAL_BIND_ADDRESS`; extend `tests/test_compose.py` for with, without, and missing-address cases and that the loopback mapping is unchanged; verify they pass
- [x] 7.4 Add an end-to-end case that starts the local stack with the override on a second loopback address and verifies ingestion on the external entry point is refused without the client certificate and accepted with it, and that the loopback entry point is still the only other published port; verify the whole suite passes
- [x] 7.5 Implement the `nighthawk_stack` role: synchronize the rendered and secret trees (secrets `no_log`, owner-only), copy the image build context, build the image, run Compose with the override, wait for health naming any unhealthy service, reload the auth service and collector when their files changed, and print the single-node notice
- [x] 7.6 Implement `playbooks/docker-teardown.yml`: remove containers and keep volumes, secrets, and certificates unless a purge is confirmed by name
- [x] 7.7 Add a Molecule scenario for `nighthawk_stack` covering file synchronization, permissions, unchanged second run, and check mode, with Compose not run; verify it passes
- [x] 7.8 Write the Docker host section of `docs/08-ansible.md` (prepare, render, deploy, verify from another machine, re-render, teardown, what is lost if the host is lost) and update `docs/07-docker-compose.md` and `docs/01-architecture.md`, where production single-node Compose is no longer deferred; verify every documented command exists in `--help` output

## 8. Certificates on demand

- [x] 8.1 Move the quickstart's "issue again if missing, expiring, or from another authority" check into `nighthawk/trust.py`, make the quickstart use it, and add `--if-needed` and `--renew-before-days` to `issue-certificate`, replacing an existing pair only after the new certificate is verified
- [x] 8.2 Add unit tests in `tests/test_trust.py` for nothing to do, close to expiry, authority replaced, and a failed replacement leaving the old pair in place; verify they pass
- [x] 8.3 Document the mode in `docs/05-gateway.md` under renewal; verify the command against a dev-mode Vault

## 9. Kubernetes cluster

- [x] 9.1 Add the optional `cluster.join_token_secret_ref` to `config/platform.schema.json` and the loader, valid only for a self-hosted Kubernetes deployment; add `config/self-hosted.example.yaml`; add `generate-cluster-token`, which stores a token once and never replaces it; add unit tests and verify they pass
- [x] 9.2 Implement `check-cluster-layout`: given the declared shape, ranges, storage disks, address pool, and per-node facts, return every problem by node for server count and parity, storage node count, kernel version and modules, required packages, missing or in-use disks, MTU mismatch, overlapping ranges, and an address pool that is empty, outside the node network, or contains a node address; add unit tests for each rule and for several problems reported together; verify they pass
- [x] 9.3 Implement the `k3s_prerequisites` role: gather the facts, run `check-cluster-layout` on the control machine, and fail the whole run with its report before any host is changed
- [x] 9.4 Implement the `k3s_node` role: download the binary for the architecture, verify the matrix checksum, install the unit and configuration (bundled network plugin, ingress controller, and service load balancer disabled; secrets encryption on), copy the join token with `no_log`, and fail on a different running version unless overridden
- [x] 9.5 Implement `playbooks/k3s-cluster.yml`: preflight and prerequisites on all nodes, the first server, further servers one at a time, agents, then wait for every node to be Ready, naming any that is not; add the optional, owner-only kubeconfig fetch
- [x] 9.6 Implement the `cilium` role: place a `HelmChart` manifest with the pinned chart version and the inventory's pod range, service range, and MTU in the server's manifests directory
- [x] 9.7 Add Molecule scenarios for `k3s_node` and `cilium` on both Ubuntu versions: checksum verified, a planted wrong checksum installs nothing, unit, configuration, and manifest content, idempotence, and check mode, with k3s not started; verify they pass
- [x] 9.8 Write the cluster section of `docs/08-ansible.md` (shapes, inventory variables, the prerequisite list, bootstrap, adding a node, fetching the kubeconfig, what phase 6 installs next); verify the prerequisite list against `check-cluster-layout`

## 10. External collector service

- [x] 10.1 Implement the `alloy_collector` role: install the pinned archive with checksum verification, create the system account, and write the hardened unit with one writable state directory
- [x] 10.2 Copy the configuration rendered by `render-collector`, the credential, and the client certificate and key from `issue-certificate --if-needed` (secrets `no_log`, readable by the collector's account only); fail before any change when the credential declares no certificate identity
- [x] 10.3 Validate a changed configuration with Alloy's own check on the host before replacing the active one, and reload instead of restart when only the configuration changed
- [x] 10.4 Add a Molecule scenario on all five systems: the service runs as its account and loads the configuration, an invalid configuration is refused and the old one kept, file permissions, idempotence, and check mode; verify it passes
- [x] 10.5 Write the external collector section of `docs/08-ansible.md` and link it from `docs/04-collection.md`; verify the documented commands

## 11. Plan and status documents

- [x] 11.1 Update the recorded deferrals in `Plan.md` and `docs/01-architecture.md`: the Ansible pins and OS matrix evidence are done, production single-node Compose is implemented, and note what remains unverified on real hosts; verify the two lists agree
- [x] 11.2 Add the verification limits to `docs/08-ansible.md` (nothing run on a real host; k3s start, Cilium, kernel prerequisites, firewall enforcement, the undo, and a remote deployment end to end never executed) and link the document from the README; verify the links resolve
- [x] 11.3 Update the diagram in `docs/03-diagrams.md` so host and cluster automation is shown as implemented and container-tested; verify it renders

## 12. Integration check

- [x] 12.1 Run the unit test command and verify every test passes
- [x] 12.2 Run `ansible-lint` on `ansible/` and `--syntax-check` on every playbook and verify both are clean
- [x] 12.3 Run every Molecule scenario and verify all pass, or that each one that cannot run here is recorded with its reason in the matrix and the document
- [x] 12.4 Run `NIGHTHAWK_E2E=1 python -m unittest tests.e2e.test_stack` and verify every case passes
- [x] 12.5 Run `openspec validate --specs --strict`, `python -m nighthawk check-pins`, and `python -m nighthawk validate` on each example document, and verify all succeed
