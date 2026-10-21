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
  env         = var.environment
  project     = var.project_name
  name_prefix = "${local.project}-${local.env}"

  account_id  = data.aws_caller_identity.current.account_id
  lambda_role = var.create_lambda_role ? aws_iam_role.lambda[0].arn : var.existing_lambda_role_arn
  sync_role   = var.create_lambda_role ? aws_iam_role.lambda_sync[0].arn : var.existing_lambda_role_arn
  model_role  = var.create_lambda_role && var.model_image_uri != "" ? aws_iam_role.lambda_model[0].arn : var.existing_lambda_role_arn
  test_role   = var.create_lambda_role ? aws_iam_role.lambda_test[0].arn : var.existing_lambda_role_arn

  backend_lambda_name = "${local.name_prefix}-backend"
  model_lambda_name   = "${local.name_prefix}-model"
  sync_lambda_name    = "${local.name_prefix}-sync"
  test_lambda_name    = "${local.name_prefix}-test"
}

resource "aws_iam_role" "lambda" {
  count = var.create_lambda_role ? 1 : 0
  name  = "${local.name_prefix}-lambda-execution"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Project = local.project, Environment = local.env }
}

# Sync, model and health-test functions receive separate execution roles so
# they do not inherit the backend's Cognito, SES, SSM and full-table access.
resource "aws_iam_role" "lambda_sync" {
  count = var.create_lambda_role ? 1 : 0
  name  = "${local.name_prefix}-sync-execution"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Project = local.project, Environment = local.env }
}

resource "aws_iam_role_policy" "lambda_sync" {
  count = var.create_lambda_role ? 1 : 0
  name  = "${local.name_prefix}-sync-storage-access"
  role  = aws_iam_role.lambda_sync[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable"]
        Resource = [module.dynamodb_sessions.table_arn, "${module.dynamodb_sessions.table_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${module.s3_data.bucket_arn}/*", "${module.s3_sessions_raw.bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${local.sync_lambda_name}:*"
      }
      ], var.s3_kms_key_arn != "" ? [{
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.s3_kms_key_arn
    }] : [])
  })
}

resource "aws_iam_role" "lambda_model" {
  count = var.create_lambda_role && var.model_image_uri != "" ? 1 : 0
  name  = "${local.name_prefix}-model-execution"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Project = local.project, Environment = local.env }
}

resource "aws_iam_role_policy" "lambda_model_logs" {
  count = var.create_lambda_role && var.model_image_uri != "" ? 1 : 0
  name  = "${local.name_prefix}-model-logs"
  role  = aws_iam_role.lambda_model[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${local.model_lambda_name}:*"
    }]
  })
}

resource "aws_iam_role" "lambda_test" {
  count = var.create_lambda_role ? 1 : 0
  name  = "${local.name_prefix}-test-execution"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Project = local.project, Environment = local.env }
}

