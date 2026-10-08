variable "region" {
  description = "AWS region hosting the remote-state bucket and lock table."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.region))
    error_message = "Specify an AWS region such as eu-west-1."
  }
}

variable "account_id" {
  description = "Expected AWS account; guards against bootstrapping the wrong account."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must contain exactly 12 digits."
  }
}

variable "name_prefix" {
  description = "Globally unique prefix for the state bucket and lock table names."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase letters/digits/hyphens, start with a letter, and end with a letter or digit."
  }
}

variable "tags" {
  description = "Additional tags on the bootstrap resources."
  type        = map(string)
  default     = {}
  nullable    = false
}
