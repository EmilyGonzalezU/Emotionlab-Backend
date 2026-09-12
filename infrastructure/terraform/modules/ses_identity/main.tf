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
}

output "ses_identity_arn" {
  description = "ARN de la identidad de correo SES"
  value       = aws_ses_email_identity.sender.arn
}

output "ses_verified_email" {
  description = "Correo verificado"
  value       = aws_ses_email_identity.sender.email
}
