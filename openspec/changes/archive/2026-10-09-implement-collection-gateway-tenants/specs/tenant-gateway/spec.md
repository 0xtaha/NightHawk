# Spec Delta

## Purpose

Provides the single authenticated TLS entry point for telemetry ingestion and
queries, binding each credential and collector certificate identity to exactly
one backend tenant so clients can never choose the tenant they write to or
read from.

## ADDED Requirements

### Requirement: Credential-derived tenant binding
The gateway SHALL determine the backend tenant ID of every forwarded request
solely from the authenticated credential's tenant/datastream binding in the
platform document, and SHALL set that ID as the tenant header on the request
it forwards.

#### Scenario: Request without a tenant header
- **WHEN** a client presents a valid credential and no `X-Scope-OrgID` header
- **THEN** the gateway forwards the request with `X-Scope-OrgID` set to the
  backend ID bound to that credential

#### Scenario: Matching tenant header
- **WHEN** a client presents a valid credential and an `X-Scope-OrgID` header
  equal to the backend ID bound to that credential
- **THEN** the gateway forwards the request with that same backend ID

### Requirement: Spoofed and conflicting tenant headers are rejected
The gateway SHALL reject, without forwarding, any request whose client-supplied
tenant header differs from the authenticated credential's backend ID, appears
more than once, or names multiple tenants.

#### Scenario: Header naming another tenant
- **WHEN** a client presents a valid credential for one backend ID and an
  `X-Scope-OrgID` header naming a different backend ID
- **THEN** the gateway responds with an authorization failure and forwards
  nothing

#### Scenario: Multi-tenant federation syntax
- **WHEN** a client presents a valid credential and an `X-Scope-OrgID` header
  containing more than one tenant ID
- **THEN** the gateway responds with an authorization failure and forwards
  nothing

### Requirement: Unauthenticated requests are rejected
The gateway SHALL reject any request with a missing, malformed, or unknown
credential with an authentication failure, and SHALL NOT reveal whether the
credential identifier exists.

#### Scenario: Missing credential
- **WHEN** a request arrives with no credential
- **THEN** the gateway responds with an authentication failure and forwards
  nothing

#### Scenario: Unknown identifier and wrong secret are indistinguishable
- **WHEN** one request uses an unknown credential identifier and another uses
  a known identifier with a wrong secret
- **THEN** both receive the same authentication failure response

### Requirement: Ingest and query permissions are separate
The gateway SHALL allow an ingestion credential to reach only ingestion routes
and a query credential to reach only query routes.

#### Scenario: Ingestion credential used to query
- **WHEN** a client presents a valid ingestion credential on a query route
- **THEN** the gateway responds with an authorization failure and forwards
  nothing

#### Scenario: Query credential used to ingest
- **WHEN** a client presents a valid query credential on an ingestion route
- **THEN** the gateway responds with an authorization failure and forwards
  nothing

### Requirement: Only enabled signals are reachable
The gateway SHALL reject a request for a signal that the credential's
datastream does not enable.

#### Scenario: Disabled signal
- **WHEN** a client presents a valid ingestion credential for a datastream
  that does not enable profiles on a profile ingestion route
- **THEN** the gateway responds with an authorization failure and forwards
  nothing

### Requirement: Certificate identity binding for remote collectors
For a credential that declares a certificate identity, the gateway SHALL
require a verified client certificate whose URI identity equals that declared
identity. On the restricted-external entry point, the gateway SHALL reject
ingestion with a credential that declares no certificate identity. The gateway
SHALL reject any certificate whose fingerprint is listed as revoked.

#### Scenario: Matching certificate
- **WHEN** a remote collector presents a valid ingestion credential and a
  verified client certificate carrying that credential's declared identity
- **THEN** the gateway forwards the request for the bound backend ID

#### Scenario: Certificate for another datastream
- **WHEN** a client presents a valid ingestion credential together with a
  verified client certificate carrying a different identity
- **THEN** the gateway responds with an authorization failure and forwards
  nothing

#### Scenario: Credential without its certificate
- **WHEN** a client presents a credential that declares a certificate identity
  and no client certificate
- **THEN** the gateway responds with an authorization failure and forwards
  nothing

#### Scenario: Revoked certificate
- **WHEN** a client presents a valid credential and a verified certificate
  whose fingerprint is in the platform document's revoked list
- **THEN** the gateway responds with an authorization failure and forwards
  nothing

#### Scenario: Client-supplied certificate header
- **WHEN** a client that performed no TLS client authentication sends a
  request header imitating the gateway's forwarded-certificate header
- **THEN** the gateway does not treat that header as a certificate identity

### Requirement: HTTP and gRPC transports
The gateway SHALL apply the same authentication and tenant-binding policy to
HTTP and gRPC requests. It SHALL accept OTLP over HTTP for metrics, logs, and
traces, OTLP over gRPC for traces, the native push protocol of each backend,
and each backend's query API. The documentation SHALL list every accepted
route with its signal, permission, and transport.

#### Scenario: OTLP over gRPC
- **WHEN** a collector sends an authenticated OTLP gRPC trace export
- **THEN** the gateway forwards it to the trace backend with the bound backend
  ID

#### Scenario: OTLP over HTTP
- **WHEN** a collector sends an authenticated OTLP HTTP trace export
- **THEN** the gateway forwards it to the trace backend with the bound backend
  ID

#### Scenario: Unrouted path
- **WHEN** an authenticated client requests a path that is not a declared
  ingestion or query route, including a backend administrative endpoint
- **THEN** the gateway does not forward the request

### Requirement: TLS only, private backends
The gateway SHALL accept only TLS connections on the ports declared for it in
the network contract, and its rendered configuration SHALL publish no route to
a backend that bypasses authentication.

#### Scenario: Every route is authenticated
- **WHEN** the gateway configuration is rendered for a valid platform document
- **THEN** every route to a signal backend has the authentication step
  attached, and every entry point is TLS-only

#### Scenario: Ports match the network contract
- **WHEN** the gateway configuration is rendered
- **THEN** its listening ports are exactly the ports of the network-contract
  gateway rules that the platform document selects as entry points

#### Scenario: Unknown entry point
- **WHEN** a platform document selects an entry point that is not a
  network-contract rule whose destination is the gateway
- **THEN** rendering fails and names the entry point

### Requirement: Fail-closed policy loading
The auth service SHALL refuse to start without a valid policy, SHALL deny
every request it cannot evaluate, and on a reload SHALL keep enforcing the
last valid policy and report the failure when the new policy is invalid.

#### Scenario: Missing or invalid policy at start
- **WHEN** the auth service starts with a missing or invalid policy
- **THEN** it exits with an error instead of serving

#### Scenario: Invalid policy on reload
- **WHEN** a running auth service is given an invalid replacement policy
- **THEN** it continues to enforce the previous policy and reports the reload
  failure through its health and metrics output

#### Scenario: Removed credential after reload
- **WHEN** a credential is removed from the platform document and the
  resulting policy is loaded
- **THEN** requests using that credential receive an authentication failure

### Requirement: Policy contains no recoverable secrets
The rendered gateway policy SHALL contain digests of credential secrets, never
the secrets themselves, and rendering it SHALL be a separate, explicit step
from ordinary contract validation and rendering.

#### Scenario: Policy bundle content
- **WHEN** the gateway policy is rendered from materialized secrets
- **THEN** the bundle contains no plaintext credential value and is written
  with owner-only permissions

#### Scenario: Ordinary rendering needs no secrets
- **WHEN** an operator runs `validate` or `render-contracts` with no secret
  files present
- **THEN** the command succeeds and emits no secret-derived material
