# Tasks

## 1. Pins, tools, and the local cluster

- [x] 1.1 Add Helm and the manifest validator to `validation_tools` with a SHA-256 per platform, and extend `fetch-tools` to download and verify them; add unit tests for a matching and a mismatching checksum; verify `fetch-tools` places both and they report the pinned versions
- [x] 1.2 Add the Vault Secrets Operator and CloudNativePG to the matrix, and add a repository and content digest to every chart the deployment installs; extend the schema so a chart the deployment installs without a digest fails validation; add unit tests and verify they pass
- [x] 1.3 Add `kubernetes.core` to `ansible/requirements.yml` and its Python client to `ansible/requirements-dev.txt`, pinned in the matrix with tracked consumers; verify `check-pins` passes and reports the new consumers
- [x] 1.4 Record in the matrix which components are horizontally scalable, and add a per-component field saying whether it was started in a cluster or only rendered; verify matrix validation passes
- [x] 1.5 Start a single-node minikube profile on the Podman driver with a NetworkPolicy-enforcing plugin and confirm a refused connection between two test pods under a deny policy; write the exact start command into the acceptance suite's setup; if no such cluster starts on this machine, stop and report instead of continuing
- [x] 1.6 Start `docs/09-kubernetes.md` with prerequisites, tool setup, and how the local cluster is created; verify each documented command runs as written

## 2. Contract and platform document

- [x] 2.1 Add the `cluster-gateway` entry point and the in-cluster flows (name resolution, Vault, cluster API for operators, Kafka, PostgreSQL, ring and gossip per backend) to `config/network.yaml`; verify `validate` passes and the rendered port table lists them
- [x] 2.2 Add `vault.kubernetes_auth` (mount, optional in-cluster address) to the platform schema and loader, valid only for a self-hosted document; update `config/self-hosted.example.yaml` with it, the `cluster-gateway` entry point, and service DNS names for `gateway.upstreams`; add unit tests for acceptance and each refusal; verify they pass
- [x] 2.3 Document both in `docs/02-configuration.md`; verify the example document validates

## 3. Vault access for a cluster

- [x] 3.1 Render `vault/kubernetes-auth.json`: the authentication role and one read-only policy per workload limited to the KV paths that workload reads, and the PKI roles the certificate manager may use; add unit tests that no policy grants write, that each workload's policy excludes other workloads' secrets, and that a Docker document renders none; verify they pass
- [x] 3.2 Extend `bootstrap-dev-vault` to apply those requirements idempotently, and add `doctor --cluster` to report a missing or wider role or policy; add unit tests with the fake Vault for apply, second run, missing role, and wider policy; verify they pass
- [x] 3.3 Document the Vault prerequisites for a cluster, including that Vault must reach the cluster API and the JWT alternative, in `docs/09-kubernetes.md`; verify the documented commands against a dev-mode Vault

## 4. Rendering for Kubernetes

- [x] 4.1 Add the Kubernetes renderer: `kubernetes/releases.yaml` with the ordered releases of the document's profile, and one values file per release, with every image by matrix digest; add unit tests for both profiles, determinism, no Vault contact, no secret value in any output, and no Kubernetes output for a Docker document; verify they pass
- [x] 4.2 Render the `platform/` files the platform chart mounts (gateway configuration, overrides, collector configuration for the node and cluster profiles through the `cluster-gateway` entry point, Grafana desired state); add a unit test that adding a datastream changes only rendered output; verify it passes
- [ ] 4.3 Extend backend configuration rendering to self-hosted Kubernetes: monolithic for development, distributed with Kafka topics and credentials references for production, refusing an unreviewed pinned version; add unit tests for each backend in each mode and the refusal; verify they pass
- [ ] 4.4 Render production availability settings (replica counts, disruption budgets, topology spread, requests and limits) and autoscalers only for components the matrix marks scalable; refuse Grafana replicas without a declared database; add unit tests; verify they pass
- [ ] 4.5 Document the rendered layout and the two profiles in `docs/09-kubernetes.md`; verify the file list in the document against an actual rendering

## 5. Network policies

- [x] 5.1 Implement the NetworkPolicy renderer: one default-deny policy per platform namespace and one allowance per in-cluster contract rule, using standard resources; add unit tests that every allowance maps to exactly one rule, every in-cluster rule has an allowance, backends accept only the gateway and their own components, and object storage and the database accept only their users; verify they pass
- [x] 5.2 Validate the rendered policies against the pinned Kubernetes schema with the manifest validator in a test; verify it passes
- [x] 5.3 Document the namespaces, the default deny, and how to extend the contract for a new flow, in `docs/09-kubernetes.md`; verify the documented rule table against the rendered policies

## 6. Platform chart

- [ ] 6.1 Create `helm/nighthawk-platform` with templates for service accounts, the Vault authentication and static secret resources, and the certificate issuer and certificates; verify `helm lint` and `helm template` succeed with the rendered development and production values
- [x] 6.2 On the local cluster with a dev-mode Vault fixture, establish whether the pinned operator's transformations can produce the three derived secrets (backend S3 environment, SeaweedFS identity file, Grafana administrator password); implement each with a transformation, or with the fallback Job and its single-Secret RBAC where it cannot; verify each derived Secret's content against Vault
- [x] 6.3 Add the auth service (init step rendering the policy from mounted Secrets, then serving) and the Traefik file-provider ConfigMap; verify on the local cluster that the policy loads, holds no credential value, and that a missing credential keeps the service from serving
- [x] 6.4 Add monolithic Mimir and Tempo for development as StatefulSets using the rendered configuration; verify on the local cluster that both become ready and accept and return a fixture
- [x] 6.5 Add the bucket and identity initialization Job and the Grafana provisioning Job; verify both complete and that a second run changes nothing
- [x] 6.6 Add the node collector DaemonSet and the cluster collector Deployment with the rendered configuration, without a privileged workload unless a datastream allows it; verify on the local cluster that node metrics arrive once
- [ ] 6.7 Add chart tests that template both profiles, validate every manifest against the pinned schema, and fail on an image that is not in the matrix by digest; verify they pass

