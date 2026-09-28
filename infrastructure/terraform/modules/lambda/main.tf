variable "function_name" {
  type = string
}

variable "role_arn" {
  type = string
}

variable "package_type" {
  type = string

  validation {
    condition     = contains(["Zip", "Image"], var.package_type)
    error_message = "package_type debe ser Zip o Image."
  }
}

variable "runtime" {
  type    = string
  default = null
}

variable "handler" {
  type    = string
  default = null
}

variable "timeout" {
  type = number
}

variable "memory_size" {
  type = number
}

variable "architectures" {
  type    = list(string)
  default = ["x86_64"]
}

variable "image_uri" {
  type    = string
  default = null
}

variable "filename" {
  description = "Ruta al archivo ZIP de la Lambda."
  type        = string
  default     = null
}

variable "environment_variables" {
  type    = map(string)
  default = {}
}

resource "aws_lambda_function" "this" {
  function_name = var.function_name
  role          = var.role_arn

  package_type = var.package_type

  runtime = (
    var.package_type == "Zip"
    ? var.runtime
    : null
  )

  handler = (
    var.package_type == "Zip"
    ? var.handler
    : null
  )

  filename = (
    var.package_type == "Zip"
    ? var.filename
    : null
  )

  image_uri = (
    var.package_type == "Image"
    ? var.image_uri
    : null
  )

  timeout       = var.timeout
  memory_size   = var.memory_size
  architectures = var.architectures

  dynamic "environment" {
    for_each = length(var.environment_variables) > 0 ? [1] : []

    content {
      variables = var.environment_variables
    }
  }
}

output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "function_arn" {
  value = aws_lambda_function.this.arn
}