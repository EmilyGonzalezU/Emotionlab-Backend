variable "bucket_name" {
  type = string
}

variable "project" {
  type = string
}

variable "env" {
  type = string
}

output "bucket_name" {
  value = var.bucket_name
}

output "bucket_arn" {
  value = "arn:aws:s3:::${var.bucket_name}"
}