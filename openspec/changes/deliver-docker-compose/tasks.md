# Tasks

## 1. Runtime feasibility and pins

- [x] 1.1 Start a user-level Podman API socket for the session and confirm
      the Compose CLI can drive it with a two-service probe file that uses
      an `internal` network, a health check, `depends_on` with
      `service_healthy` and `service_completed_successfully`, a named
      volume, a loopback port binding, and `up --wait`; record which
      features work and any workaround in new `docs/07-docker-compose.md`.
      Verify the probe stack starts, reports healthy, and is removed.
- [x] 1.2 Start pulling the pinned Pyroscope 2.3.1, Alloy 1.20.1, Traefik
      3.7.13, SeaweedFS 4.47, and a current Python 3.12 slim base image as
      background steps; add a `container_images` section (repository, tag,
      digest) to `config/versions.yaml` and its schema for these and for
      Mimir, Loki, Tempo, and Grafana, with digests resolved from the
      registry. Verify every image is present locally and its digest equals
      the matrix value, including the four that were already pulled.
- [x] 1.3 Add per-platform SHA-256 checksums for the pinned `sops` and
      `age` release files to `config/versions.yaml` and its schema, taken
      from each release's published checksums; verify with a
      `tests/test_versions.py` case that a missing checksum fails
      validation.
- [x] 1.4 Confirm by running the already-pulled images that Mimir 3.2.1 and
      Tempo 3.0.3 start as a single Kafka-free process with a minimal
      configuration, and record the selecting flags or settings with their
      pinned-reference source in `docs/07-docker-compose.md`. If either
      cannot, stop and raise it before continuing, because it contradicts
      the Kafka-free Compose decision.
- [x] 1.5 For each of the nine images, list the binaries usable for a health
      probe (application subcommand, `wget`, `curl`, shell) by inspecting
      the image, and record the chosen probe or "external wait" per service
      in `docs/07-docker-compose.md`. Verify each chosen in-image probe
      command runs in its image.

## 2. Contract additions

- [x] 2.1 Add the required `grafana` block (`hostname`,
      `admin_secret_ref`) and `gateway.upstreams.grafana` to
      `config/platform.schema.json`, add the `gateway` to `grafana`
      (tcp/3000) network rule, extend `nighthawk/config.py` with a `Grafana`
      dataclass and cross-checks (hostname differs from the gateway's,
      secret resolves and is not reused, upstream port matches the network
      rule), and update `config/tenants.example.yaml` and the test
      fixtures. Verify with new `tests/test_config.py` rejection cases and
      a passing full suite.
- [x] 2.2 Define storage identity secrets as JSON with `access_key` and
      `secret_key`, and add `generate-storage-identity` to
      `nighthawk/trust.py` and the CLI behind the `doctor` gate. Verify with
      tests: a declared identity is stored in that shape, an existing value
      is not replaced, a secret that is not a storage identity is refused,
      and neither key appears in output.
- [x] 2.3 Extend `issue-certificate`: `--server` issues for the gateway and
      Grafana hostnames, and new `--storage` issues for exactly the storage
      binding endpoint hostnames. Verify with tests that parse the issued
      certificates' DNS names and extended key usage, and that
      `--storage` fails for a document whose bindings are not local
      secret-identity bindings.
- [x] 2.4 Update `docs/02-configuration.md` (grafana block, upstream,
      storage identity shape) and the trust section of
      `docs/05-gateway.md` (certificate names, `--storage`,
      `generate-storage-identity`); verify each documented command runs
      with `--help`.

## 3. Gateway and collector changes

- [x] 3.1 Add the Grafana UI router and service to `nighthawk/gateway.py`
      (one router per entry point, `Host(grafana.hostname)`, no auth or
      tenant middleware) and list it in `routes.md`. Verify with
      `tests/test_gateway.py`: the UI router exists per entry point with no
      `forwardAuth`, every signal-backend router still has exactly one, no
      signal-backend router matches the UI hostname, and output stays
      deterministic.
