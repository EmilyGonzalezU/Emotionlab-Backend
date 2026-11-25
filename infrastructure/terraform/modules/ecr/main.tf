variable "repository_name" {
  type = string
}

variable "project" {
  type = string
}

variable "env" {
  type = string
}

resource "aws_ecr_repository" "this" {
  name                 = var.repository_name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Project     = var.project
    Environment = var.env
    ManagedBy   = "Terraform"
  }
}

output "repository_name" {
  value = aws_ecr_repository.this.name
}

output "repository_uri" {
  value = aws_ecr_repository.this.repository_url
}

output "repository_arn" {
  value = aws_ecr_repository.this.arn
}
