output "aws_region" {
  description = "Region AWS utilizada"
  value       = var.aws_region
}

output "cognito_user_pool_id" {
  description = "ID del User Pool de Cognito"
  value       = module.cognito.user_pool_id
}

output "cognito_app_client_id" {
  description = "ID del App Client del backend Flask en Cognito"
  value       = module.cognito.app_client_id
}

output "cognito_user_pool_arn" {
  description = "ARN del User Pool de Cognito"
  value       = module.cognito.user_pool_arn
}

output "dynamodb_users_table" {
  description = "Nombre tabla DynamoDB de usuarios"
  value       = module.dynamodb_users.table_name
  sensitive   = false
}

output "dynamodb_participants_table" {
  description = "Nombre tabla DynamoDB de participantes"
  value       = module.dynamodb_participants.table_name
}

output "dynamodb_sessions_table" {
  description = "Nombre tabla DynamoDB de sesiones"
  value       = module.dynamodb_sessions.table_name
}

output "dynamodb_audit_logs_table" {
  description = "Nombre tabla DynamoDB de auditoria"
  value       = module.dynamodb_audit_logs.table_name
}

output "dynamodb_report_requests_table" {
  description = "Nombre tabla DynamoDB de solicitudes de informes"
  value       = module.dynamodb_report_requests.table_name
}

output "s3_report_pdfs_bucket" {
  description = "Nombre bucket S3 privado para PDFs de informes"
  value       = module.s3_report_pdfs.bucket_name
}

output "s3_sessions_raw_bucket" {
  description = "Nombre bucket S3 privado para JSON raw de sesiones"
  value       = module.s3_sessions_raw.bucket_name
}

output "ses_verified_email" {
  description = "Correo verificado en SES"
  value       = var.email_sender_address
}
