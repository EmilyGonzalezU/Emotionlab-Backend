variable "name_prefix" {
  description = "Prefijo para nombres de recursos"
  type        = string
}

variable "env" {
  description = "Entorno (dev/prod)"
  type        = string
}

variable "project" {
  description = "Nombre del proyecto"
  type        = string
}

resource "aws_cognito_user_pool" "main" {
  name = "${var.name_prefix}-users"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  username_configuration {
    case_sensitive = false
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  admin_create_user_config {
    allow_admin_create_user_only = true

    invite_message_template {
      email_message = <<-HTML
        <!DOCTYPE html>
        <html lang="es">
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <title>Bienvenido a EmotionLab</title>
        </head>
        <body style="margin:0;padding:0;background-color:#eef3f7;font-family:Arial,Helvetica,sans-serif;color:#183247;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#eef3f7;">
            <tr>
              <td align="center" style="padding:32px 16px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;background-color:#ffffff;border:1px solid #d8e3ea;border-radius:12px;overflow:hidden;">
                  <tr>
                    <td style="padding:28px 32px;background-color:#123d52;color:#ffffff;">
                      <div style="font-size:24px;font-weight:bold;letter-spacing:0.4px;">EmotionLab</div>
                      <div style="margin-top:8px;font-size:12px;letter-spacing:1.6px;text-transform:uppercase;color:#9ed8d1;">Evaluación emocional y VR</div>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:36px 32px 16px;">
                      <div style="font-size:14px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#148b8a;">Bienvenido/a</div>
                      <h1 style="margin:10px 0 16px;font-size:28px;line-height:1.2;color:#123d52;">Tu cuenta ha sido creada</h1>
                      <p style="margin:0;font-size:16px;line-height:1.6;">Tu cuenta de especialista ha sido creada correctamente. Estas son tus credenciales de acceso inicial:</p>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:0 32px 24px;">
                      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#f3f8fa;border:1px solid #d8e3ea;border-radius:8px;">
                        <tr>
                          <td style="padding:18px 20px 6px;font-size:12px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#587080;">Correo</td>
                        </tr>
                        <tr>
                          <td style="padding:0 20px 16px;font-size:16px;color:#183247;word-break:break-word;">{username}</td>
                        </tr>
                        <tr>
                          <td style="padding:0 20px 6px;font-size:12px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#587080;">Contraseña temporal</td>
                        </tr>
                        <tr>
                          <td style="padding:0 20px 20px;font-family:Courier New,monospace;font-size:22px;font-weight:bold;letter-spacing:2px;color:#123d52;word-break:break-word;">{####}</td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:0 32px 28px;">
                      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#fff8e8;border-left:4px solid #e2a83b;">
                        <tr>
                          <td style="padding:16px;font-size:14px;line-height:1.6;color:#624c20;">Por seguridad, al iniciar sesión por primera vez deberás establecer una nueva contraseña. Tu contraseña temporal es válida durante 7 días.</td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:0 32px 32px;font-size:14px;line-height:1.6;color:#587080;">Ingresa a EmotionLab desde la aplicación para comenzar.</td>
                  </tr>
                  <tr>
                    <td style="padding:20px 32px;background-color:#f3f8fa;border-top:1px solid #d8e3ea;font-size:12px;line-height:1.6;color:#6d7f89;">Este es un mensaje automático de EmotionLab. Por favor, no respondas a este correo.</td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </body>
        </html>
      HTML
      email_subject = "Bienvenido a EmotionLab — Tu cuenta ha sido creada"
      sms_message   = "EmotionLab: Hola {username}, tu codigo temporal es {####}. Cambiala al iniciar sesion. Valido 7 dias."
    }
  }

  verification_message_template {
    email_message = <<-HTML
      <!DOCTYPE html>
      <html lang="es">
      <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Restablecimiento de contraseña</title>
      </head>
      <body style="margin:0;padding:0;background-color:#eef3f7;font-family:Arial,Helvetica,sans-serif;color:#183247;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#eef3f7;">
          <tr>
            <td align="center" style="padding:32px 16px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;background-color:#ffffff;border:1px solid #d8e3ea;border-radius:12px;overflow:hidden;">
                <tr>
                  <td style="padding:28px 32px;background-color:#123d52;color:#ffffff;">
                    <div style="font-size:24px;font-weight:bold;letter-spacing:0.4px;">EmotionLab</div>
                    <div style="margin-top:8px;font-size:12px;letter-spacing:1.6px;text-transform:uppercase;color:#9ed8d1;">Seguridad de tu cuenta</div>
                  </td>
                </tr>
                <tr>
                  <td style="padding:36px 32px 16px;">
                    <div style="font-size:14px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#148b8a;">Restablecimiento de contraseña</div>
                    <h1 style="margin:10px 0 16px;font-size:28px;line-height:1.2;color:#123d52;">Tu código de verificación</h1>
                    <p style="margin:0;font-size:16px;line-height:1.6;">Recibimos una solicitud para restablecer la contraseña de tu cuenta.</p>
                  </td>
                </tr>
                <tr>
                  <td align="center" style="padding:8px 32px 28px;">
                    <div style="font-size:12px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#587080;">Código de verificación</div>
                    <div style="display:inline-block;margin-top:12px;padding:18px 28px;background-color:#f3f8fa;border:1px solid #b8d8d7;border-radius:8px;font-family:Courier New,monospace;font-size:30px;font-weight:bold;letter-spacing:8px;color:#123d52;">{####}</div>
                    <p style="margin:20px 0 0;font-size:14px;line-height:1.6;color:#587080;">Introduce este código en EmotionLab para continuar con el restablecimiento de tu contraseña.</p>
                  </td>
                </tr>
                <tr>
                  <td style="padding:0 32px 32px;">
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#fff8e8;border-left:4px solid #e2a83b;">
                      <tr>
                        <td style="padding:16px;font-size:14px;line-height:1.6;color:#624c20;">Si no solicitaste este cambio, puedes ignorar este mensaje.</td>
                      </tr>
                    </table>
                  </td>
                </tr>
                <tr>
                  <td style="padding:20px 32px;background-color:#f3f8fa;border-top:1px solid #d8e3ea;font-size:12px;line-height:1.6;color:#6d7f89;">Este es un mensaje automático de EmotionLab. Por favor, no respondas a este correo.</td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
      </html>
    HTML
    email_subject = "EmotionLab — Código para restablecer tu contraseña"
  }

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 5
      max_length = 255
    }
  }

  schema {
    name                = "name"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 200
    }
  }

  schema {
    name                = "custom:rol"
    attribute_data_type = "String"
    required            = false
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 20
    }
  }

  schema {
    name                = "custom:legacy_uid"
    attribute_data_type = "String"
    required            = false
    mutable             = false
    string_attribute_constraints {
      min_length = 0
      max_length = 64
    }
  }

  email_configuration {
    # ============================================================
    # AWS Learner Lab: no tenemos permiso para ses:VerifyEmailIdentity
    # y por tanto no disponemos del ses_source_arn requerido para el
    # modo DEVELOPER (requiere explícitamente Source ARN).
    #
    # SOLUCIÓN: cambiar a modo COGNITO_DEFAULT.
    #   - Cognito envía correos desde su propia infraestructura.
    #   - No requiere SES Source ARN.
    #   - Limite sandbox diario es suficiente para etapa dev.
    #
    # NOTA: from_email_address / reply_to_email_address NO son
    # compatibles con COGNITO_DEFAULT (AWS retorna
    # InvalidParameterException: "From email address is not supported
    # for the COGNITO_DEFAULT email configuration"). Por ello se
    # omiten en este modo; el backend Python (SESService) en cambio
    # la identidad SES se verifique manualmente desde la consola.
    # ============================================================
    email_sending_account = "COGNITO_DEFAULT"
  }

  user_pool_add_ons {
    advanced_security_mode = "OFF"
  }

  tags = {
    Project = var.project
    Env     = var.env
  }
}

