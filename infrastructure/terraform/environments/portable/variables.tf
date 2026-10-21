variable "aws_region" {
  description = "Region AWS para desplegar los recursos"
  type        = string

  default = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "aws_region debe tener formato de región AWS, por ejemplo us-east-1."
  }
}

variable "environment" {
  description = "Entorno y parte del prefijo de los recursos."
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Prefijo lógico de los recursos EmotionLab."
  type        = string
  default     = "emotionlab"
}

variable "create_lambda_role" {
  description = "Crea un rol IAM dedicado para las funciones Lambda EmotionLab."
  type        = bool
  default     = true
}

variable "existing_lambda_role_arn" {
  description = "ARN de rol de ejecución existente. Solo se usa si create_lambda_role=false."
  type        = string
  default     = ""
}

variable "data_bucket_name" {
  description = "Nombre opcional del bucket S3 de datos. Si se deja vacío, se genera uno único por cuenta."
  type        = string
  default     = ""
}

variable "report_pdfs_bucket_name" {
  description = "Nombre opcional del bucket S3 para PDFs. Si se deja vacío, se genera uno único por cuenta."
  type        = string
  default     = ""
}

variable "sessions_raw_bucket_name" {
  description = "Nombre opcional del bucket S3 para sesiones raw. Si se deja vacío, se genera uno único por cuenta."
  type        = string
  default     = ""
}

variable "model_image_uri" {
  description = "URI de la imagen ECR del modelo. Si se deja vacía, el Lambda del modelo no se crea."
  type        = string
  default     = ""
}
variable "jwt_secret" {
  description = "Obsoleto; se conserva para aceptar terraform.tfvars locales anteriores y no se usa. JWT_SECRET se lee desde Secrets Manager."
  type        = string
  sensitive   = true
  default     = ""
}

variable "unity_data_url" {
  description = "URL externa que utiliza Sync para obtener los datos provenientes de los proyectos Unity."
  type        = string
  default     = ""
}

variable "cors_allowed_origins" {
  description = "Orígenes frontend permitidos separados por coma; incluya el origen del sitio y null para Electron si corresponde."
  type        = string
  default     = "http://localhost:5173,http://127.0.0.1:5173,null"
}

variable "s3_versioning_enabled" {
  description = "Activa versionado en buckets de datos EmotionLab."
  type        = bool
  default     = true
}

variable "s3_kms_key_arn" {
  description = "ARN opcional de una CMK para cifrado SSE-KMS en los buckets. Vacío utiliza SSE-S3."
  type        = string
  default     = ""
}

variable "dynamodb_pitr_enabled" {
  description = "Activa Point-in-Time Recovery para las tablas del entorno."
  type        = bool
  default     = true
}

variable "backend_log_retention_days" {
  description = "Retención de logs backend y funciones Lambda."
  type        = number
  default     = 90
}

variable "migration_log_retention_days" {
  description = "Retención de logs de importación/migración."
  type        = number
  default     = 180
}

variable "enable_frontend_hosting" {
  description = "Crea un bucket privado y distribución CloudFront opcional para el frontend."
  type        = bool
  default     = false
}

variable "frontend_bucket_name" {
  description = "Nombre opcional y globalmente único para el bucket del sitio."
  type        = string
  default     = ""
}

variable "frontend_domain_name" {
  description = "Dominio opcional del sitio; requiere un certificado ACM emitido en us-east-1."
  type        = string
  default     = ""
}

variable "frontend_acm_certificate_arn" {
  description = "ARN opcional de certificado ACM de us-east-1 para el dominio frontend."
  type        = string
  default     = ""
  validation {
    condition     = var.frontend_acm_certificate_arn == "" || can(regex("^arn:aws:acm:us-east-1:[0-9]{12}:certificate/.+", var.frontend_acm_certificate_arn))
    error_message = "El certificado CloudFront debe ser un ARN ACM de us-east-1."
  }
}

variable "email_sender_address" {
  description = "Correo remitente utilizado por EmotionLab."
  type        = string
  default     = ""
}

variable "email_sender_name" {
  description = "Nombre visible del remitente."
  type        = string
  default     = "EmotionLab"
}

variable "ses_source_arn" {
  description = "ARN de identidad SES verificada. Opcional."
  type        = string
  default     = ""
}

variable "enable_ses_identity" {
  description = "Crea una identidad de correo SES opcional. Debe verificarse desde el correo recibido."
  type        = bool
  default     = false
}

variable "model_ecr_repository_name" {
  type    = string
  default = "emotionlab-model"
}
