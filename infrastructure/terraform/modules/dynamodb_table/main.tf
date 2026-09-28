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
  description = "Lista de atributos que participan en la PK, SK o claves de GSIs"
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

  # ============================================================
  # Determina si la tabla tiene Sort Key
  # ============================================================

  _has_sk = (
    var.sort_key_name != null &&
    var.sort_key_type != null
  )

  # ============================================================
  # PK y SK de la tabla
  # ============================================================

  _pk_sk_by_name = merge(
    {
      (var.partition_key_name) = var.partition_key_type
    },
    local._has_sk ? {
      (var.sort_key_name) = var.sort_key_type
    } : {}
  )

  # ============================================================
  # Atributos entregados explícitamente por el módulo
  #
  # IMPORTANTE:
  # Solo se utilizan para determinar el tipo de una clave.
  # Los atributos normales del item NO deben declararse aquí.
  # ============================================================

  _user_attrs_by_name = {
    for a in var.attributes :
    a.name => a.type
  }

  # ============================================================
  # Construye todas las claves que DynamoDB realmente necesita
  # declarar en AttributeDefinitions:
  #
  #   - PK de la tabla
  #   - SK de la tabla
  #   - hash_key de cada GSI
  #   - range_key de cada GSI
  #
  # NO incluye:
  #   - non_key_attributes
  #   - campos normales de los items
  # ============================================================

  _key_attrs = concat(

    # ----------------------------------------------------------
    # Partition Key de la tabla
    # ----------------------------------------------------------

    [
      {
        name = var.partition_key_name
        type = var.partition_key_type
      }
    ],

    # ----------------------------------------------------------
    # Sort Key de la tabla, si existe
    # ----------------------------------------------------------

    local._has_sk ? [
      {
        name = var.sort_key_name
        type = var.sort_key_type
      }
    ] : [],

    # ----------------------------------------------------------
    # Claves de todos los GSIs
    # ----------------------------------------------------------

    [
      for entry in flatten([
        for gsi in var.global_secondary_indexes : [

          # Hash key
          {
            name = gsi.hash_key

            type = lookup(
              local._pk_sk_by_name,
              gsi.hash_key,
              lookup(
                local._user_attrs_by_name,
                gsi.hash_key,
                "S"
              )
            )
          },

          # Range key, si existe
          gsi.range_key != null ? {
            name = gsi.range_key

            type = lookup(
              local._pk_sk_by_name,
              gsi.range_key,
              lookup(
                local._user_attrs_by_name,
                gsi.range_key,
                "S"
              )
            )
          } : null
        ]
      ]) :

      entry

      if entry != null
    ]
  )

  # ============================================================
  # Agrupa las claves por nombre para eliminar duplicados.
  #
  # Ejemplo:
  # session_id puede ser:
  #   - clave de un GSI
  #   - range key de otro GSI
  #
  # DynamoDB solo debe recibirlo una vez.
  # ============================================================

  _key_attrs_by_name = {
    for a in local._key_attrs :
    a.name => a...
  }

  # ============================================================
  # Fusiona las claves duplicadas.
  # ============================================================

  _merged_key_attrs = [
    for name, entries in local._key_attrs_by_name : {
      name = name
      type = entries[0].type
    }
  ]

  # ============================================================
  # Mapa final de claves
  # ============================================================

  _merged_key_attrs_by_name = {
    for a in local._merged_key_attrs :
    a.name => a
  }

  # ============================================================
  # Detecta atributos declarados en var.attributes que realmente
  # NO son claves de DynamoDB.
  #
  # Esto evita errores como:
  #
  #   nombre
  #   pred_bienestar
  #   hora
  #   edad
  #
  # cuando solamente aparecen como non_key_attributes.
  # ============================================================

  _extraneous_user_attrs = [
    for a in var.attributes :
    a
    if !contains(
      keys(local._merged_key_attrs_by_name),
      a.name
    )
  ]

  _extraneous_msg = (
    length(local._extraneous_user_attrs) > 0
    ? join(
      ", ",
      [
        for a in local._extraneous_user_attrs :
        "${a.name} (type=${a.type})"
      ]
    )
    : ""
  )

  # ============================================================
  # Verifica que si el usuario declara explícitamente una clave
  # en var.attributes, el tipo coincida.
  # ============================================================

  _type_mismatches = [
    for name, user_type in local._user_attrs_by_name :
    {
      name     = name
      declared = user_type
      used_as  = local._merged_key_attrs_by_name[name].type
    }

    if contains(
      keys(local._merged_key_attrs_by_name),
      name
    ) && user_type != local._merged_key_attrs_by_name[name].type
  ]

  _type_mismatch_msg = (
    length(local._type_mismatches) > 0
    ? join(
      ", ",
      [
        for m in local._type_mismatches :
        "${m.name} (var=${m.declared} vs key_schema/gsi=${m.used_as})"
      ]
    )
    : ""
  )

  # ============================================================
  # Atributos finales que Terraform enviará a DynamoDB.
  #
  # SOLO contiene atributos utilizados como:
  #   - PK
  #   - SK
  #   - GSI hash key
  #   - GSI range key
  # ============================================================

  _all_attrs = local._merged_key_attrs
}