- [x] 3.2 Add `--otlp-only` to `render-collector`, moving
      `pyroscope.receive_http` to a shared source that OTLP-only rendering
      keeps. Verify with `tests/test_collector.py` (no discovery, scrape of
      hosts, socket path, or cAdvisor component; redaction and delivery
      present for every enabled signal; pushed profiles still forwarded
      through redaction) and with `alloy fmt --test` and `alloy validate`
      using `.tools/alloy-linux-amd64` on the shared source and a rendered
      OTLP-only collector.
- [x] 3.3 Update `docs/04-collection.md` and `docs/05-gateway.md` for the
      OTLP-only option and the UI route; verify the documented
      `render-collector --otlp-only` command runs as written.

## 4. Backend static configuration

- [x] 4.1 Create `nighthawk/backends.py` with a reviewed-pin record per
      backend and renderers for Mimir, Loki, Tempo, and Pyroscope
      single-process configuration as decided in design.md, confirming each
      setting against the pinned configuration reference and recording
      setting, value, and source URL in `docs/07-docker-compose.md`. Have
      `render-contracts` write `backends/<name>.yaml` for
      `deployment: docker` and print an explicit skip message otherwise.
      Verify with `tests/test_backends.py`: deterministic output, buckets
      equal the backend's bindings, no credential value, multitenancy and
      federation settings, runtime-override path and retention worker
      present, HTTP port equals the network contract, and an unreviewed pin
      or architecture fails naming the backend.
- [x] 4.2 Validate each rendered configuration with its own binary where
      the pinned image offers a check (`-config.verify`, `-modules`,
      `--help` parse, or a short start that exits on bad configuration);
      verify each exits zero, or record in the docs that the image has no
      such check.

## 5. Local object storage

- [x] 5.1 Confirm against SeaweedFS 4.47 documentation and the pulled image
      the S3 identity file format, per-bucket action syntax, how anonymous
      access is disabled, the S3 TLS flags, and the bucket-creation command;
      record them with sources in `docs/07-docker-compose.md`. Verify by
      running the image standalone with a hand-written identity file: an
      allowed identity can put and get in its bucket, is denied in another
      bucket, and an anonymous request is denied.
- [x] 5.2 Create `nighthawk/storage.py` rendering the identity
      configuration from materialized storage identities into the secrets
      directory with mode `0600`, and the list of buckets for
      initialization. Verify with `tests/test_storage.py`: one identity per
      binding identity, actions limited to that backend's buckets, no
      anonymous identity, a malformed identity secret fails naming the
      reference without printing it, and output is deterministic.

## 6. Compose stack

- [x] 6.1 Write `docker-compose/nighthawk.Dockerfile` (pinned base by
      digest, pinned requirements, non-root user) and a `.dockerignore`
      that excludes secrets, `.tools`, `.generated`, and
      `.materialized-secrets`. Verify the image builds under Podman, runs
      `python -m nighthawk --help` as a non-root UID, and contains no file
      from the excluded directories.
- [x] 6.2 Write `docker-compose/docker-compose.yaml` and
      `docker-compose/.env.example` per design.md: services, four networks
      with three internal, the single loopback port, named volumes, restart
      policies, memory limits, non-root users, health checks or `wait-*`
      one-shots per task 1.5, `storage-init` and `grafana-init`, and the
      `host-collection` and `sample` profiles. Add one `tracked_consumers`
      entry per image. Verify `compose config` succeeds, `check-pins`
      passes, and a new `tests/test_compose.py` asserts from the resolved
      file: one published port bound to loopback, no privileged service or
      runtime socket outside `host-collection`, every long-running service
      has a restart policy, no Kafka service, no secret value in the Compose
      file or `.env.example`, and the auth service's networks are only
      those shared with Traefik and Alloy.
- [x] 6.3 Write `docker-compose/docker-compose.s3.yaml` removing SeaweedFS
      and `storage-init` and their dependencies. Verify `compose config`
      with both files succeeds and a test asserts no SeaweedFS service
      remains and no backend depends on a removed service.

## 7. Quickstart and teardown

