terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  env         = "dev"
  project     = "emotionlab"
  name_prefix = "${local.project}-${local.env}"

  account_id  = data.aws_caller_identity.current.account_id
  lambda_role = "arn:aws:iam::${local.account_id}:role/${var.lambda_role_name}"

  backend_lambda_name = "EmotionLabBackendFunction"
  model_lambda_name   = "EmotionLabModelFunction"
  sync_lambda_name    = "EmotionLabSyncFunction"
  test_lambda_name    = "EmotionLabTestFunction"
}
module "ecr_model" {
  source = "../../modules/ecr"

  repository_name = var.model_ecr_repository_name
  project         = local.project
  env             = local.env
}
# ============================================================
# COGNITO
# ============================================================

module "cognito" {
  source = "../../modules/cognito"

  name_prefix          = local.name_prefix
  env                  = local.env
  project              = local.project
  email_sender_address = var.email_sender_address
  email_sender_name    = var.email_sender_name
  ses_source_arn       = var.ses_source_arn
}

# ============================================================
# S3 DATA
# ============================================================

module "s3_data" {
  source = "../../modules/s3_data_bucket"

  bucket_name = var.data_bucket_name != "" ? var.data_bucket_name : "${local.name_prefix}-data-${local.account_id}"
  project     = local.project
  env         = local.env
}

# ============================================================
# DYNAMODB USERS
# ============================================================

