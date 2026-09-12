variable "env" {
  description = "Entorno (dev/prod)"
  type        = string
}

variable "project" {
  description = "Nombre del proyecto"
  type        = string
}

variable "params" {
  description = "Mapa { path = value } de parametros a guardar en Parameter Store"
  type        = map(string)
  default     = {}
}

variable "secure_params" {
  description = "Mapa { path = value } de parametros cifrados (SecureString)"
  type        = map(string)
  default     = {}
}

resource "aws_ssm_parameter" "string" {
  for_each = var.params

  name      = each.key
  type      = "String"
  value     = each.value
  overwrite = true
  tags = {
    Project = var.project
    Env     = var.env
  }
}

resource "aws_ssm_parameter" "secure" {
  for_each = var.secure_params

  name      = each.key
  type      = "SecureString"
  value     = each.value
  overwrite = true
  key_id    = "alias/aws/ssm"
  tags = {
    Project = var.project
    Env     = var.env
  }
}
