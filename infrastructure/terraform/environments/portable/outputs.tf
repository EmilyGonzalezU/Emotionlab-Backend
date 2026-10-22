output "aws_region" {
  description = "Region AWS utilizada"
  value       = var.aws_region
}

output "api_endpoint" {
  description = "URL HTTPS base del API Gateway para configurar VITE_API_URL."
  value       = module.api_gateway.api_endpoint
}

output "model_ecr_repository_url" {
  description = "URI del repositorio ECR para publicar la imagen de inferencia."
  value       = module.ecr_model.repository_uri
}

output "jwt_secret_arn" {
  description = "ARN del secreto JWT generado por Terraform en Secrets Manager. El state debe mantenerse cifrado y con acceso restringido."
  value       = aws_secretsmanager_secret.jwt_secret.arn
}

output "frontend_url" {
  description = "URL CloudFront del sitio si enable_frontend_hosting=true."
  value       = var.enable_frontend_hosting ? module.frontend_hosting[0].url : null
}

output "frontend_bucket_name" {
  description = "Bucket de sitio privado si enable_frontend_hosting=true."
  value       = var.enable_frontend_hosting ? module.frontend_hosting[0].bucket_name : null
}

output "frontend_distribution_id" {
  description = "Distribución CloudFront si enable_frontend_hosting=true."
  value       = var.enable_frontend_hosting ? module.frontend_hosting[0].distribution_id : null
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

output "ses_identity_arn" {
  description = "ARN de identidad SES opcional; complete la verificación manual por correo."
  value       = var.enable_ses_identity ? module.ses_identity[0].ses_identity_arn : null
}