# ==============================================================
# VALIDACIÓN DE ATRIBUTOS EXCEDENTES
#
# Si var.attributes contiene algo que no es PK/SK/GSI key,
# se genera un recurso terraform_data para hacer visible
# el problema antes del apply.
# ==============================================================

resource "terraform_data" "validate" {

  count = (
    length(local._extraneous_user_attrs) > 0
    ? 1
    : 0
  )

  triggers_replace = {
    error = "dynamodb_table module '${var.table_name}': atributos en var.attributes NO usados en KeySchema ni en GSI hash/range keys. Elimine: ${local._extraneous_msg}. En DynamoDB solo se declaran atributos usados por la tabla y por los indices."
  }
}

# ==============================================================
# VALIDACIÓN DE TIPOS
# ==============================================================

resource "terraform_data" "validate_types" {

  count = (
    length(local._type_mismatches) > 0
    ? 1
    : 0
  )

  triggers_replace = {
    error = "dynamodb_table module '${var.table_name}': tipo distinto entre var.attributes y KeySchema/GSI keys. Corrija: ${local._type_mismatch_msg}."
  }
}

# ==============================================================
# TABLA DYNAMODB
# ==============================================================

resource "aws_dynamodb_table" "table" {

  name = var.table_name

  billing_mode = var.billing_mode

  read_capacity = (
    var.billing_mode == "PROVISIONED"
    ? var.read_capacity
    : null
  )

  write_capacity = (
    var.billing_mode == "PROVISIONED"
    ? var.write_capacity
    : null
  )

  # ------------------------------------------------------------
  # PK
  # ------------------------------------------------------------

  hash_key = var.partition_key_name

  # ------------------------------------------------------------
  # SK
  # ------------------------------------------------------------

  range_key = (
    local._has_sk
    ? var.sort_key_name
    : null
  )

  # ------------------------------------------------------------
  # AttributeDefinitions
  #
  # IMPORTANTE:
  # local._all_attrs contiene solamente las claves.
  # ------------------------------------------------------------

  dynamic "attribute" {

    for_each = local._all_attrs

    content {

      name = attribute.value.name

      type = attribute.value.type
    }
  }

  # ============================================================
  # GLOBAL SECONDARY INDEXES
  # ============================================================

  dynamic "global_secondary_index" {

    for_each = var.global_secondary_indexes

    content {

      name = global_secondary_index.value.name

      hash_key = global_secondary_index.value.hash_key

      range_key = global_secondary_index.value.range_key

      projection_type = (
        global_secondary_index.value.projection_type
      )

      # --------------------------------------------------------
      # INCLUDE:
      #
      # Aquí SÍ pueden aparecer atributos normales como:
      #
      # nombre
      # pred_bienestar
      # fecha
      # hora
      # edad
      # genero
      # difficulty
      #
      # Estos NO deben aparecer en AttributeDefinitions.
      # --------------------------------------------------------

      non_key_attributes = (
        global_secondary_index.value.projection_type == "INCLUDE"
        ? global_secondary_index.value.non_key_attributes
        : null
      )

      # --------------------------------------------------------
      # Capacidad de lectura
      # --------------------------------------------------------

      read_capacity = (
        var.billing_mode == "PROVISIONED"
        ? global_secondary_index.value.read_capacity
        : null
      )

      # --------------------------------------------------------
      # Capacidad de escritura
      # --------------------------------------------------------

      write_capacity = (
        var.billing_mode == "PROVISIONED"
        ? global_secondary_index.value.write_capacity
        : null
      )
    }
  }

  # ============================================================
  # POINT IN TIME RECOVERY
  # ============================================================

  point_in_time_recovery {
    enabled = false
  }

  # ============================================================
  # SERVER SIDE ENCRYPTION
  # ============================================================

  server_side_encryption {

    enabled = true

    kms_key_arn = null
  }

  # ============================================================
  # TAGS
  # ============================================================

  tags = var.tags
}

# ==============================================================
# OUTPUTS
# ==============================================================

output "table_name" {

  description = "Nombre de la tabla DynamoDB creada"

  value = aws_dynamodb_table.table.name
}

output "table_arn" {

  description = "ARN de la tabla DynamoDB"

  value = aws_dynamodb_table.table.arn
}