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

locals {
  env         = "dev"
  project     = "emotionlab"
  name_prefix = "${local.project}-${local.env}"
}

module "cognito" {
  source = "../../modules/cognito"

  name_prefix          = local.name_prefix
  env                  = local.env
  project              = local.project
  email_sender_address = var.email_sender_address
  email_sender_name    = var.email_sender_name
  ses_source_arn       = var.ses_source_arn
}

module "s3_data" {
  source = "../../modules/s3_data_bucket"

  bucket_name = "emotionlab-dev-data"
  project     = local.project
  env         = local.env
}

module "dynamodb_users" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-users"
  partition_key_name = "uid"
  partition_key_type = "S"
  sort_key_name      = null
  sort_key_type      = null
  billing_mode       = "PAY_PER_REQUEST"

  #
  # Regla: solo atributos usados en KeySchema o como hash/range de GSI.
  #   uid         -> PK
  #   email       -> gsi-users-email hash_key
  #   rol         -> gsi-users-rol hash_key
  #   created_at  -> gsi-users-rol range_key  (ANTES FALTABA - provoca Invalid GSI range_key attribute missing)
  #
  attributes = [
    { name = "email", type = "S" },
    { name = "rol", type = "S" },
    { name = "created_at", type = "S" },
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
    },
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

module "dynamodb_participants" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-participants"
  partition_key_name = "participant_id"
  partition_key_type = "S"
  sort_key_name      = null
  sort_key_type      = null
  billing_mode       = "PAY_PER_REQUEST"

  #
  # participant_id es PK -> NO se repite en attributes (evitar duplicado).
  # Solo: np_1..np_4 (hash_keys de los 4 GSIs) y nombre_normalizado (range_key comun).
  #
  attributes = [
    { name = "np_1", type = "S" },
    { name = "np_2", type = "S" },
    { name = "np_3", type = "S" },
    { name = "np_4", type = "S" },
    { name = "nombre_normalizado", type = "S" },
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
    },
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