module "dynamodb_users" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-users"
  partition_key_name = "uid"
  partition_key_type = "S"
  sort_key_name      = null
  sort_key_type      = null
  billing_mode       = "PAY_PER_REQUEST"

  attributes = [
    {
      name = "email"
      type = "S"
    },
    {
      name = "rol"
      type = "S"
    },
    {
      name = "created_at"
      type = "S"
    }
  ]

  global_secondary_indexes = [
    {
      name            = "gsi-users-email"
      hash_key        = "email"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name               = "gsi-users-rol"
      hash_key           = "rol"
      range_key          = "created_at"
      projection_type    = "INCLUDE"
      non_key_attributes = ["uid", "nombre", "email", "activo"]
      read_capacity      = null
      write_capacity     = null
    }
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

# ============================================================
# DYNAMODB PARTICIPANTS
# ============================================================

module "dynamodb_participants" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-participants"
  partition_key_name = "participant_id"
  partition_key_type = "S"
  sort_key_name      = null
  sort_key_type      = null
  billing_mode       = "PAY_PER_REQUEST"

  attributes = [
    {
      name = "np_1"
      type = "S"
    },
    {
      name = "np_2"
      type = "S"
    },
    {
      name = "np_3"
      type = "S"
    },
    {
      name = "np_4"
      type = "S"
    },
    {
      name = "nombre_normalizado"
      type = "S"
    }
  ]

  global_secondary_indexes = [
    {
      name               = "gsi-pnp-1"
      hash_key           = "np_1"
      range_key          = "nombre_normalizado"
      projection_type    = "INCLUDE"
      non_key_attributes = ["participant_id", "nombre"]
      read_capacity      = null
      write_capacity     = null
    },
    {
      name               = "gsi-pnp-2"
      hash_key           = "np_2"
      range_key          = "nombre_normalizado"
      projection_type    = "INCLUDE"
      non_key_attributes = ["participant_id", "nombre"]
      read_capacity      = null
      write_capacity     = null
    },
    {
      name               = "gsi-pnp-3"
      hash_key           = "np_3"
      range_key          = "nombre_normalizado"
      projection_type    = "INCLUDE"
      non_key_attributes = ["participant_id", "nombre"]
      read_capacity      = null
      write_capacity     = null
    },
    {
      name               = "gsi-pnp-4"
      hash_key           = "np_4"
      range_key          = "nombre_normalizado"
      projection_type    = "INCLUDE"
      non_key_attributes = ["participant_id", "nombre"]
      read_capacity      = null
      write_capacity     = null
    }
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

# ============================================================
# DYNAMODB SESSIONS
# ============================================================

module "dynamodb_sessions" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-sessions"
  partition_key_name = "participant_id"
  partition_key_type = "S"
  sort_key_name      = "sk"
  sort_key_type      = "S"
  billing_mode       = "PAY_PER_REQUEST"

  attributes = [
    {
      name = "session_id"
      type = "S"
    },
    {
      name = "status"
      type = "S"
    },
    {
      name = "fecha"
      type = "S"
    },
    {
      name = "source_key"
      type = "S"
    },
    {
      name = "created_at"
      type = "S"
    }
  ]

  global_secondary_indexes = [
    {
      name            = "gsi-sessions-status-date"
      hash_key        = "status"
      range_key       = "created_at"
      projection_type = "INCLUDE"

      non_key_attributes = [
        "session_id",
        "participant_id",
        "nombre",
        "pred_bienestar",
        "fecha",
        "hora",
        "edad",
        "genero",
        "difficulty"
      ]

      read_capacity  = null
      write_capacity = null
    },
    {
      name            = "gsi-sessions-sessionid"
      hash_key        = "session_id"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name            = "gsi-sessions-sourcekey"
      hash_key        = "source_key"
      projection_type = "INCLUDE"

      non_key_attributes = [
        "participant_id",
        "sk",
        "session_id"
      ]

      read_capacity  = null
      write_capacity = null
    },
    {
      name            = "gsi-sessions-fecha"
      hash_key        = "fecha"
      range_key       = "session_id"
      projection_type = "INCLUDE"

      non_key_attributes = [
        "status",
        "pred_bienestar",
        "nombre",
        "participant_id"
      ]

      read_capacity  = null
      write_capacity = null
    }
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

# ============================================================
# DYNAMODB AUDIT LOGS
# ============================================================

module "dynamodb_audit_logs" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-audit-logs"
  partition_key_name = "PK"
  partition_key_type = "S"
  sort_key_name      = "SK"
  sort_key_type      = "S"
  billing_mode       = "PAY_PER_REQUEST"

  attributes = [
    {
      name = "actor_uid"
      type = "S"
    },
    {
      name = "action"
      type = "S"
    },
    {
      name = "target_uid"
      type = "S"
    },
    {
      name = "session_id"
      type = "S"
    },
    {
      name = "report_request_id"
      type = "S"
    },
    {
      name = "participant_id"
      type = "S"
    },
    {
      name = "timestamp"
      type = "S"
    }
  ]

  global_secondary_indexes = [
    {
      name            = "gsi-audit-actor-date"
      hash_key        = "actor_uid"
      range_key       = "timestamp"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name            = "gsi-audit-action-month"
      hash_key        = "action"
      range_key       = "SK"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name            = "gsi-audit-targetuser"
      hash_key        = "target_uid"
      range_key       = "timestamp"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name            = "gsi-audit-session"
      hash_key        = "session_id"
      range_key       = "timestamp"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name            = "gsi-audit-report"
      hash_key        = "report_request_id"
      range_key       = "timestamp"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name            = "gsi-audit-participant"
      hash_key        = "participant_id"
      range_key       = "timestamp"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    }
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

# ============================================================
# DYNAMODB REPORT REQUESTS
# ============================================================

module "dynamodb_report_requests" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-report-requests"
  partition_key_name = "requester_uid"
  partition_key_type = "S"
  sort_key_name      = "sk"
  sort_key_type      = "S"
  billing_mode       = "PAY_PER_REQUEST"

  attributes = [
    {
      name = "report_request_id"
      type = "S"
    },
    {
      name = "session_id"
      type = "S"
    },
    {
      name = "status"
      type = "S"
    },
    {
      name = "requested_at"
      type = "S"
    }
  ]

  global_secondary_indexes = [
    {
      name            = "gsi-report-status-date"
      hash_key        = "status"
      range_key       = "requested_at"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name            = "gsi-report-session"
      hash_key        = "session_id"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name            = "gsi-report-id"
      hash_key        = "report_request_id"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    }
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

# ============================================================
# S3 REPORT PDFS
# ============================================================

module "s3_report_pdfs" {
  source = "../../modules/s3_private_bucket"

  bucket_name = var.report_pdfs_bucket_name != "" ? var.report_pdfs_bucket_name : "${local.name_prefix}-report-pdfs-${local.account_id}"
  env         = local.env
  project     = local.project

}

# ============================================================
# S3 RAW SESSIONS
# ============================================================

module "s3_sessions_raw" {
  source = "../../modules/s3_private_bucket"

  bucket_name = var.sessions_raw_bucket_name != "" ? var.sessions_raw_bucket_name : "${local.name_prefix}-sessions-raw-${local.account_id}"
  env         = local.env
  project     = local.project

}

# ============================================================
# SSM PARAMETER STORE
# ============================================================

module "parameter_store" {
  source = "../../modules/parameter_store_params"

  env     = local.env
  project = local.project

  params = {
    "/emotionlab/dev/cognito/user-pool-id" = module.cognito.user_pool_id

    "/emotionlab/dev/cognito/app-client-id" = module.cognito.app_client_id

    "/emotionlab/dev/s3/report-pdfs-bucket" = module.s3_report_pdfs.bucket_name

    "/emotionlab/dev/s3/sessions-raw-bucket" = module.s3_sessions_raw.bucket_name

    "/emotionlab/dev/dynamodb/users-table" = module.dynamodb_users.table_name

    "/emotionlab/dev/dynamodb/participants-table" = module.dynamodb_participants.table_name

    "/emotionlab/dev/dynamodb/sessions-table" = module.dynamodb_sessions.table_name

    "/emotionlab/dev/dynamodb/audit-table" = module.dynamodb_audit_logs.table_name

    "/emotionlab/dev/dynamodb/report-requests-table" = module.dynamodb_report_requests.table_name

    "/emotionlab/dev/ses/sender-email" = var.email_sender_address
  }
}

# ============================================================
# CLOUDWATCH LOG GROUPS
# ============================================================

module "cloudwatch_logs" {
  source = "../../modules/cloudwatch_log_groups"

  env     = local.env
  project = local.project

  log_groups = [
    {
      name              = "/emotionlab/dev/backend"
      retention_in_days = 90
    },
    {
      name              = "/emotionlab/dev/migration"
      retention_in_days = 180
    }
  ]
}

# ============================================================
# LAMBDA BACKEND
# ============================================================

module "lambda_backend" {
  source = "../../modules/lambda"

  function_name = local.backend_lambda_name
  role_arn      = local.lambda_role

  package_type = "Zip"
  runtime      = "python3.13"
  handler      = "lambda_handler.lambda_handler"

  filename = "${path.root}/../../packages/backend.zip"

  timeout     = 60
  memory_size = 1024

  environment_variables = {
    JWT_SECRET            = var.jwt_secret
    COGNITO_APP_CLIENT_ID = module.cognito.app_client_id

    # La URL de /predict queda determinada por el API Gateway
    # de EmotionLab.
    PREDICTION_API_URL = "${module.api_gateway.api_endpoint}/predict"

    STORAGE_BACKEND      = "dynamodb"
    COGNITO_USER_POOL_ID = module.cognito.user_pool_id
  }
}

# ============================================================
# LAMBDA MODEL
# ============================================================

module "lambda_model" {
  count = var.model_image_uri != "" ? 1 : 0

  source = "../../modules/lambda"

  function_name = local.model_lambda_name
  role_arn      = local.lambda_role
  package_type  = "Image"
  image_uri     = var.model_image_uri
  timeout       = 30
  memory_size   = 1024
}

# ============================================================
# LAMBDA SYNC
# ============================================================

module "lambda_sync" {
  source = "../../modules/lambda"

  function_name = local.sync_lambda_name
  role_arn      = local.lambda_role

  package_type = "Zip"
  runtime      = "python3.12"
  handler      = "lambda_function.lambda_handler"

  filename = "${path.root}/../../packages/sync.zip"

  timeout     = 120
  memory_size = 512

  environment_variables = {
    PREDICTION_API_URL = "${module.api_gateway.api_endpoint}/predict"

    # API EXTERNA DEL PROYECTO UNITY.
    # Terraform NO crea ni administra esta API.
    AWS_UNITY_DATA_URL = var.unity_data_url

    DYNAMODB_TABLE_SESSIONS = module.dynamodb_sessions.table_name
  }
}

# ============================================================
# LAMBDA TEST
# ============================================================

module "lambda_test" {
  source = "../../modules/lambda"

  function_name = local.test_lambda_name
  role_arn      = local.lambda_role

  package_type = "Zip"
  runtime      = "python3.13"
  handler      = "lambda_function.lambda_handler"

  filename = "${path.root}/../../packages/test.zip"

  timeout     = 30
  memory_size = 1024
}

# ============================================================
# API GATEWAY
# ============================================================
module "api_gateway" {
  source = "../../modules/api_gateway"

  api_name            = "emotionlab-test-api"
  backend_lambda_name = local.backend_lambda_name
  model_lambda_name   = local.model_lambda_name
  test_lambda_name    = local.test_lambda_name
  model_enabled       = var.model_image_uri != ""
}