## 7. Add-ons

- [x] 7.1 Implement the `k8s_addons` role: pull each chart, compare its digest with the matrix, install from the verified file in order, wait for health naming a failing add-on, and skip Longhorn and the Kafka and PostgreSQL operators for the development shape
- [ ] 7.2 Configure MetalLB with the inventory's address pool, Longhorn with the declared storage disks, and Traefik as the only service with an external address on the selected entry points; add a test that a planted wrong chart digest installs nothing
- [x] 7.3 Add `playbooks/k8s-addons.yml` and the inventory variables (kubeconfig, rendered directory, platform image); verify `ansible-lint`, the syntax check, and the repository checks for literals and inventory secrets pass
- [x] 7.4 Run the playbook against the local cluster for the development shape; verify cert-manager, Traefik, MetalLB, and the Vault Secrets Operator are healthy, exactly one service has an external address, and a second run changes nothing
- [x] 7.5 Document the add-ons, what each shape installs, and what was started versus rendered, in `docs/09-kubernetes.md`

## 8. Platform deployment

- [x] 8.1 Implement the `k8s_platform` role: refuse a profile that differs from the cluster shape, apply the network policies, install the releases from `releases.yaml` in order with health gates naming the release and workload that failed, run Grafana provisioning, and print the development profile's limits
- [x] 8.2 Add `playbooks/k8s-platform.yml` and `playbooks/k8s-teardown.yml` (releases removed, volumes and secrets kept, purge only with the cluster's name); verify lint, syntax, and repository checks pass
- [ ] 8.3 Write the development values for SeaweedFS, Loki, Pyroscope, and Grafana from their official charts in single-replica mode; verify they template and validate
- [ ] 8.4 Write the production values for SeaweedFS, the four backends, Grafana, the CloudNativePG cluster with separate databases and roles, and the Strimzi cluster with a topic and user per backend; verify they template and validate, and that replicas, budgets, and spread are present for every stateful component
- [x] 8.5 Deploy the development profile to the local cluster; verify every workload is ready, Grafana is provisioned, the output states the profile is not highly available, and a second run upgrades nothing and restarts no pod
- [x] 8.6 Document deployment, re-deployment after a changed document, teardown, and purge in `docs/09-kubernetes.md`; verify every documented command and playbook exists

## 9. Acceptance on the local cluster

- [x] 9.1 Add the opt-in suite's fixture: create the cluster, run the dev-mode Vault fixture, bootstrap it, build and load the platform image, render, and run both playbooks; delete the cluster afterwards
- [x] 9.2 Add cases for the four-signal round trip through the cluster collector and through Grafana, for two customers with two datastreams each; verify they pass
- [x] 9.3 Add cases for cross-tenant access, a spoofed tenant header, permission separation, a disabled signal, and the client certificate requirement on the external entry point; verify they pass
- [x] 9.4 Add cases for network isolation: a non-gateway pod is refused by a backend, the gateway is refused by object storage, a backend cannot leave the cluster, and no service other than the gateway has an external address; verify they pass
- [x] 9.5 Add cases for secret delivery: no Vault token or AppRole secret in any Secret or ConfigMap, a rotated ingestion credential accepted and the old one refused within the stated period, and a certificate issued with its key generated in the cluster; verify they pass
- [x] 9.6 Add cases for a changed override picked up without a restart, data surviving a deleted backend pod, and teardown followed by redeployment returning earlier telemetry, with an unconfirmed purge deleting nothing; verify they pass
- [x] 9.7 Record the measured resource requests of the development profile and the observed results in `docs/09-kubernetes.md`, and set each component's started-or-rendered field in the matrix from what the suite proved

## 10. Documents and plan

- [ ] 10.1 Complete `docs/09-kubernetes.md`: production topology, the small, medium, and large resource budgets, Kafka and PostgreSQL ownership, the Mimir chart exception, and the verification limits (production never installed, Longhorn and replication never exercised, high availability not observed)
- [x] 10.2 Update `docs/01-architecture.md` (secret delivery decided, the rule that the cluster reads Vault read-only, recorded deferrals), `docs/08-ansible.md` (what follows `k3s-cluster.yml`), `docs/03-diagrams.md`, the README, and `Plan.md` (todo 6 self-hosted half delivered, AWS half open); verify links resolve and the two deferral lists agree

## 11. Integration

- [x] 11.1 Run the unit test command and verify every test passes
- [x] 11.2 Run `ansible-lint`, the syntax check on every playbook, and the existing role scenarios whose roles changed; verify all pass
- [ ] 11.3 Run the chart tests for both profiles and verify no template error, schema violation, or unlisted image
- [x] 11.4 Run the local-cluster acceptance suite from a clean cluster and verify every case passes
- [x] 11.5 Run the Docker end-to-end suite and verify it still passes, since the renderer and the network contract changed
- [x] 11.6 Run `openspec validate --specs --strict`, `python -m nighthawk check-pins`, and `python -m nighthawk validate` on each example document, and verify all succeed
