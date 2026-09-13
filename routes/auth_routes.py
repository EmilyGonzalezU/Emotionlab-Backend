"""Rutas de autenticacion — EmotionLab.

Migracion ETAPA 2 AWS:
  - POST /login                 -> Cognito ADMIN_NO_SRP_AUTH (puede devolver NEW_PASSWORD_REQUIRED)
  - POST /confirm-new-password  -> Resuelve challenge NEW_PASSWORD_REQUIRED
  - POST /forgot-password       -> Envia codigo 6 digitos Cognito por SES
  - POST /reset-password        -> DUAL: legacy (token, new_password) O cognito (email, code, new_password)
  - POST /change-password       -> (autenticado) Cognito ChangePassword
  - GET  /me                    -> usuario actual (compatibilidad con frontends legacy)
"""


from flask import Blueprint, current_app, g, jsonify, request
from middleware.jwt_required import jwt_required


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _services():
    return current_app.extensions["services"]


def _auth_service():
    return _services()["auth_service"]


# ============================================================
# LOGIN
# ============================================================

@auth_bp.post("/login")
def login():
    body = request.get_json(silent=True) or {}
    email, password = body.get("email"), body.get("password")
    if not email or not password:
        return jsonify({"error": "email y password son obligatorios"}), 400
    try:
        result = _auth_service().login(email, password)
    except Exception:
        raise
    return jsonify(result), 200


# ============================================================
# CONFIRMAR NUEVA CONTRASEÑA (NEW_PASSWORD_REQUIRED)
# ============================================================

@auth_bp.post("/confirm-new-password")
def confirm_new_password():
    """Resuelve el challenge Cognito NEW_PASSWORD_REQUIRED del primer login."""
    body = request.get_json(silent=True) or {}
    session = body.get("session")
    user_id_for_srp = body.get("user_id_for_srp")
    new_password = body.get("new_password")
    email = body.get("email")

    if not session or not user_id_for_srp or not new_password:
        return jsonify({
            "error": "session, user_id_for_srp y new_password son obligatorios",
        }), 400

    try:
        result = _auth_service().confirm_new_password(
            session=session,
            user_id_for_srp=user_id_for_srp,
            new_password=new_password,
            email=email,
        )
        return jsonify(result), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# OLVIDÉ MI CONTRASEÑA (envío de código 6 dígitos Cognito)
# ============================================================

@auth_bp.post("/forgot-password")
def forgot_password():
    body = request.get_json(silent=True) or {}
    email = body.get("email")
    if not email:
        return jsonify({"error": "El email es obligatorio"}), 400
    try:
        result = _auth_service().forgot_password(email)
        return jsonify(result), 200
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "no encontrado" in msg or "username/client" in msg:
            return jsonify({"mensaje": "Si el email existe, se ha enviado un código de recuperación"}), 200
        return jsonify({"error": str(e)}), 500


# ============================================================
# RESTABLECER CONTRASEÑA (DUAL: legacy token / cognito code)
# ============================================================

@auth_bp.post("/reset-password")
def reset_password():
    """Restaura la contraseña.

    DUAL MODO:
      - Legacy (mientras se migra frontend antiguo):
          { "token": "<jwt legacy>", "new_password": "..." }
      - Cognito (flujo con código 6 dígitos por SES):
          { "email": "a@b.c", "code": "123456", "new_password": "..." }
    """
    body = request.get_json(silent=True) or {}
    new_password = body.get("new_password")
    if not new_password:
        return jsonify({"error": "new_password es obligatorio"}), 400

    legacy_token = body.get("token")
    cognito_email = body.get("email")
    cognito_code = body.get("code")

    try:
        if legacy_token:
            # Modo legacy (compatibilidad)
            result = _auth_service().reset_password(legacy_token, new_password)
        elif cognito_email and cognito_code:
            # Modo Cognito nuevo
            result = _auth_service().reset_password(
                email=cognito_email,
                code=cognito_code,
                new_password=new_password,
            )
        else:
            return jsonify({
                "error": "Se requiere token (legacy) o bien email + code (Cognito)",
            }), 400
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ============================================================
# CAMBIAR CONTRASEÑA (autenticado, con contraseña actual)
# ============================================================

@auth_bp.post("/change-password")
@jwt_required
def change_password():
    body = request.get_json(silent=True) or {}
    current_password = body.get("current_password")
    new_password = body.get("new_password")
    if not current_password or not new_password:
        return jsonify({
            "error": "current_password y new_password son obligatorios",
        }), 400

    try:
        authorization = request.headers.get("Authorization", "")
        _, _, access_token = authorization.partition(" ")
        result = _auth_service().change_password(
            access_token=access_token,
            old_password=current_password,
            new_password=new_password,
        )
        return jsonify(result), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# /auth/me — compatibilidad con frontends antiguos
# ============================================================

@auth_bp.get("/me")
@jwt_required
def auth_me():
    current_user = getattr(g, "current_user", None) or {}
    safe_user = dict(current_user)
    for private_field in ("password", "password_hash"):
        safe_user.pop(private_field, None)
    return jsonify(safe_user), 200
