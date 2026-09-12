variable "table_name" {
  description = "Nombre de la tabla DynamoDB"
  type        = string
}

variable "partition_key_name" {
  description = "Nombre del atributo que sera la Partition Key (PK)"
  type        = string
}

variable "partition_key_type" {
  description = "Tipo de dato de la PK: S, N o B"
  type        = string
  default     = "S"
}

variable "sort_key_name" {
  description = "Nombre del atributo Sort Key (SK, opcional)"
  type        = string
  default     = null
}

variable "sort_key_type" {
  description = "Tipo de dato del SK: S, N o B"
  type        = string
  default     = null
}

variable "billing_mode" {
  description = "Modo de facturacion: PAY_PER_REQUEST o PROVISIONED"
  type        = string
  default     = "PAY_PER_REQUEST"
  validation {
    condition     = contains(["PAY_PER_REQUEST", "PROVISIONED"], var.billing_mode)
    error_message = "billing_mode debe ser PAY_PER_REQUEST o PROVISIONED"
  }
}

variable "read_capacity" {
  description = "RCU (solo si billing_mode=PROVISIONED)"
  type        = number
  default     = null
}

variable "write_capacity" {
  description = "WCU (solo si billing_mode=PROVISIONED)"
  type        = number
  default     = null
}

variable "attributes" {
  description = "Lista de atributos de nivel superior que deben declararse (PK, SK y atributos usados en GSIs)"
  type = list(object({
    name = string
    type = string
  }))
  default = []
}

variable "global_secondary_indexes" {
  description = "Lista de GSIs a crear en la tabla"
  type = list(object({
    name               = string
    hash_key           = string
    range_key          = optional(string, null)
    projection_type    = string
    non_key_attributes = optional(list(string), [])
    read_capacity      = optional(number, null)
    write_capacity     = optional(number, null)
  }))
  default = []
}

variable "tags" {
  description = "Tags para recursos"
  type        = map(string)
  default     = {}
}

locals {
  _has_sk = var.sort_key_name != null && var.sort_key_type != null

  # ============================================================
  # CONJUNTO VERDADERO DE ATRIBUTOS QUE DEBEN DECLARARSE:
  #   · PK de la tabla
  #   · SK de la tabla (si existe)
  #   · hash_key y range_key de CADA GSI (nada más)
  #
  # En DynamoDB NO se declaran atributos usados únicamente en:
  #   - projection_type INCLUDE non_key_attributes
  #   - campos normales del item
  # ============================================================
  # ============================================================
  # Construye _key_attrs: cada entrada tiene (name, type).
  #   · PK y SK usan partition_key_type / sort_key_type (variables explicitas).
  #   · hash_key y range_key de GSI:
  #       - si el atributo coincidía con PK o SK, heredan type de la KeySchema.
  #       - si el usuario declaró el atributo en var.attributes, usan ese type.
  #       - si no aparecen en var.attributes ni en KeySchema, usan "S" por defecto.
  # ============================================================
  _pk_sk_by_name = merge(
    { for a in [{ name = var.partition_key_name, type = var.partition_key_type }] : a.name => a.type },
    local._has_sk ? { for a in [{ name = var.sort_key_name, type = var.sort_key_type }] : a.name => a.type } : {}
  )
  _user_attrs_by_name = { for a in var.attributes : a.name => a.type }

  _key_attrs = concat(
    [
      {
        name = var.partition_key_name
        type = var.partition_key_type
      }
    ],
    local._has_sk ? [
      {
        name = var.sort_key_name
        type = var.sort_key_type
      }
    ] : [],
    # Eliminamos nulls explicitamente (si no, el for comprehension group-by falla
    # al intentar acceder a .name sobre un valor null).
    [
      for entry in flatten([
        for gsi in var.global_secondary_indexes : [
          for k in [
            { name = gsi.hash_key },
            (gsi.range_key != null ? { name = gsi.range_key } : null)
          ] :
          k != null ? {
            name = k.name,
            type = lookup(local._pk_sk_by_name, k.name, lookup(local._user_attrs_by_name, k.name, "S"))
          } : null
        ]
      ]) : entry
      if entry != null
    ]
  )

  # ============================================================
  # Deduplica por name. Si un mismo atributo aparece varias veces
  # (p.ej. session_id como PK y range de algún GSI) conserva el
  # type. Si llegaran a existir 2 types para el mismo name, esto
  # falla luego en la validación de tipos.
  # ============================================================
  _key_attrs_by_name = {
    for a in local._key_attrs : a.name => a...
  }

  _merged_key_attrs = [
    for name, entries in local._key_attrs_by_name : {
      name = name
      type = entries[0].type
    }
  ]

  _merged_key_attrs_by_name = {
    for a in local._merged_key_attrs : a.name => a
  }

  # ============================================================
  # Validación: var.attributes NO debe contener atributos que NO
  # se usen en KeySchema/GSIs (son innecesarios y potencialmente
  # fuente de duplicados con PK/SK).
  # ============================================================
  _extraneous_user_attrs = [
    for a in var.attributes : a if !contains(keys(local._merged_key_attrs_by_name), a.name)
  ]
  _extraneous_msg = length(local._extraneous_user_attrs) > 0 ? join(", ", [
    for a in local._extraneous_user_attrs : "${a.name} (type=${a.type})"
  ]) : ""

  # ============================================================
  # Validación: si el usuario incluyó explícitamente en
  # var.attributes un atributo que ya pertenece a KeySchema o a
  # un GSI key (para proveer un tipo distinto), verificamos que
  # el tipo COINCIDA. Si no coincide, fallamos con mensaje.
  # ============================================================
  _type_mismatches = [
    for name, user_type in local._user_attrs_by_name :
    { name = name, declared = user_type, used_as = local._merged_key_attrs_by_name[name].type }
    if contains(keys(local._merged_key_attrs_by_name), name) && user_type != local._merged_key_attrs_by_name[name].type
  ]
  _type_mismatch_msg = length(local._type_mismatches) > 0 ? join(", ", [
    for m in local._type_mismatches : "${m.name} (var=${m.declared} vs key_schema/gsi=${m.used_as})"
  ]) : ""

  # Atributos finales (sin duplicados): tomamos _merged_key_attrs (dedup).
  # Si var.attributes provee el mismo atributo con el mismo tipo, NO lo
  # duplicamos; es idempotente.
  _all_attrs = local._merged_key_attrs
}

