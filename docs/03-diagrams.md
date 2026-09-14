# Architecture and monitoring diagrams

These diagrams describe NightHawk's **target architecture**, including the
approved SeaweedFS and AWS storage decisions. They do not represent a deployed
platform.

The configuration validator/intermediate renderer, Pyroscope retention fragment,
and AWS storage Terraform modules are implemented and locally tested.
Runtime collection, gateway enforcement, dashboards, alerting, and full deployment
automation remain planned.

## 1. Platform architecture

The same logical observability services run in **one selected deployment
profile**. Local deployments use SeaweedFS backed by local disks/PVCs; AWS
deployments use S3. These are alternatives, not a storage replication chain.

```mermaid
flowchart TB
    subgraph Control["Configuration and provisioning"]
        Config["Platform, tenant and network contracts"]
        Renderer["Typed Python validator<br/>and intermediate renderer"]
        Secrets["SOPS + age<br/>planned secret lifecycle"]
        Terraform["Terraform object-storage facade<br/>AWS adapter implemented"]
        Config --> Renderer
        Secrets -. "credential and trust references" .-> Renderer
        Terraform -. "storage output contract" .-> Renderer
    end

    Sources["Applications, nodes, clusters,<br/>VMs and device bridges"]
    Alloy["Alloy collectors<br/>node and cluster discovery kept separate"]
    Users["Customer users"]

    subgraph Core["Logical platform - hosted in the selected runtime"]
        Ingress["TLS entry point<br/>Traefik + cert-manager on Kubernetes"]
        Gateway["Tenant gateway<br/>ingest/query permissions and tenant binding"]
        Grafana["Grafana<br/>one organization per customer"]

        subgraph Backends["Private signal backends"]
            Mimir["Mimir<br/>metrics"]
            Loki["Loki<br/>logs"]
            Tempo["Tempo<br/>traces"]
            Pyroscope["Pyroscope v2<br/>profiles"]
        end

        Kafka["Strimzi-managed Kafka<br/>distributed Tempo and Mimir ingest storage only"]
        StoreBinding["Separate backend buckets<br/>and storage identities"]
    end

    Sources --> Alloy
    Alloy -->|"HTTPS / OTLP<br/>mTLS for remote collectors"| Ingress
    Users -->|"HTTPS"| Ingress
    Ingress -->|"telemetry"| Gateway
    Ingress -->|"authenticated UI"| Grafana
    Grafana -->|"organization-scoped query credentials"| Gateway
    Gateway --> Mimir
    Gateway --> Loki
    Gateway --> Tempo
    Gateway --> Pyroscope
    Mimir <-->|"production ingest log"| Kafka
    Tempo <-->|"distributed ingest log"| Kafka
    Mimir --> StoreBinding
    Loki --> StoreBinding
    Tempo --> StoreBinding
    Pyroscope --> StoreBinding
    Renderer -. "planned policy and provisioning integration" .-> Core

    subgraph Local["Local deployment - Docker Compose or self-hosted k3s"]
        LocalRuntime["Compose: monolithic, non-HA<br/>k3s: Cilium, MetalLB and Longhorn"]
        Seaweed["SeaweedFS S3<br/>masters, volumes, filers and S3 frontends"]
        LocalDisks["Local named volumes / disks / PVCs<br/>persist object data and metadata"]
        Postgres["Self-hosted production: shared HA PostgreSQL<br/>separate Grafana and filer databases / credentials"]
        Seaweed --> LocalDisks
        Seaweed -->|"production filer metadata"| Postgres
        Postgres --> LocalDisks
    end

    subgraph Cloud["Cloud deployment - AWS"]
        EKS["Multi-AZ EKS<br/>on-demand capacity for stateful workloads"]
        S3["Private S3 buckets<br/>per-backend KMS encryption"]
        IRSA["Per-backend IRSA roles<br/>bucket-scoped permissions"]
        CloudDB["HA PostgreSQL for multi-replica Grafana<br/>no SeaweedFS metadata database"]
    end

    LocalRuntime -. "hosts" .-> Core
    EKS -. "hosts" .-> Core
    StoreBinding -->|"Docker / self-hosted"| Seaweed
    StoreBinding -->|"AWS: private service connectivity"| S3
    IRSA -. "authorizes backend S3 access" .-> StoreBinding
    Grafana -. "self-hosted production" .-> Postgres
    Grafana -. "AWS multi-replica" .-> CloudDB
    Terraform -. "provisions when authorized" .-> S3
    Terraform -. "provisions when authorized" .-> IRSA

    classDef implemented fill:#dbeafe,stroke:#2563eb,color:#172554
    class Config,Renderer,Terraform implemented
```

**Reading the diagram**

- Blue nodes identify implemented configuration/module code, not deployed
  infrastructure. Dotted arrows show hosting, provisioning, or configuration
  relationships; solid arrows show application or storage paths.
- The gateway selects a stable backend tenant ID for each authorized
  customer/datastream pair. Clients cannot choose arbitrary tenant IDs.
- Raw backend endpoints and storage remain private. Kubernetes uses
  default-deny NetworkPolicies with explicit allowances.
