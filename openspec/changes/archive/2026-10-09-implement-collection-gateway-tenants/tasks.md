# Tasks

## 1. Contract and compatibility-matrix foundations

- [x] 1.1 Resolve the current stable Grafana Alloy release from
      https://github.com/grafana/alloy/releases, add `collectors.alloy`
      (`version`, `source`, `runtime_verified: false`) to
      `config/versions.yaml` and to `config/versions.schema.json` as a
      required section, and verify with a new `tests/test_versions.py` test
      that the real matrix loads and that removing the pin fails validation.
- [x] 1.2 Add `cryptography` (a release supporting Python 3.12 and 3.14) and
      its transitive dependencies, exactly pinned, to `requirements.txt`;
      verify `pip install -r requirements.txt` succeeds in a fresh venv and
      the existing suite still passes.
- [x] 1.3 Add the required `gateway` object to `config/platform.schema.json`
      (hostname, `entry_points`, `client_ca_secret_ref`, optional
      `client_ca_key_secret_ref`, `auth_service`, `upstreams`,
      `revoked_certificate_fingerprints` as 64-char lowercase hex), tighten
      `drop_fields` items to `^[A-Za-z0-9_.-]+$`, and replace the metrics
      signal's `ingestion_rate_bytes_per_second` with
      `ingestion_rate_samples_per_second`; verify
      `test_schema_documents_are_valid` passes.
- [x] 1.4 Add private rules to `config/network.yaml` for `gateway` to
      `auth-service` (9180), `mimir` (8080), `loki` (3100), `tempo` (3200,
      4317, 4318), `pyroscope` (4040), and `grafana` to `gateway` (443);
      verify `load_network` accepts the file and
      `tests/test_terraform_boundaries.py` still passes unchanged.
- [x] 1.5 Extend `nighthawk/config.py` with a `Gateway` dataclass on
      `Platform` and the cross-checks from design.md (entry points are
      gateway-destined network rules, upstream and auth-service ports match
      network rules, an upstream exists for every enabled signal, secret
      references resolve, metrics unit); `load_platform` takes the network
      rules it needs. Verify with new `tests/test_config.py` cases for each
      rejection (unknown entry point, port not in contract, missing
      upstream, malformed fingerprint, bytes-based metrics budget, unsafe
      drop-field name) and one for two ingestion credentials on the same
      pair being valid.
- [x] 1.6 Update `config/tenants.example.yaml` and the fixtures in
      `tests/test_config.py` and `tests/test_secrets.py` for the new
      contract; verify `python -m nighthawk validate --config
      config/tenants.example.yaml` succeeds and the full suite passes.
- [x] 1.7 Update the "Tenant and credential contract" section of
      `docs/02-configuration.md` for the gateway block, the metrics unit,
      and the drop-field alphabet; verify every field in the schema's
      `gateway` object is described.

## 2. Runtime tenant overrides

- [x] 2.1 Check each candidate override key in design.md's mapping table
      against the pinned version's configuration reference (Mimir 3.2.1,
      Loki 3.7.8, Tempo 3.0.3, Pyroscope 2.3.1 at their release tags),
      including whether a per-tenant key enforces `query_concurrency` on
      each backend, and record key, unit, and source URL per backend in a
      table in new `docs/06-tenant-provisioning.md`. Verify every table row
      has a source URL at the pinned tag. If Mimir 3.2.1 turns out to
      enforce a per-tenant byte rate, stop and raise it before continuing,
      because it invalidates the samples-per-second contract decision.
- [x] 2.2 Create `nighthawk/overrides.py` with a reviewed-pin record and
      field mapping per backend, rendering Mimir, Loki, Tempo, and Pyroscope
      override documents keyed by backend ID, and the unenforced-limits
      report; keep `pyroscope_overrides` importable from
      `nighthawk.retention`. Verify with `tests/test_overrides.py`: two
      tenants with two datastreams each yield four independent entries per
      enabled backend, a disabled signal yields no entry, no document has a
      default or wildcard entry, every declared value is in an override or
      the report, and a changed backend pin fails naming the backend.
- [x] 2.3 Have `render-contracts` write `mimir-overrides.yaml`,
      `loki-overrides.yaml`, `tempo-overrides.yaml`, and
      `unenforced-limits.json` alongside the existing Pyroscope fragment;
      verify by extending `test_rendering_is_deterministic_and_does_not_overwrite`
      to cover the new files.
