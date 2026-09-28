variable "api_name" {
  description = "Nombre del HTTP API"
  type        = string
}

variable "stage_name" {
  description = "Nombre del stage"
  type        = string
  default     = "$default"
}

variable "backend_lambda_name" {
  description = "Nombre de EmotionLabBackendFunction"
  type        = string
}

variable "model_lambda_name" {
  description = "Nombre de EmotionLabModelFunction"
  type        = string
}

variable "test_lambda_name" {
  description = "Nombre de EmotionLabTestFunction"
  type        = string
}

variable "model_enabled" {
  description = "Indica si el Lambda del modelo está disponible"
  type        = bool
  default     = false
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  backend_lambda_arn = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.backend_lambda_name}"

  model_lambda_arn = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.model_lambda_name}"

  test_lambda_arn = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.test_lambda_name}"
}

resource "aws_apigatewayv2_api" "this" {
  name          = var.api_name
  protocol_type = "HTTP"
}

# ============================================================
# BACKEND
# ============================================================

resource "aws_apigatewayv2_integration" "backend" {
  api_id = aws_apigatewayv2_api.this.id

  integration_type       = "AWS_PROXY"
  integration_uri        = local.backend_lambda_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000

  depends_on = [
    aws_lambda_permission.backend
  ]
}

resource "aws_apigatewayv2_route" "backend" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.backend.id}"
}

resource "aws_lambda_permission" "backend" {
  statement_id  = "AllowExecutionFromApiGatewayBackend"
  action        = "lambda:InvokeFunction"
  function_name = var.backend_lambda_name
  principal     = "apigateway.amazonaws.com"

  source_arn = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# ============================================================
# MODEL
# ============================================================
# El modelo es opcional porque actualmente no tenemos la
# imagen Docker disponible en ECR.
#
# Cuando model_enabled = false:
# - No se crea la integración /predict
# - No se crea la ruta POST /predict
# - No se crea el permiso para el Lambda del modelo
#
# Cuando model_enabled = true:
# - Se crean los tres recursos anteriores.
# ============================================================

resource "aws_apigatewayv2_integration" "model" {
  count = var.model_enabled ? 1 : 0

  api_id = aws_apigatewayv2_api.this.id

  integration_type       = "AWS_PROXY"
  integration_uri        = local.model_lambda_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000

  depends_on = [
    aws_lambda_permission.model
  ]
}

resource "aws_apigatewayv2_route" "model" {
  count = var.model_enabled ? 1 : 0

  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /predict"
  target    = "integrations/${aws_apigatewayv2_integration.model[0].id}"
}

resource "aws_lambda_permission" "model" {
  count = var.model_enabled ? 1 : 0

  statement_id  = "AllowExecutionFromApiGatewayModel"
  action        = "lambda:InvokeFunction"
  function_name = var.model_lambda_name
  principal     = "apigateway.amazonaws.com"

  source_arn = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# ============================================================
# TEST
# ============================================================

resource "aws_apigatewayv2_integration" "test" {
  api_id = aws_apigatewayv2_api.this.id

  integration_type       = "AWS_PROXY"
  integration_uri        = local.test_lambda_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000

  depends_on = [
    aws_lambda_permission.test
  ]
}

resource "aws_apigatewayv2_route" "test" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /test"
  target    = "integrations/${aws_apigatewayv2_integration.test.id}"
}

resource "aws_lambda_permission" "test" {
  statement_id  = "AllowExecutionFromApiGatewayTest"
  action        = "lambda:InvokeFunction"
  function_name = var.test_lambda_name
  principal     = "apigateway.amazonaws.com"

  source_arn = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# ============================================================
# STAGE
# ============================================================

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = var.stage_name
  auto_deploy = true
}

# ============================================================
# OUTPUTS
# ============================================================

output "api_id" {
  value = aws_apigatewayv2_api.this.id
}

output "api_endpoint" {
  value = aws_apigatewayv2_api.this.api_endpoint
}