module "dynamodb_sessions" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-sessions"
  partition_key_name = "participant_id"
  partition_key_type = "S"
  sort_key_name      = "sk"
  sort_key_type      = "S"
  billing_mode       = "PAY_PER_REQUEST"

  #
  # participant_id y sk son PK+SK -> NO repetir en attributes.
  # Solo claves de los GSIs:
  #   status, created_at  (GSI1 hash+range)
  #   session_id          (GSI2 hash; GSI4 range)
  #   source_key          (GSI3 hash)
  #   fecha               (GSI4 hash)
  #
  attributes = [
    { name = "session_id", type = "S" },
    { name = "status", type = "S" },
    { name = "fecha", type = "S" },
    { name = "source_key", type = "S" },
    { name = "created_at", type = "S" },
    # GSI aditivo para la búsqueda exacta por nombre ya usada en runtime.
    { name = "nombre", type = "S" },
  ]

  global_secondary_indexes = [
    {
      name               = "gsi-sessions-status-date"
      hash_key           = "status"
      range_key          = "created_at"
      projection_type    = "INCLUDE"
      non_key_attributes = ["session_id", "participant_id", "nombre", "pred_bienestar", "fecha", "hora", "edad", "genero", "difficulty"]
      read_capacity      = null
      write_capacity     = null
    },
    {
      name            = "gsi-sessions-sessionid"
      hash_key        = "session_id"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
    {
      name               = "gsi-sessions-sourcekey"
      hash_key           = "source_key"
      projection_type    = "INCLUDE"
      non_key_attributes = ["participant_id", "sk", "session_id"]
      read_capacity      = null
      write_capacity     = null
    },
    {
      name               = "gsi-sessions-fecha"
      hash_key           = "fecha"
      range_key          = "session_id"
      projection_type    = "INCLUDE"
      non_key_attributes = ["status", "pred_bienestar", "nombre", "participant_id"]
      read_capacity      = null
      write_capacity     = null
    },
    # session_id ordena resultados de nombres coincidentes sin cambiar PK/SK.
    {
      name            = "gsi-sessions-nombre"
      hash_key        = "nombre"
      range_key       = "session_id"
      projection_type = "ALL"
      read_capacity   = null
      write_capacity  = null
    },
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

module "dynamodb_audit_logs" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-audit-logs"
  partition_key_name = "PK"
  partition_key_type = "S"
  sort_key_name      = "SK"
  sort_key_type      = "S"
  billing_mode       = "PAY_PER_REQUEST"

  #
  # PK y SK son la KeySchema -> NO repetir en attributes.
  # audit_id NUNCA se usa en ningun GSI/KeySchema -> ELIMINADO (era atributo innecesario).
  # Solo se declaran las claves de los 6 GSIs:
  #   actor_uid, target_uid, session_id, report_request_id, participant_id, timestamp
  #   action (hash), SK (range ya declarado en KeySchema)
  #
  attributes = [
    { name = "actor_uid", type = "S" },
    { name = "action", type = "S" },
    { name = "target_uid", type = "S" },
    { name = "session_id", type = "S" },
    { name = "report_request_id", type = "S" },
    { name = "participant_id", type = "S" },
    { name = "timestamp", type = "S" },
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
    },
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

module "dynamodb_report_requests" {
  source = "../../modules/dynamodb_table"

  table_name         = "${local.name_prefix}-report-requests"
  partition_key_name = "requester_uid"
  partition_key_type = "S"
  sort_key_name      = "sk"
  sort_key_type      = "S"
  billing_mode       = "PAY_PER_REQUEST"

  #
  # requester_uid y sk son PK+SK -> NO repetir en attributes.
  # Solo claves de los 3 GSIs:
  #   status, requested_at  (GSI1 hash+range)
  #   session_id            (GSI2 hash)
  #   report_request_id     (GSI3 hash)
  #
  attributes = [
    { name = "report_request_id", type = "S" },
    { name = "session_id", type = "S" },
    { name = "status", type = "S" },
    { name = "requested_at", type = "S" },
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
    },
  ]

  tags = {
    Project = local.project
    Env     = local.env
  }
}

module "s3_report_pdfs" {
  source = "../../modules/s3_private_bucket"

  bucket_name = "${local.name_prefix}-report-pdfs"
  env         = local.env
  project     = local.project
  tags = {
    Purpose = "Report PDFs storage"
  }
}

module "s3_sessions_raw" {
  source = "../../modules/s3_private_bucket"

  bucket_name = "${local.name_prefix}-sessions-raw"
  env         = local.env
  project     = local.project
  tags = {
    Purpose = "Session raw JSON data (when >350KB)"
  }
}

module "parameter_store" {
  source = "../../modules/parameter_store_params"

  env     = local.env
  project = local.project
  params = {
    "/emotionlab/dev/cognito/user-pool-id"           = module.cognito.user_pool_id
    "/emotionlab/dev/cognito/app-client-id"          = module.cognito.app_client_id
    "/emotionlab/dev/s3/report-pdfs-bucket"          = module.s3_report_pdfs.bucket_name
    "/emotionlab/dev/s3/sessions-raw-bucket"         = module.s3_sessions_raw.bucket_name
    "/emotionlab/dev/dynamodb/users-table"           = module.dynamodb_users.table_name
    "/emotionlab/dev/dynamodb/participants-table"    = module.dynamodb_participants.table_name
    "/emotionlab/dev/dynamodb/sessions-table"        = module.dynamodb_sessions.table_name
    "/emotionlab/dev/dynamodb/audit-table"           = module.dynamodb_audit_logs.table_name
    "/emotionlab/dev/dynamodb/report-requests-table" = module.dynamodb_report_requests.table_name
    "/emotionlab/dev/ses/sender-email"               = var.email_sender_address
  }
}

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
    },
  ]
}