- [x] 2.4 Document in `docs/06-tenant-provisioning.md` how each backend
      loads its override file, which static backend settings retention
      depends on (for Phases 4 and 6), which limits stay at backend
      defaults, and that a rendered value is not proof of deletion; verify
      the documented `render-contracts` command produces the listed files.

## 3. Gateway route model and Traefik configuration

- [x] 3.1 Confirm against the pinned backend API references the ingestion
      and query paths for each signal (remote write, Loki push, OTLP HTTP
      paths on Mimir, Loki, and Tempo, Tempo's OTLP gRPC service name,
      Pyroscope push and query services, and each query API prefix), and
      against the Traefik 3.7.13 reference the `forwardAuth`,
      `passTLSClientCert`, and TLS-option field names, the forwarded
      certificate header name and encoding, and that Traefik overwrites a
      client-supplied copy of that header. Record the results with source
      URLs in new `docs/05-gateway.md`; verify every route-table row and
      every Traefik field used has a cited source.
- [x] 3.2 Create `nighthawk/gateway.py` with the single route table and a
      renderer producing Traefik file-provider dynamic configuration (one
      router per selected entry point and route row, `forwardAuth` address
      carrying entry, signal, and permission, prefix strip or path rewrite,
      `passTLSClientCert`, TLS option with the client CA) and static
      configuration (TLS-only entry points on the selected network-rule
      ports). Verify with `tests/test_gateway.py`: every router has the
      auth middleware, there is no catch-all router, entry-point ports equal
      the selected network rules' ports, routes exist only for signals that
      have an upstream, and output is deterministic.
- [x] 3.3 Have `render-contracts` write `gateway/traefik-dynamic.yaml`,
      `gateway/traefik-static.yaml`, and `gateway/routes.md` (generated from
      the route table); verify the rendered files contain no secret value
      and that `test_render_contracts_succeeds_without_secrets_files_present`
      still passes.

## 4. Auth service and policy bundle

- [x] 4.1 Create `nighthawk/authz.py` with the policy-bundle loader and
      validator and the pure `decide` function following design.md's
      evaluation order, including URI-SAN extraction and fingerprinting of
      the forwarded certificate. Verify with `tests/test_authz.py` covering
      one case per scenario in `specs/tenant-gateway/spec.md`: absent and
      matching tenant header, different tenant, multi-tenant syntax,
      repeated header, missing, malformed, unknown, and wrong-secret
      credentials returning identical responses, ingest-on-query and
      query-on-ingest, disabled signal, matching, mismatched, missing, and
      revoked certificate, certificate-less ingest on a restricted-external
      entry point, and two tenants with two datastreams each where no
      credential obtains another pair's backend ID.
- [x] 4.2 Add `render-gateway-policy` to `nighthawk/__main__.py`, reading
      materialized credential secrets and writing the digest-only bundle
      with mode `0600` via atomic rename, rejecting secrets shorter than 32
      characters by credential ID. Verify with tests that the bundle
      contains no plaintext secret, has owner-only permissions, fails
      naming the credential for a short or missing secret without printing
      it, and that removing a credential from the document removes it from
      the bundle.
- [x] 4.3 Add the `serve-authz` command: a standard-library HTTP adapter
      exposing `/verify`, `/healthz`, and `/metrics`, exiting non-zero on a
      missing or invalid policy at start, reloading on `SIGHUP`, and keeping
      the last valid policy with a reload-failure metric on a bad reload.
      Verify with tests that start the server on an ephemeral loopback port:
      an allowed request returns `200` with the bound `X-Scope-OrgID`,
      denied requests return `401`/`403` with no tenant header, an invalid
      policy prevents start, and a bad reload keeps the old policy and
      flips the health output.
- [x] 4.4 Complete `docs/05-gateway.md`: request flow, the generated route
      table, the decision order and status codes, the requirement that only
      Traefik can reach the auth service, the reading of "missing tenant
      header", policy render and reload commands with expected output,
      troubleshooting, and the runtime checks owed by Phase 4
      (forwarded-certificate header spoofing, load). Verify each documented
      command runs as written against `config/tenants.example.yaml`, using
      a temporary secrets directory for the policy render.

## 5. Gateway trust lifecycle