# ============================================================
# Precondiciones: fallamos ANTES del plan/apply si hay errores.
# Esto evita el "Duplicate object key" y además evita atributos
# declarados innecesariamente (o con tipos que no coinciden).
#
# NOTA: "terraform_data" es un RESOURCE (managed) NO un data source.
# ============================================================
resource "terraform_data" "validate" {
  count = length(local._extraneous_user_attrs) > 0 ? 1 : 0
  triggers_replace = {
    error = "dynamodb_table module '${var.table_name}': atributos en var.attributes NO usados en KeySchema ni en GSI hash/range keys. Elimine: ${local._extraneous_msg}. En DynamoDB solo se declaran atributos usados por la tabla y por los indices."
  }
}

resource "terraform_data" "validate_types" {
  count = length(local._type_mismatches) > 0 ? 1 : 0
  triggers_replace = {
    error = "dynamodb_table module '${var.table_name}': tipo distinto entre var.attributes y KeySchema/GSI keys. Corrija: ${local._type_mismatch_msg}."
  }
}

resource "aws_dynamodb_table" "table" {
  name           = var.table_name
  billing_mode   = var.billing_mode
  read_capacity  = var.billing_mode == "PROVISIONED" ? var.read_capacity : null
  write_capacity = var.billing_mode == "PROVISIONED" ? var.write_capacity : null

  hash_key  = var.partition_key_name
  range_key = local._has_sk ? var.sort_key_name : null

  dynamic "attribute" {
    for_each = local._all_attrs
    content {
      name = attribute.value.name
      type = attribute.value.type
    }
  }

  dynamic "global_secondary_index" {
    for_each = var.global_secondary_indexes
    content {
      name               = global_secondary_index.value.name
      hash_key           = global_secondary_index.value.hash_key
      range_key          = global_secondary_index.value.range_key
      projection_type    = global_secondary_index.value.projection_type
      non_key_attributes = global_secondary_index.value.projection_type == "INCLUDE" ? global_secondary_index.value.non_key_attributes : null
      read_capacity      = var.billing_mode == "PROVISIONED" ? global_secondary_index.value.read_capacity : null
      write_capacity     = var.billing_mode == "PROVISIONED" ? global_secondary_index.value.write_capacity : null
    }
  }

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = null
  }

  tags = var.tags
}

output "table_name" {
  description = "Nombre de la tabla DynamoDB creada"
  value       = aws_dynamodb_table.table.name
}

output "table_arn" {
  description = "ARN de la tabla DynamoDB"
  value       = aws_dynamodb_table.table.arn
}