- Compose is Kafka-free. Kafka is part of the selected distributed Kubernetes
  backend architectures, not a dependency of Loki or Pyroscope.
- The self-hosted HA PostgreSQL cluster is shared to reduce resource usage.
  Separate databases and credentials do not eliminate its shared failure domain.
- The diagram omits the separately protected Terraform state/bootstrap store.
  It is not a telemetry bucket and must not be included in ordinary teardown.

## 2. How monitoring works

This sequence follows telemetry from collection through tenant-aware ingestion,
storage, investigation, and alert delivery. `Signal backends` represents Mimir,
Loki, Tempo, and Pyroscope; their internal distributed components are condensed.

```mermaid
sequenceDiagram
    autonumber
    participant Sources as Apps / infrastructure / platform itself
    participant Alloy as Alloy collectors
    participant Gateway as Tenant gateway
    participant Backends as Signal backends
    participant Storage as SeaweedFS locally / S3 on AWS
    participant Grafana as Grafana and alert evaluation
    participant Receiver as Webhook receiver

    loop Continuous collection
        Alloy->>Sources: Discover targets, scrape metrics and read configured logs
        Sources-->>Alloy: Collected metrics and logs
        Sources->>Alloy: Push SDK telemetry to configured receivers
        Note over Sources,Alloy: Profiling uses supported SDK / collector integrations.<br/>Privileged eBPF collection is optional and separate.
        Alloy->>Alloy: Apply approved field dropping, relabeling and signal routing
        Alloy->>Alloy: Buffer with bounded queues and retry/backpressure policy
        Alloy->>Gateway: Send telemetry with ingestion credentials over TLS
        Gateway->>Gateway: Validate credentials, tenant mapping and remote mTLS identity
        alt Unauthorized identity, spoofed header or disallowed signal
            Gateway-->>Alloy: Reject request and report the failure
        else Authorized ingestion
            Gateway->>Gateway: Bind the request to its tenant/datastream backend ID
            Gateway->>Backends: Route the enabled signal with trusted tenant context
            Note over Gateway,Backends: Metrics to Mimir, logs to Loki,<br/>traces to Tempo, profiles to Pyroscope.
            Backends-->>Gateway: Ingestion response
            Gateway-->>Alloy: Acknowledge or return an explicit ingestion error
        end
    end

    loop Backend persistence and compaction
        Backends->>Storage: Write blocks / objects to the backend's own buckets
        Storage-->>Backends: Storage result
        Note over Backends,Storage: Persistence timing and acknowledgement semantics vary by backend.<br/>Distributed Tempo / Mimir may use Kafka before object storage.
    end

    Note over Grafana: A customer opens dashboards or Explore<br/>inside their Grafana organization.
    Grafana->>Gateway: Query using organization/datastream-scoped query credentials
    Gateway->>Gateway: Validate query permission and bind the trusted backend ID
    alt Query denied
        Gateway-->>Grafana: Explicit authorization error
    else Query authorized
        Gateway->>Backends: Execute tenant-scoped queries
        Backends->>Storage: Read persisted data as needed, alongside recent backend data
        Storage-->>Backends: Stored telemetry
        Backends-->>Gateway: Query results or explicit backend error
        Gateway-->>Grafana: Authorized results or error
        Grafana->>Grafana: Display dashboards and correlate metrics, logs, traces and profiles
    end

    loop Scheduled alert evaluation
        Grafana->>Gateway: Query alert inputs with scoped query credentials
        Gateway->>Backends: Execute authorized alert queries
        Backends-->>Gateway: Results or backend error
        Gateway-->>Grafana: Results, no data or explicit error
        Grafana->>Grafana: Evaluate thresholds / burn rates and configured no-data/error handling
        alt Notification policy requires delivery
            Grafana->>Receiver: Send configured webhook notification
            Receiver-->>Grafana: Delivery response
        end
    end

    loop Retention and deletion workers
        Backends->>Backends: Select explicit retention for each tenant/datastream
        Backends->>Storage: Delete expired data after backend processing windows
        Note over Backends,Storage: AWS noncurrent versions and backups can outlive backend deletion.<br/>Verify actual removal. Configuration alone is not proof.
    end
```

**Important monitoring details**

1. **Collection is not only scraping.** Metrics can be scraped, SDK telemetry can
   be pushed, logs need configured readers/receivers, and profiles need supported
   instrumentation or profiling collectors.
2. **Isolation applies to writes and reads.** Ingestion-only credentials cannot
   query data. Grafana organizations have datastream-specific data sources and
   cannot enable cross-customer queries through client-supplied headers.
3. **Redaction is a processing requirement, not a guarantee shown by an arrow.**
   Supported transformations must be tested with sensitive fixtures; profiling
   payload sanitization has additional limits.
4. **Alerts need an external destination.** Development uses a test webhook
   receiver; production requires a real configured destination. An optional
   external dead-man monitor covers failures of the shared monitoring platform.
5. **The platform monitors itself.** Collectors, gateway, backends, Kafka,
   databases, and storage must expose operational signals through the same
   collection design. External failure detection remains important.

See [architecture decisions](01-architecture.md) and
[implemented configuration tooling](02-configuration.md) for details and
current implementation boundaries.