resource "aws_cognito_user_pool_client" "flask_backend" {
  name         = "${var.name_prefix}-flask-backend"
  user_pool_id = aws_cognito_user_pool.main.id
  explicit_auth_flows = [
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]
  prevent_user_existence_errors = "ENABLED"
  generate_secret               = false

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  access_token_validity  = 8
  id_token_validity      = 8
  refresh_token_validity = 7

  # ========================================================================
  # IMPORTANTE - NO declarar read_attributes ni write_attributes en este
  # App Client.
  #
  # Motivo (limitación AWS Cognito + hashicorp/aws provider bug):
  #   custom declarados con DOBLE prefijo "custom:custom:rol" y
  #   "custom:custom:legacy_uid" (provocó que al mandar CUALQUIER valor
  #   en write_attributes — incluso ["name"] — AWS rechazara con
  #   InvalidParameterException: Invalid write attributes).
  #
  # Al NO especificar read_attributes/write_attributes, el provider y
  # Cognito asignan los defaults: TODOS los atributos standard (email,
  # name, given_name, family_name, phone_number, sub, etc.) son
  # legibles/escribibles por el App Client.
  #
  # Impacto NULO para EmotionLab:
  #   El backend NO usa el App Client para reads/writes de atributos.
  #   Todas las operaciones se hacen via AdminCreateUser /
  #   AdminGetUser / AdminUpdateUserAttributes / AdminInitiateAuth
  #   (API admin con credenciales IAM). Estas API ADMIN ignoran por
  #   completo read_attributes y write_attributes del App Client.
  #   El backend sí podrá leer custom:custom:rol y custom:custom:legacy_uid
  #   directamente desde las respuestas Admin APIs.
  # ========================================================================
}

output "user_pool_id" {
  description = "ID del User Pool de Cognito"
  value       = aws_cognito_user_pool.main.id
}

output "user_pool_arn" {
  description = "ARN del User Pool de Cognito"
  value       = aws_cognito_user_pool.main.arn
}

output "app_client_id" {
  description = "ID del App Client para Flask backend"
  value       = aws_cognito_user_pool_client.flask_backend.id
}

output "app_client_secret" {
  description = "Secret del App Client (si aplica)"
  value       = aws_cognito_user_pool_client.flask_backend.client_secret
  sensitive   = true
}
