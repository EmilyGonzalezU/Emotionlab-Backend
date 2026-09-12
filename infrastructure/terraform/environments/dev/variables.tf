variable "aws_region" {
  description = "Region AWS para desplegar los recursos"
  type        = string
  default     = "us-east-1"
  validation {
    condition     = contains(["us-east-1", "us-east-2", "sa-east-1", "eu-west-1"], var.aws_region)
    error_message = "Region AWS no valida."
  }
}

variable "email_sender_address" {
  description = "Correo remitente para SES (debe ser verificado; el usuario hara clic en el link de verificacion que llega a esta casilla)"
  type        = string
  default     = "em.gonzalezu@duocuc.cl"
}

variable "email_sender_name" {
  description = "Nombre visible del remitente de correos"
  type        = string
  default     = "EmotionLab"
}

variable "ses_source_arn" {
  description = "ARN de identidad SES verificado (opcional, se genera si no se entrega)"
  type        = string
  default     = ""
}