resource "aws_iam_role_policy" "lambda_test_logs" {
  count = var.create_lambda_role ? 1 : 0
  name  = "${local.name_prefix}-test-logs"
  role  = aws_iam_role.lambda_test[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${local.test_lambda_name}:*"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_app" {
  count = var.create_lambda_role ? 1 : 0
  name  = "${local.name_prefix}-application-access"
  role  = aws_iam_role.lambda[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchWriteItem"], Resource = concat([for arn in [module.dynamodb_users.table_arn, module.dynamodb_participants.table_arn, module.dynamodb_sessions.table_arn, module.dynamodb_audit_logs.table_arn, module.dynamodb_report_requests.table_arn] : "${arn}/*"], [module.dynamodb_users.table_arn, module.dynamodb_participants.table_arn, module.dynamodb_sessions.table_arn, module.dynamodb_audit_logs.table_arn, module.dynamodb_report_requests.table_arn]) },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = ["${module.s3_data.bucket_arn}/*", "${module.s3_report_pdfs.bucket_arn}/*", "${module.s3_sessions_raw.bucket_arn}/*"] },
      { Effect = "Allow", Action = ["cognito-idp:AdminCreateUser", "cognito-idp:AdminUpdateUserAttributes", "cognito-idp:AdminSetUserPassword", "cognito-idp:AdminDisableUser", "cognito-idp:AdminEnableUser", "cognito-idp:AdminDeleteUser", "cognito-idp:AdminInitiateAuth", "cognito-idp:AdminRespondToAuthChallenge"], Resource = [module.cognito.user_pool_arn] },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.jwt_secret.arn },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/lambda/${local.backend_lambda_name}:*" },
    ], var.email_sender_address != "" ? [{ Effect = "Allow", Action = ["ses:SendEmail"], Resource = var.ses_source_arn != "" ? var.ses_source_arn : "arn:aws:ses:${var.aws_region}:${local.account_id}:identity/${var.email_sender_address}" }] : [], var.s3_kms_key_arn != "" ? [{ Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.s3_kms_key_arn }] : [])
  })
}

resource "aws_secretsmanager_secret" "jwt_secret" {
  name                    = "${local.project}/${local.env}/JWT_SECRET"
  recovery_window_in_days = 30
  tags                    = { Project = local.project, Environment = local.env }
}
module "ecr_model" {
  source = "../../modules/ecr"

  repository_name = var.model_ecr_repository_name
  project         = local.project
  env             = local.env
}

module "frontend_hosting" {
  count  = var.enable_frontend_hosting ? 1 : 0
  source = "../../modules/frontend_hosting"

  bucket_name         = var.frontend_bucket_name != "" ? var.frontend_bucket_name : "${local.name_prefix}-frontend-${local.account_id}"
  domain_name         = var.frontend_domain_name
  acm_certificate_arn = var.frontend_acm_certificate_arn
  project             = local.project
  env                 = local.env
}
# ============================================================
# COGNITO
# ============================================================

module "cognito" {
  source = "../../modules/cognito_portable"

  name_prefix = local.name_prefix
  env         = local.env
  project     = local.project
}

module "ses_identity" {
  count  = var.enable_ses_identity ? 1 : 0
  source = "../../modules/ses_identity"

  ses_email_address = var.email_sender_address
  project           = local.project
  env               = local.env
}

# ============================================================
# S3 DATA
# ============================================================

module "s3_data" {
  source = "../../modules/s3_data_bucket"

  bucket_name        = var.data_bucket_name != "" ? var.data_bucket_name : "${local.name_prefix}-data-${local.account_id}"
  project            = local.project
  env                = local.env
  versioning_enabled = var.s3_versioning_enabled
  kms_key_arn        = var.s3_kms_key_arn
}

# ============================================================
# DYNAMODB USERS
# ============================================================

module "dynamodb_users" {
  source = "../../modules/dynamodb_table"

  table_name                     = "${local.name_prefix}-users"
  partition_key_name             = "uid"
  partition_key_type             = "S"
  sort_key_name                  = null
  sort_key_type                  = null
  billing_mode                   = "PAY_PER_REQUEST"
  point_in_time_recovery_enabled = var.dynamodb_pitr_enabled

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

  table_name                     = "${local.name_prefix}-participants"
  partition_key_name             = "participant_id"
  partition_key_type             = "S"
  sort_key_name                  = null
  sort_key_type                  = null
  billing_mode                   = "PAY_PER_REQUEST"
  point_in_time_recovery_enabled = var.dynamodb_pitr_enabled

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

  table_name                     = "${local.name_prefix}-sessions"
  partition_key_name             = "participant_id"
  partition_key_type             = "S"
  sort_key_name                  = "sk"
  sort_key_type                  = "S"
  billing_mode                   = "PAY_PER_REQUEST"
  point_in_time_recovery_enabled = var.dynamodb_pitr_enabled

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

  table_name                     = "${local.name_prefix}-audit-logs"
  partition_key_name             = "PK"
  partition_key_type             = "S"
  sort_key_name                  = "SK"
  sort_key_type                  = "S"
  billing_mode                   = "PAY_PER_REQUEST"
  point_in_time_recovery_enabled = var.dynamodb_pitr_enabled

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

  table_name                     = "${local.name_prefix}-report-requests"
  partition_key_name             = "requester_uid"
  partition_key_type             = "S"
  sort_key_name                  = "sk"
  sort_key_type                  = "S"
  billing_mode                   = "PAY_PER_REQUEST"
  point_in_time_recovery_enabled = var.dynamodb_pitr_enabled

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

  bucket_name        = var.report_pdfs_bucket_name != "" ? var.report_pdfs_bucket_name : "${local.name_prefix}-report-pdfs-${local.account_id}"
  env                = local.env
  project            = local.project
  versioning_enabled = var.s3_versioning_enabled
  kms_key_arn        = var.s3_kms_key_arn

}

# ============================================================
# S3 RAW SESSIONS
# ============================================================

module "s3_sessions_raw" {
  source = "../../modules/s3_private_bucket"

  bucket_name        = var.sessions_raw_bucket_name != "" ? var.sessions_raw_bucket_name : "${local.name_prefix}-sessions-raw-${local.account_id}"
  env                = local.env
  project            = local.project
  versioning_enabled = var.s3_versioning_enabled
  kms_key_arn        = var.s3_kms_key_arn

}

# ============================================================
# SSM PARAMETER STORE
# ============================================================

module "parameter_store" {
  source = "../../modules/parameter_store_params"

  env     = local.env
  project = local.project

  params = {
    "/${local.project}/${local.env}/cognito/user-pool-id" = module.cognito.user_pool_id

    "/${local.project}/${local.env}/cognito/app-client-id" = module.cognito.app_client_id

    "/${local.project}/${local.env}/s3/report-pdfs-bucket" = module.s3_report_pdfs.bucket_name

    "/${local.project}/${local.env}/s3/sessions-raw-bucket" = module.s3_sessions_raw.bucket_name

    "/${local.project}/${local.env}/dynamodb/users-table" = module.dynamodb_users.table_name

    "/${local.project}/${local.env}/dynamodb/participants-table" = module.dynamodb_participants.table_name

    "/${local.project}/${local.env}/dynamodb/sessions-table" = module.dynamodb_sessions.table_name

    "/${local.project}/${local.env}/dynamodb/audit-table" = module.dynamodb_audit_logs.table_name

    "/${local.project}/${local.env}/dynamodb/report-requests-table" = module.dynamodb_report_requests.table_name

    "/${local.project}/${local.env}/ses/sender-email" = var.email_sender_address
  }
}

# ============================================================
# CLOUDWATCH LOG GROUPS
# ============================================================

module "cloudwatch_logs" {
  source = "../../modules/cloudwatch_log_groups"

  env     = local.env
  project = local.project

  log_groups = concat([
    {
      name              = "/aws/lambda/${local.backend_lambda_name}"
      retention_in_days = var.backend_log_retention_days
    },
    {
      name              = "/aws/lambda/${local.sync_lambda_name}"
      retention_in_days = var.migration_log_retention_days
    },
    {
      name              = "/aws/lambda/${local.test_lambda_name}"
      retention_in_days = var.backend_log_retention_days
    }
    ], var.model_image_uri != "" ? [{
      name              = "/aws/lambda/${local.model_lambda_name}"
      retention_in_days = var.backend_log_retention_days
  }] : [])
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

  filename   = "${path.root}/../../packages/backend.zip"
  depends_on = [aws_iam_role_policy.lambda_app, module.cloudwatch_logs]

  timeout     = 60
  memory_size = 1024

  environment_variables = {
    JWT_SECRET_ID         = aws_secretsmanager_secret.jwt_secret.arn
    COGNITO_APP_CLIENT_ID = module.cognito.app_client_id

    # La URL de /predict queda determinada por el API Gateway
    # de EmotionLab.
    PREDICTION_API_URL = "${module.api_gateway.api_endpoint}/predict"

    STORAGE_BACKEND                = "dynamodb"
    COGNITO_USER_POOL_ID           = module.cognito.user_pool_id
    AWS_REGION                     = var.aws_region
    DYNAMODB_USERS_TABLE           = module.dynamodb_users.table_name
    DYNAMODB_PARTICIPANTS_TABLE    = module.dynamodb_participants.table_name
    DYNAMODB_SESSIONS_TABLE        = module.dynamodb_sessions.table_name
    DYNAMODB_AUDIT_LOGS_TABLE      = module.dynamodb_audit_logs.table_name
    DYNAMODB_REPORT_REQUESTS_TABLE = module.dynamodb_report_requests.table_name
    S3_DATA_BUCKET                 = module.s3_data.bucket_name
    S3_REPORT_PDFS_BUCKET          = module.s3_report_pdfs.bucket_name
    S3_SESSIONS_RAW_BUCKET         = module.s3_sessions_raw.bucket_name
    SES_SENDER_ADDRESS             = var.email_sender_address
    SES_SENDER_NAME                = var.email_sender_name
    CLOUDWATCH_ENABLED             = "false"
    CORS_ALLOWED_ORIGINS           = var.cors_allowed_origins
    SSM_PREFIX                     = "/${local.project}/${local.env}/"
    ENV                            = local.env
  }
}

# ============================================================
# LAMBDA MODEL
# ============================================================

module "lambda_model" {
  count = var.model_image_uri != "" ? 1 : 0

  source = "../../modules/lambda"

  function_name = local.model_lambda_name
  role_arn      = local.model_role
  package_type  = "Image"
  image_uri     = var.model_image_uri
  timeout       = 30
  memory_size   = 1024
  depends_on    = [module.ecr_model, aws_iam_role_policy.lambda_model_logs, module.cloudwatch_logs]
}

# ============================================================
# LAMBDA SYNC
# ============================================================

module "lambda_sync" {
  source = "../../modules/lambda"

  function_name = local.sync_lambda_name
  role_arn      = local.sync_role

  package_type = "Zip"
  runtime      = "python3.13"
  handler      = "lambda_function.lambda_handler"

  filename   = "${path.root}/../../packages/sync.zip"
  depends_on = [aws_iam_role_policy.lambda_sync, module.cloudwatch_logs]

  timeout     = 120
  memory_size = 512

  environment_variables = {
    PREDICTION_API_URL = "${module.api_gateway.api_endpoint}/predict"

    # API EXTERNA DEL PROYECTO UNITY.
    # Terraform NO crea ni administra esta API.
    AWS_UNITY_DATA_URL = var.unity_data_url

    DYNAMODB_SESSIONS_TABLE = module.dynamodb_sessions.table_name
    S3_SESSIONS_RAW_BUCKET  = module.s3_sessions_raw.bucket_name
    S3_DATA_BUCKET          = module.s3_data.bucket_name
    AWS_REGION              = var.aws_region
    SSM_PREFIX              = "/${local.project}/${local.env}/"
    ENV                     = local.env
  }
}

# ============================================================
# LAMBDA TEST
# ============================================================

module "lambda_test" {
  source = "../../modules/lambda"

  function_name = local.test_lambda_name
  role_arn      = local.test_role

  package_type = "Zip"
  runtime      = "python3.13"
  handler      = "lambda_function.lambda_handler"

  filename   = "${path.root}/../../packages/test.zip"
  depends_on = [aws_iam_role_policy.lambda_test_logs, module.cloudwatch_logs]

  timeout     = 30
  memory_size = 1024
}

# ============================================================
# API GATEWAY
# ============================================================
module "api_gateway" {
  source = "../../modules/api_gateway"

  api_name             = "${local.name_prefix}-api"
  backend_lambda_name  = local.backend_lambda_name
  model_lambda_name    = local.model_lambda_name
  test_lambda_name     = local.test_lambda_name
  model_enabled        = var.model_image_uri != ""
  backend_lambda_arn   = module.lambda_backend.function_arn
  test_lambda_arn      = module.lambda_test.function_arn
  model_lambda_arn     = var.model_image_uri != "" ? module.lambda_model[0].function_arn : null
  cors_allowed_origins = split(",", var.cors_allowed_origins)
}
