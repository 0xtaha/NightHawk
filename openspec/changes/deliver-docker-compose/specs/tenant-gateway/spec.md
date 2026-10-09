# Spec Delta

## ADDED Requirements

### Requirement: Grafana UI through the gateway listener
The gateway SHALL route requests for the Grafana UI hostname to Grafana over
TLS on the selected entry points, without tenant authentication and without
setting a tenant header, and SHALL NOT route that hostname to any signal
backend.

#### Scenario: UI request
- **WHEN** a browser requests the Grafana UI hostname over TLS
- **THEN** the request reaches Grafana, which performs its own login

#### Scenario: UI hostname cannot reach a backend
- **WHEN** a client requests a telemetry ingestion or query path using the
  Grafana UI hostname
- **THEN** the request is handled by Grafana and never forwarded to a signal
  backend

#### Scenario: Telemetry routes stay authenticated
- **WHEN** the gateway configuration is rendered with the Grafana UI route
- **THEN** every route to a signal backend still has the authentication step
  attached
