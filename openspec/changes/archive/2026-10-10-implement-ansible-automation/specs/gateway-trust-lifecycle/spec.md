# Spec Delta

## ADDED Requirements

### Requirement: Issuing only when needed
The system SHALL offer a mode of certificate issuance that requests a new
certificate only when none exists at the output location, when the existing
one expires within an operator-supplied period, or when it was not signed by
the authority currently in Vault, and that otherwise changes nothing and
says so.

#### Scenario: Nothing to do
- **WHEN** a valid certificate from the current authority with more than the
  supplied period left exists
- **THEN** no request is sent to Vault and both files are unchanged

#### Scenario: Close to expiry
- **WHEN** the existing certificate expires within the supplied period
- **THEN** a new certificate and key replace the old ones, with the same
  names and identity

#### Scenario: Authority replaced
- **WHEN** the existing certificate was signed by an authority Vault no
  longer has
- **THEN** a new certificate is issued

#### Scenario: Replacement fails
- **WHEN** a needed replacement cannot be issued
- **THEN** the existing certificate and key are left in place and the
  command fails
