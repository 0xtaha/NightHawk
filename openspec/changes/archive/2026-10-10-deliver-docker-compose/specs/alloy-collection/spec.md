# Spec Delta

## ADDED Requirements

### Requirement: OTLP-only collector
The collector render SHALL support an option that omits a profile's host
sources and produces a collector that only receives pushed telemetry and
scrapes itself, with the same redaction and delivery as any other collector.

#### Scenario: Rendering without host sources
- **WHEN** a collector is rendered with the OTLP-only option
- **THEN** the output contains no host discovery or host scraping component,
  needs no runtime socket or host mount, and still contains the redaction and
  delivery pipelines for every enabled signal

#### Scenario: Pushed profiles are still accepted
- **WHEN** an OTLP-only collector is rendered for a datastream that enables
  profiles
- **THEN** it accepts pushed profiles and forwards them through redaction
