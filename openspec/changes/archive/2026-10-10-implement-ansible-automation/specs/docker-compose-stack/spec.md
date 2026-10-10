# Spec Delta

## ADDED Requirements

### Requirement: External entry point override
The Compose configuration SHALL offer an override that publishes the
gateway's certificate-requiring external entry point on an address and port
the operator supplies, and SHALL publish that entry point in no other way.
The override SHALL NOT change where the loopback entry point is published.

#### Scenario: Without the override
- **WHEN** the stack is started without the override
- **THEN** only the loopback entry point is published

#### Scenario: With the override
- **WHEN** the stack is started with the override and an address
- **THEN** the external entry point is published on that address, and the
  loopback entry point is still published on loopback only

#### Scenario: No address supplied
- **WHEN** the override is used without an address
- **THEN** the configuration fails to resolve

#### Scenario: Ingestion on the external entry point
- **WHEN** a collector sends telemetry to the published external entry point
  with a credential but without a client certificate
- **THEN** the request is refused, and accepted when the credential's
  certificate is presented
