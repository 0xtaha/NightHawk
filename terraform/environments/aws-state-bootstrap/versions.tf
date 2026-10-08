terraform {
  required_version = "= 1.13.5"

  # Deliberately no backend block: this root creates the remote backend
  # every other AWS root uses, so it must use its own local state. See
  # README.md's "Local state is deliberate here" section.

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.12.0"
    }
  }
}
