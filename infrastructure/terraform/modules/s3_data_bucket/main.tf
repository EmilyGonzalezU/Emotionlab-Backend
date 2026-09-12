variable "bucket_name" {
  type = string
}

variable "project" {
  type = string
}

variable "env" {
  type = string
}

resource "aws_s3_bucket" "data" {
  bucket = var.bucket_name

  tags = {
    Project = var.project
    Env     = var.env
    Purpose = "application-json-data"
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket = aws_s3_bucket.data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

output "bucket_name" {
  value = aws_s3_bucket.data.id
}

output "bucket_arn" {
  value = aws_s3_bucket.data.arn
}