- [x] 7.1 Add `fetch-tools` (download pinned `sops` and `age` into
      `.tools/` with checksum verification, discarding mismatches). Verify
      with tests using a fake downloader (match installs, mismatch discards
      and fails naming the tool) and by running it for real, after which
      `nighthawk doctor` with `.tools` on `PATH` reports both tools ok.
- [x] 7.2 Create `nighthawk/quickstart.py` with `quickstart-docker` and
      `teardown-docker` as designed, with the container and Compose
      commands injected. Verify with `tests/test_quickstart.py` using fakes:
      a failed preflight creates nothing, a first run creates each missing
      secret, identity, CA, and certificate exactly once, a second run
      creates none, rendering replaces the previous output atomically, an
      unhealthy service is named in the error, ordinary teardown never
      passes a volume-removal flag, and purge requires confirmation.
- [x] 7.3 Run `quickstart-docker` for real under Podman from a state with no
      generated secrets and empty volumes. Verify every service is healthy,
      the output names the sample tenant and labels its retention as an
      example, `git status` shows no untracked secret material, and a
      second run exits zero having created nothing new.

## 8. Sample workload and fixtures

- [x] 8.1 Add `emit-fixtures` (standard-library OTLP HTTP JSON for metrics,
      logs, and traces, and a checked-in pprof fixture pushed to the
      collector) with the documented fixture set, run identifier, and
      sensitive markers under each drop field. Verify with unit tests
      against a local fake receiver: payload shape per signal, the run
      identifier on every item, every drop field carrying a unique marker
      in each documented position, and a non-2xx response producing a
      non-zero exit naming the signal and status.
- [ ] 8.2 Add `sample-workload/` (instrumented service, pinned
      requirements, Dockerfile, non-root) and wire it to the Compose
      `sample` profile. Verify the image builds, and with the stack running
      and the profile enabled each of the four data sources returns data
      from it after the documented warm-up.

## 9. End-to-end verification

- [x] 9.1 Add `tests/e2e/platform.yaml` (two tenants, two datastreams each,
      one with fewer signals) and the `tests/e2e/` cases listed in
      design.md, skipped unless `NIGHTHAWK_E2E=1`. Verify the default suite
      still passes without containers and reports the e2e cases as skipped.
- [x] 9.2 Start the stack from the e2e document and run the e2e cases.
      Verify each passes, and for any that fails decide with evidence
      whether the platform or the test is wrong; fix platform defects, and
      record anything attributable to Podman as a documented limitation
      instead of weakening the assertion.
- [ ] 9.3 Measure cold start (empty volumes, images present) and warm start
      (existing volumes) of the quickstart three times each, and peak
      memory per service. Verify the numbers, machine, and runtime are
      recorded in `docs/00-quickstart.md`.
- [x] 9.4 Set `runtime_verified: true` in `config/versions.yaml` only for
      components the e2e run exercised, noting in the docs that
      verification was under rootless Podman. Verify `check-pins` and the
      versions tests pass.

## 10. Documentation and closing checks

- [ ] 10.1 Write `docs/00-quickstart.md` (prerequisites including the
      Podman socket setup and resource assumptions, the one command,
      expected output, where to log in, teardown, purge, troubleshooting)
      and complete `docs/07-docker-compose.md` (topology and networks,
      volumes, health and start order, privilege exceptions, backend
      settings table, object storage, S3 override, host-collection profile,
      observed results and Podman differences, what remains unverified such
      as retention deletion). Verify every command in both documents by
      running it as written.
- [ ] 10.2 Update `docs/01-architecture.md`, `docs/03-diagrams.md`, and
      the "What is still unproven" and "Validation" sections of
      `docs/04` to `docs/06` to reflect what was observed at runtime and
      what was not. Verify no document claims Docker Engine or production
      verification.
- [ ] 10.3 Run the unit suite, `check-pins`, `compose config` for both file
      sets, and `openspec validate deliver-docker-compose --strict`; verify
      all succeed, then run `teardown-docker` and confirm no NightHawk
      container is left running while the volumes remain.
