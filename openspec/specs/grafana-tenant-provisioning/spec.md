# grafana-tenant-provisioning

## Purpose

Provisions one Grafana organization per customer tenant with
datastream-specific data sources and cross-signal correlations, so each
customer's users can explore only their own telemetry.

## Requirements

### Requirement: Deterministic desired state
The system SHALL render a Grafana desired-state document from a validated
platform document containing one organization per tenant and, within it, one
data source per datastream and enabled signal, and SHALL render it
deterministically without requiring any secret.

#### Scenario: Organizations and data sources
- **WHEN** desired state is rendered for two tenants, one with two datastreams
  enabling all four signals and one with a single datastream enabling only
  metrics and logs
- **THEN** it contains two organizations, with eight data sources in the first
  and two in the second

#### Scenario: Repeated render
- **WHEN** desired state is rendered twice from the same platform document
- **THEN** the two outputs are byte-identical

### Requirement: Stable data source identifiers
Each data source SHALL have a UID derived only from its backend ID and signal
that is stable across renders and valid for Grafana's UID format regardless of
backend ID length.

#### Scenario: Unchanged UID after unrelated edits
- **WHEN** a platform document gains another tenant
- **THEN** the UIDs of all previously rendered data sources are unchanged

#### Scenario: Longest permitted backend ID
- **WHEN** desired state is rendered for a datastream whose backend ID has the
  maximum permitted length
- **THEN** every data source UID satisfies Grafana's UID length and character
  constraints and no two data sources share a UID

### Requirement: Data sources query only through the gateway
Each data source SHALL address the gateway's query endpoint for its signal,
authenticate with its datastream's query credential, and SHALL NOT set a
tenant header or address a backend directly.

#### Scenario: Data source definition
- **WHEN** desired state is rendered
- **THEN** every data source URL is the gateway hostname, references the
  datastream's query credential by secret reference, and carries no
  `X-Scope-OrgID` setting

### Requirement: No cross-tenant access
The desired state SHALL NOT contain any data source that can read more than
one backend ID, and SHALL disable anonymous access.

#### Scenario: No federation
- **WHEN** desired state is rendered for any valid platform document
- **THEN** each data source is bound to exactly one datastream's query
  credential and each organization contains only its own tenant's data sources

### Requirement: Cross-signal correlations within a datastream
For each datastream, the system SHALL configure correlations between the
signals it enables: traces to logs, traces to metrics, traces to profiles,
logs to traces, and metric exemplars to traces. A correlation SHALL only
target a data source of the same datastream.

#### Scenario: All signals enabled
- **WHEN** desired state is rendered for a datastream enabling all four
  signals
- **THEN** its trace data source links to that datastream's log, metric, and
  profile data sources, and its log and metric data sources link to its trace
  data source

#### Scenario: Missing correlation target
- **WHEN** a datastream enables traces but not profiles
- **THEN** no traces-to-profiles correlation is rendered for it

### Requirement: Idempotent reconciliation
The system SHALL provide a command that applies the desired state to a running
Grafana through its administrative API, creating missing organizations and
data sources and updating changed ones, such that applying an unchanged
desired state a second time makes no changes.

#### Scenario: First apply
- **WHEN** an operator applies desired state to a Grafana that has none of the
  declared organizations
- **THEN** the system creates each organization and its data sources and
  reports what it created

#### Scenario: Second apply
- **WHEN** the same desired state is applied again
- **THEN** the system reports no changes

#### Scenario: Unreachable or unauthorized Grafana
- **WHEN** Grafana cannot be reached or rejects the administrative credential
- **THEN** the command fails explicitly, names the failing request, and does
  not print any credential

### Requirement: No destructive reconciliation by default
The reconciler SHALL NOT delete organizations, and SHALL remove a data source
that is no longer declared only when an explicit prune option is given,
reporting each undeclared item otherwise.

#### Scenario: Tenant removed from the platform document
- **WHEN** desired state no longer contains a previously provisioned tenant
  and the reconciler runs
- **THEN** the organization is left in place and reported as undeclared

#### Scenario: Dry run
- **WHEN** the reconciler runs in dry-run mode
- **THEN** it reports the changes it would make and sends no modifying request