- [x] 5.1 Create `nighthawk/trust.py` and the `generate-credential` command
      (32-byte URL-safe token stored through `encrypt_secret`, refusing an
      existing key, behind the `doctor` gate). Verify with tests using the
      injected runner pattern from `tests/test_secrets.py`: a declared
      credential is encrypted at its file/key, an undeclared credential and
      an existing key both fail without writing, and the plaintext never
      appears in stdout.
- [x] 5.2 Add `init-ca` (ECDSA P-256 CA, key SOPS-encrypted at
      `client_ca_key_secret_ref`, certificate at `client_ca_secret_ref`,
      `production` refused without `--confirm-local-ca`). Verify with tests
      for the development path, the production refusal, and that a
      document without `client_ca_key_secret_ref` fails explicitly.
- [x] 5.3 Add `issue-certificate` for collector certificates (sole SAN is
      the credential's declared URI identity, clientAuth only) and gateway
      server certificates (`gateway.hostname`, serverAuth only), with
      required `--valid-days`, `0600` key output, refusal to overwrite, and
      the fingerprint printed. Verify with tests that parse the issued
      certificate: SAN, EKU, and validity are as requested, an undeclared
      identity and a missing validity both fail, and the printed
      fingerprint equals the one `authz` computes for the same certificate.
- [x] 5.4 Add an end-to-end unit test of rotation and revocation across
      `trust`, `render-gateway-policy`, and `decide`: overlapping
      credentials both accepted, the removed one rejected after re-render,
      a renewed certificate with the same identity accepted without a
      document change, and a fingerprint added to the revoked list rejected
      while a sibling certificate is accepted. Verify the test passes.
- [x] 5.5 Add a "Trust lifecycle" section to `docs/05-gateway.md` with
      command sequences, expected results, and rollback for first issuance,
      credential rotation, certificate renewal, and revocation, and the
      production prerequisite of an operator-supplied CA. Verify the
      sequences match the implemented command names and flags by running
      each with `--help`.

## 6. Alloy collection

- [x] 6.1 Confirm against the pinned Alloy version's component reference
      the components and arguments design.md relies on
      (`prometheus.remote_write` queue and WAL settings, `loki.write` and
      `loki.process` stages, `otelcol.receiver.otlp`,
      `otelcol.processor.memory_limiter`, `otelcol.processor.transform`
      with `delete_matching_keys`, `otelcol.exporter.otlphttp` queue and
      retry settings, `pyroscope.write`, `pyroscope.relabel`,
      `pyroscope.ebpf`, `basic_auth` `password_file`, client TLS file
      arguments, clustering, Kubernetes discovery field selectors) and
      record them with source URLs in new `docs/04-collection.md`. Verify
      every component used later in this group appears in that table.
- [x] 6.2 Write the static source files under `alloy-configs/` for the
      `docker`, `k8s-node`, `k8s-cluster`, `remote-cluster`, `vm`, and
      `external-service` profiles, split by signal and forwarding only to
      the fixed `*.redact` receivers, plus `alloy-configs/profiling-ebpf/`
      and an `alloy-configs/README.md` naming the pinned Alloy version; add
      that README to `tracked_consumers`. Verify with
      `tests/test_collector.py` that static files reference no exporter and
      no secret, that no base profile contains an eBPF component, that
      `k8s-node` and `k8s-cluster` declare disjoint discovery roles and job
      names with node-scoped selectors in `k8s-node`, and that
      `python -m nighthawk check-pins` passes.
- [x] 6.3 Create `nighthawk/collector.py` and the `render-collector` command
      generating `datastream.alloy` (OTLP gRPC and HTTP receivers for
      enabled signals, memory limiter, redaction for every drop field in
      every enabled pipeline, bounded exporters to
      `https://<gateway.hostname>`, basic auth by `password_file`, client
      certificate arguments for the three remote profiles, collector
      self-scrape, optional `--self-monitoring`) into a new directory.
      Verify with tests for each scenario in
      `specs/alloy-collection/spec.md`: deterministic output, unknown
      profile or pair rejected with no output, disabled signal absent,
      every exporter has explicit queue and retry bounds, every drop field
      present case-insensitively in every enabled pipeline, exporters
      referenced only by redaction components, no `X-Scope-OrgID` and no
      secret value, remote profile without a certificate identity rejected,
      eBPF overlay refused unless `allow_privileged_profiling` is true and
      profiles are enabled, and self-monitoring scrapes present only with
      the flag.
- [x] 6.4 Fetch the pinned Alloy binary for this platform into a
      Git-ignored `.tools/` directory, verifying the published checksum,
      and run `alloy fmt` over every file in `alloy-configs/` and over a
      rendered collector for each profile from
      `config/tenants.example.yaml`; fix any reported error. Verify all
      runs exit zero, or, if the binary cannot be obtained, record in
      `docs/04-collection.md` that the configurations are syntax-unverified
      and why.
- [x] 6.5 Complete `docs/04-collection.md`: what each profile collects and
      what still needs SDKs, kube-state-metrics, or exporters, the node and
      cluster split, required environment variables and file paths, queue
      and retry bounds and what is dropped when they are exceeded,
      redaction coverage and its documented limits (free-text bodies,
      profile payloads, hashing), the privileged profiling overlay, and the
      one-collector-per-datastream constraint. Verify the documented
      `render-collector` command runs as written for each profile.

## 7. Grafana tenant provisioning

- [x] 7.1 Create `nighthawk/grafana.py` with the pure `desired_state`
      render (organizations, data sources with hashed stable UIDs, gateway
      URLs, query-credential references, same-datastream correlations) and
      have `render-contracts` write `grafana/desired-state.json`. Verify
      with `tests/test_grafana.py` for each desired-state scenario in
      `specs/grafana-tenant-provisioning/spec.md`: organization and data
      source counts, byte-identical re-render, UIDs unchanged when a tenant
      is added, UID validity and uniqueness for a 63-character backend ID,
      no `X-Scope-OrgID` and no backend address, each organization holding
      only its tenant's data sources, and correlations omitted when the
      target signal is disabled.
- [x] 7.2 Confirm against the Grafana 13.2 HTTP API reference the
      organization and data source endpoints, the organization-selection
      mechanism for admin requests, the UID length limit, and the
      correlation `jsonData` field names per data source type; record them
      with source URLs in `docs/06-tenant-provisioning.md`. Verify every
      endpoint and field the reconciler uses is listed.
- [x] 7.3 Add the `provision-grafana` command with an injectable HTTP
      client, `--dry-run`, and `--prune`, reading query-credential secrets
      from the materialized directory and the admin password from a file.
      Verify with tests against an in-memory fake Grafana: first apply
      creates everything and reports it, second apply reports no changes,
      a changed data source is updated in place, an undeclared organization
      is reported and never deleted, an undeclared data source is removed
      only with `--prune`, dry run sends no modifying request, and an
      unreachable or unauthorized Grafana fails naming the request without
      printing a credential.
- [x] 7.4 Complete the Grafana section of `docs/06-tenant-provisioning.md`:
      prerequisites, the provisioning command with expected output,
      required Grafana settings for Phases 4 and 6 (anonymous access off,
      customers not granted organization admin), why isolation is enforced
      at the gateway, prune and rollback behavior, and troubleshooting.
      Verify the documented dry-run command runs as written against the
      fake-Grafana test fixture or with `--help`.

## 8. Architecture alignment and integration checks

- [x] 8.1 Update `docs/01-architecture.md` (gateway as Traefik plus the
      auth service, the route and policy outputs, the per-datastream
      collector model), the Status section of `docs/02-configuration.md`
      (what is now rendered and enforced, and what remains unproven at
      runtime), and `docs/03-diagrams.md` (auth service node, implemented
      styling for collection, gateway, overrides, and Grafana provisioning,
      corrected status paragraph). Verify the Mermaid blocks still parse
      and that no document claims runtime verification.
- [x] 8.2 Run `python -m unittest discover -s tests -p "test_*.py"` and
      `python -m nighthawk check-pins`; verify both succeed with no skipped
      tests other than ones explicitly documented as needing an absent
      binary.
- [x] 8.3 From `config/tenants.example.yaml`, run `render-contracts`, then
      `render-collector` for every profile, and confirm the pieces agree:
      the collector's gateway URL and credential ID, the Traefik entry
      points, the policy bundle's backend ID, the override documents' keys,
      and the Grafana data source URLs all refer to the same hostname,
      ports, and backend ID. Verify with a single cross-artifact test added
      to the suite.
- [x] 8.4 Run `openspec validate implement-collection-gateway-tenants
      --strict` and verify it reports valid; list in the change summary
      every check that could not run here (Alloy, Traefik, Grafana, SOPS,
      and age are not installed) as an explicit limitation.
