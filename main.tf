terraform {
  required_providers {
    ovh = {
      source = "ovh/ovh"
    }
  }
}

provider "ovh" {
  endpoint           = "ovh-eu"
  application_key    = var.ovh_application_key
  application_secret = var.ovh_application_secret
  consumer_key       = var.ovh_consumer_key
}

# 1. Utilisateur technique de troubleshooting
resource "ovh_cloud_project_user" "ais_troubleshoot_user" {
  service_name = var.ovh_service_name
  description  = "Troubleshoot S3 user"
}

resource "ovh_cloud_project_user_s3_credential" "ais_troubleshoot_keys" {
  service_name = var.ovh_service_name
  user_id      = ovh_cloud_project_user.ais_troubleshoot_user.id
}

# 2. Bucket S3 pour les données brutes (raw) - "Normal" (Standard)
resource "ovh_cloud_project_storage" "ais_raw" {
  service_name = var.ovh_service_name
  name         = "ais-raw-${var.environment}"
  region_name  = var.s3_region_name
}

# 3. Bucket S3 pour le DuckLake consolidé - Public
resource "ovh_cloud_project_storage" "ais_public" {
  service_name = var.ovh_service_name
  name         = "ais-public-${var.environment}"
  region_name  = var.s3_region_name
}

# 5. Politique d'accès S3 pour les buckets ais-raw et ais-public
resource "ovh_cloud_project_user_s3_policy" "system_s3_policy" {
  service_name = var.ovh_service_name
  user_id      = ovh_cloud_project_user.ais_troubleshoot_user.id
  policy       = jsonencode({
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:*"]
        Resource = [
          "arn:aws:s3:::${ovh_cloud_project_storage.ais_raw.name}",
          "arn:aws:s3:::${ovh_cloud_project_storage.ais_raw.name}/*",
          "arn:aws:s3:::${ovh_cloud_project_storage.ais_public.name}",
          "arn:aws:s3:::${ovh_cloud_project_storage.ais_public.name}/*"
        ]
      }
    ]
  })
}

# --- OUTPUTS ---

output "s3_access_key" {
  value     = ovh_cloud_project_user_s3_credential.ais_troubleshoot_keys.access_key_id
  sensitive = true
}

output "s3_secret_key" {
  value     = ovh_cloud_project_user_s3_credential.ais_troubleshoot_keys.secret_access_key
  sensitive = true
}

output "ais_raw_bucket_name" {
  value = ovh_cloud_project_storage.ais_raw.name
}

output "ais_public_bucket_name" {
  value = ovh_cloud_project_storage.ais_public.name
}
