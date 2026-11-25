terraform {
  backend "s3" {
    bucket       = "emotionlab-tfstate-dev-595859497127"
    key          = "dev/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
