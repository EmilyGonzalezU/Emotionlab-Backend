variable "ses_email_address" {
  description = "Direccion de correo electronico a verificar en SES"
  type        = string
}

variable "project" {
  description = "Nombre del proyecto"
  type        = string
}

variable "env" {
  description = "Entorno (dev/prod)"
  type        = string
}

resource "aws_ses_email_identity" "sender" {
  email = var.ses_email_address

  lifecycle {
    precondition {
      condition     = trimspace(var.ses_email_address) != ""
      error_message = "Configure una dirección SES antes de habilitar la identidad."
    }
  }
}

output "ses_identity_arn" {
  description = "ARN de la identidad de correo SES"
  value       = aws_ses_email_identity.sender.arn
}

output "ses_verified_email" {
  description = "Correo verificado"
  value       = aws_ses_email_identity.sender.email
}
