# Spec Delta

## MODIFIED Requirements

### Requirement: Collection-time redaction
A rendered collector SHALL remove every field named in its datastream's
drop-field list from metric labels, log labels and structured metadata, OTLP
span, log, and metric attributes, OTLP resource attributes, and profile
labels, matching field names case-insensitively, before delivery. In
pipelines whose label names cannot contain `.` or `-`, the collector SHALL
also remove the label whose name is the drop field with those characters
replaced by `_`. The documentation SHALL state which content is not
sanitized, including free-text log bodies beyond the documented key/value
patterns and profile payloads.

#### Scenario: Drop-field coverage in every enabled pipeline
- **WHEN** a collector is rendered for a datastream with drop fields
  `password` and `email` and all four signals enabled
- **THEN** each of the metrics, logs, traces, and profiles pipelines contains
  a removal rule for `password` and for `email`

#### Scenario: Redaction precedes delivery
- **WHEN** any profile is rendered
- **THEN** no path from a receiver or discovery component to a gateway
  exporter bypasses that signal's redaction stage

#### Scenario: Drop field with a separator in a label pipeline
- **WHEN** a collector is rendered for a datastream with drop field
  `user.email`
- **THEN** the metric, log, and profile label rules remove both `user.email`
  and `user_email`, and the attribute rules remove `user.email`
