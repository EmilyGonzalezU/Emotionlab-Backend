variable "bucket_name" {
  description = "Nombre del bucket S3 (debe existir previamente en AWS Learner Lab)"
  type        = string
}

variable "env" {
  description = "Entorno (dev/prod)"
  type        = string
}

variable "project" {
  description = "Nombre del proyecto"
  type        = string
}

variable "versioning_enabled" {
  description = "Habilitar versionado de objetos (prod=true, dev=false recomendado). Se ignora en Learner Lab; gestionar via scripts/setup_s3_configs.py."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags adicionales (solo metadata; no aplicados via Terraform en Learner Lab)"
  type        = map(string)
  default     = {}
}

# ============================================================
# AWS LEARNER LAB SCP WORKAROUND
# ============================================================
# El SCP del Learner Lab aplica DENY EXPLÍCITO a s3:GetObjectLockConfiguration.
# El provider hashicorp/aws 5.x LLAMA AUTOMATICAMENTE a GetObjectLockConfiguration
# para TODOS los recursos hijos de configuración S3:
#   aws_s3_bucket / aws_s3_bucket_server_side_encryption_configuration /
#   aws_s3_bucket_policy / aws_s3_bucket_public_access_block /
#   aws_s3_bucket_ownership_controls / aws_s3_bucket_versioning
#
# Solución compatible con Learner Lab SIN modificar SCP:
#   · Terraform solo usa data "aws_s3_bucket" (SOLO LECTURA via HeadBucket).
#     HeadBucket NO lee Object Lock → no es bloqueado.
#   · Las 5 configuraciones de seguridad S3 (Encryption, PublicAccessBlock,
#     Policy HTTPS-only, OwnershipControls, Versioning) se aplican mediante el
#     script idempotente scripts/setup_s3_configs.py que ejecuta directamente:
#       PutBucketEncryption + PutPublicAccessBlock + PutBucketPolicy +
#       PutBucketOwnershipControls + PutBucketVersioning
#     Estas 5 APIs de escritura NO consultan Object Lock y pasan OK el SCP.
#
# REQUISITO PREVIO (ya debe estar ejecutado):
#   terraform state rm module.s3_report_pdfs.aws_s3_bucket.private
#   terraform state rm module.s3_sessions_raw.aws_s3_bucket.private
#   (solo modifica .tfstate local → NO destruye los buckets en AWS).
#
# VERIFICACIÓN:
#   python scripts/setup_s3_configs.py           # aplicar configuraciones
#   python scripts/setup_s3_configs.py --verify  # verificar aplicadas
# ============================================================
data "aws_s3_bucket" "private" {
  bucket = var.bucket_name
}

output "bucket_name" {
  description = "Nombre del bucket S3"
  value       = data.aws_s3_bucket.private.id
}

output "bucket_arn" {
  description = "ARN del bucket S3"
  value       = data.aws_s3_bucket.private.arn
}

output "bucket_domain_name" {
  description = "Dominio regional del bucket"
  value       = data.aws_s3_bucket.private.bucket_regional_domain_name
}

output "bucket_region" {
  description = "Region del bucket"
  value       = data.aws_s3_bucket.private.region
}
