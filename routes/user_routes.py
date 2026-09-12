"""Rutas de usuarios — EmotionLab (ETAPA 2 AWS).

Cambios clave respecto a legacy:
    - POST /users NO RECIBE password en el body.
        Cognito genera la contraseña temporal y envía la invitación.
  - /users/me/password -> delega a Cognito ChangePassword (no hay password_hash local).
"""

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    request,
)

from core.exceptions import AuthenticationError, UserNotFound
from core.roles import ROLE_ADMIN
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


users_bp = Blueprint(
    "users",
    __name__,
    url_prefix="/users",
)


def _current_actor() -> tuple[str, dict]:
    """Retorna (actor_uid, actor_info) desde g.current_user inyectado por jwt_required."""
    current_user = getattr(g, "current_user", {}) or {}
    actor_uid = current_user.get("uid") or current_user.get("id")
    if not actor_uid:
        raise AuthenticationError(
            "El usuario autenticado no tiene un identificador valido"
        )
    return actor_uid, dict(current_user)


def _services():
    return current_app.extensions["services"]


# ============================================================
# LISTAR USUARIOS
# ============================================================

@users_bp.get("")
@jwt_required
@roles_required(ROLE_ADMIN)
def list_users():
    return jsonify(
        _services()["user_service"].list_public_users()
    )


# ============================================================
# CREAR USUARIO — ETAPA 2 (SIN password en body)
# ============================================================

@users_bp.post("")
@jwt_required
@roles_required(ROLE_ADMIN)
def create_user():
    """Crea un especialista/administrador.

    Body:
        nombre (str): Nombre completo.
        email (str):  Correo único.
        rol (str):    ROLE_ADMIN o ROLE_TEACHER.

    NO SE ESPERA password: Cognito genera una temporal y envía la invitación.
    """
    body = request.get_json(silent=True) or {}

    required = ("nombre", "email", "rol")
    if any(not body.get(field) for field in required):
        return jsonify({
            "error": "nombre, email y rol son obligatorios (password NO se recibe aquí; se genera temporal en Cognito)"
        }), 400

    services = _services()
    user_service = services["user_service"]
    actor_uid, actor_info = _current_actor()

    # Cognito genera la contraseña temporal y envía la invitación por email.
    user = user_service.create_user(
        "",
        body["nombre"],
        body["email"],
        body["rol"],
    )
    target_uid = user.get("uid")

    user_service.log_activity(
        actor_uid,
        "Creación de usuario (Cognito + DynamoDB)",
        target_uid=target_uid,
        actor_info=actor_info,
        extra={"creado_en_cognito": True, "invitacion_enviada_por_cognito": True},
    )

    return jsonify(user_service.public_user(user)), 201


# ============================================================
# ACTUALIZAR USUARIO
# ============================================================

@users_bp.put("/<uid>")
@jwt_required
@roles_required(ROLE_ADMIN)
def update_user(uid):
    body = request.get_json(silent=True) or {}

    allowed_fields = {"nombre", "email", "rol", "activo"}
    body = {
        key: value
        for key, value in body.items()
        if key in allowed_fields
    }

    if not body:
        return jsonify({
            "error": "No hay campos válidos para actualizar"
        }), 400

    services = _services()
    user_service = services["user_service"]
    actor_uid, actor_info = _current_actor()

    target = user_service.get_user(uid)
    if target is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    user_service.update_user(uid, body)

    user_service.log_activity(
        actor_uid,
        "Actualización de usuario",
        target_uid=uid,
        details={"campos": sorted(body.keys())},
        actor_info=actor_info,
    )

    return jsonify({"message": "Usuario actualizado correctamente"}), 200


# ============================================================
# DESHABILITAR / HABILITAR USUARIO
# ============================================================

@users_bp.patch("/<uid>/disable")
@jwt_required
@roles_required(ROLE_ADMIN)
def disable_user(uid):
    user_service = _services()["user_service"]
    target = user_service.get_user(uid)
    if target is None:
        return jsonify({"error": "Usuario no encontrado"}), 404
    user_service.disable_user(uid)
    return jsonify({"message": "Usuario deshabilitado correctamente"}), 200


@users_bp.patch("/<uid>/enable")
@jwt_required
@roles_required(ROLE_ADMIN)
def enable_user(uid):
    user_service = _services()["user_service"]
    target = user_service.get_user(uid)
    if target is None:
        return jsonify({"error": "Usuario no encontrado"}), 404
    user_service.enable_user(uid)
    return jsonify({"message": "Usuario habilitado correctamente"}), 200


# ============================================================
# ELIMINAR USUARIO
# ============================================================

@users_bp.delete("/<uid>")
@jwt_required
@roles_required(ROLE_ADMIN)
def delete_user(uid):
    services = _services()
    user_service = services["user_service"]
    actor_uid, actor_info = _current_actor()

    target = user_service.get_user(uid)
    if target is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    user_service.delete_user(uid)

    user_service.log_activity(
        actor_uid,
        "Cuenta eliminada",
        target_uid=uid,
        actor_info=actor_info,
    )

    return jsonify({"message": "Usuario eliminado correctamente"}), 200


# ============================================================
# CAMBIAR MI CONTRASEÑA — ETAPA 2 (Cognito)
# ============================================================

@users_bp.patch("/me/password")
@jwt_required
def change_password():
    """Cambio de contraseña estando autenticado.

    Delegado 100% a Cognito (no existe password_hash en DynamoDB).
    Body: { current_password, new_password }
    """
    body = request.get_json(silent=True) or {}

    if not body.get("current_password") or not body.get("new_password"):
        return jsonify({
            "error": "current_password y new_password son obligatorios"
        }), 400

    if len(body["new_password"]) < 8:
        return jsonify({
            "error": "La nueva contraseña debe tener al menos 8 caracteres"
        }), 400

    auth_service = _services()["auth_service"]
    authorization = request.headers.get("Authorization", "")
    _, _, access_token = authorization.partition(" ")

    try:
        auth_service.change_password(
            access_token=access_token,
            old_password=body["current_password"],
            new_password=body["new_password"],
        )
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        return jsonify({"error": f"Error al cambiar la contraseña: {exc}"}), 500

    return jsonify({"message": "Contraseña actualizada correctamente"}), 200


# ============================================================
# USUARIO ACTUAL (lectura ya hecha por jwt_required en g.current_user)
# ============================================================

@users_bp.get("/me")
@jwt_required
def get_current_user():
    current_user = getattr(g, "current_user", None) or {}
    safe_user = dict(current_user)
    for private_field in ("password", "password_hash"):
        safe_user.pop(private_field, None)
    return jsonify(safe_user), 200


# ============================================================
# ACTIVIDAD ADMINISTRATIVA
# ============================================================

@users_bp.get("/activity")
@jwt_required
@roles_required(ROLE_ADMIN)
def list_activity():
    service = _services()["user_service"]
    return jsonify(service.list_activity(limit=8)), 200


@users_bp.get("/<uid>/activity")
@jwt_required
@roles_required(ROLE_ADMIN)
def list_user_activity(uid):
    service = _services()["user_service"]
    target = service.get_user(uid)
    if target is None:
        return jsonify({"error": "Usuario no encontrado"}), 404
    return jsonify(service.list_activity(uid)), 200
