# NightHawk

A tenant-aware Grafana observability platform: metrics in Mimir, logs in
Loki, traces in Tempo, and profiles in Pyroscope, collected with Alloy,
reached through one authenticated TLS gateway, and explored in Grafana with
one organization per customer.

It is under construction. The Docker Compose profile runs end to end on one
machine. The AWS infrastructure is written and tested as plans only. The
Kubernetes workloads, host automation, dashboards, and alerting are not built
yet.

- **Try it locally:** [docs/00-quickstart.md](docs/00-quickstart.md). It needs
  a container runtime and starts a development HashiCorp Vault for secrets
  and certificates.
- **What it is and what has been verified:**
  [docs/01-architecture.md](docs/01-architecture.md) and
  [docs/07-docker-compose.md](docs/07-docker-compose.md).
- **Configuration and commands:** [docs/02-configuration.md](docs/02-configuration.md).
- **The plan and its amendments:** [Plan.md](Plan.md).
- **Accepted behaviour, capability by capability:** [openspec/specs/](openspec/specs/).

All documents are in [docs/](docs/).
