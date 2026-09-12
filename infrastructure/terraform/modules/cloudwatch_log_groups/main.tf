variable "env" {
  description = "Entorno (dev/prod)"
  type        = string
}

variable "project" {
  description = "Nombre del proyecto"
  type        = string
}

variable "log_groups" {
  description = "Lista de grupos de logs a crear"
  type = list(object({
    name              = string
    retention_in_days = optional(number, 90)
  }))
  default = []
}

resource "aws_cloudwatch_log_group" "groups" {
  for_each          = { for lg in var.log_groups : lg.name => lg }
  name              = each.key
  retention_in_days = each.value.retention_in_days
  skip_destroy      = false
  tags = {
    Project = var.project
    Env     = var.env
  }
}

output "log_group_names" {
  description = "Nombres de los grupos de logs creados"
  value       = [for lg in var.log_groups : lg.name]
}
