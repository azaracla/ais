variable "ovh_application_key" {
  type = string
}

variable "ovh_application_secret" {
  type      = string
  sensitive = true
}

variable "ovh_consumer_key" {
  type      = string
  sensitive = true
}

variable "ovh_service_name" {
  type        = string
  description = "OVH Project ID (service_name)"
}

variable "region" {
  type        = string
  description = "Région OpenStack pour le compute/networking (ex: GRA9, GRA11)"
  default     = "GRA9"
}

variable "s3_region_name" {
  type        = string
  description = "Région OVH S3 Object Storage (ex: GRA)"
  default     = "GRA"
}

variable "environment" {
  type        = string
  description = "Deployment environment (e.g., prod, dev)"
  default     = "prod"
}
