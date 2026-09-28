variable "aws_region" {
  description = "Region AWS para desplegar los recursos"
  type        = string

  default = "us-east-1"

  validation {
    condition = contains(
      ["us-east-1", "us-east-2", "sa-east-1", "eu-west-1"],
      var.aws_region
    )

    error_message = "Region AWS no valida."
  }
}

variable "lambda_role_name" {
  description = "Nombre del rol IAM existente que utilizaran las Lambdas."
  type        = string
  default     = "LabRole"
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
  description = "Secreto JWT usado por el backend. No subir terraform.tfvars a Git."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.jwt_secret)) >= 32
    error_message = "jwt_secret debe tener al menos 32 caracteres."
  }
}

variable "unity_data_url" {
  description = "URL externa que utiliza Sync para obtener los datos provenientes de los proyectos Unity."
  type        = string

  validation {
    condition     = trimspace(var.unity_data_url) != ""
    error_message = "unity_data_url debe contener la URL del servicio de datos de Unity."
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

variable "model_ecr_repository_name" {
  type    = string
  default = "emotionlab-model"
}