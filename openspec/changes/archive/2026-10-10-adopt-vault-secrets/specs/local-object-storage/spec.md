# Spec Delta

## MODIFIED Requirements

### Requirement: Storage identity generation
The system SHALL provide a command that generates the access key and secret
key for a storage identity declared in the platform document and stores them
only in Vault under that identity's secret reference, refusing to replace an
existing value.

#### Scenario: Generating a declared identity
- **WHEN** an operator generates the keys for a declared storage identity
- **THEN** a new access key and secret key are stored in Vault at its
  reference and neither is printed

#### Scenario: Undeclared identity
- **WHEN** an operator requests keys for a secret that is not a storage
  identity of the platform document
- **THEN** the system fails explicitly and writes nothing
