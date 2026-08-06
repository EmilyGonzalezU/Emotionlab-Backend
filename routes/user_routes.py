from uuid import uuid4

from flask import Blueprint, current_app, g, jsonify, request

from core.exceptions import AuthenticationError
from core.roles import ROLE_ADMIN
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


users_bp = Blueprint("users", __name__, url_prefix="/users")


def _current_actor_uid() -> str:
    current_user = getattr(g, "current_user", {})
    actor_uid = current_user.get("uid") or current_user.get("id")
    if not actor_uid:
        raise AuthenticationError("El usuario autenticado no tiene un identificador valido")
    return actor_uid


@users_bp.get("")
@jwt_required
@roles_required(ROLE_ADMIN)
def list_users():
    return jsonify(current_app.extensions["services"]["user_service"].list_public_users())


@users_bp.post("")
@jwt_required
@roles_required(ROLE_ADMIN)
def create_user():
    body = request.get_json(silent=True) or {}
    required = ("nombre", "email", "rol", "password")
    if any(not body.get(field) for field in required):
        return jsonify({"error": "nombre, email, rol y password son obligatorios"}), 400

    uid = str(uuid4())
    crypto = current_app.extensions["services"]["crypto_service"]
    user = current_app.extensions["services"]["user_service"].create_user(
        uid,
        body["nombre"],
        body["email"],
        body["rol"],
        crypto.hash_password(body["password"]),
    )
    current_app.extensions["services"]["user_service"].log_activity(
        _current_actor_uid(),
        "Creación de usuario",
        target_uid=uid,
    )
    return jsonify(current_app.extensions["services"]["user_service"].public_user(user)), 201

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
        return jsonify({"error": "No hay campos válidos para actualizar"}), 400

    current_app.extensions["services"]["user_service"].update_user(uid, body)
    current_app.extensions["services"]["user_service"].log_activity(
        _current_actor_uid(),
        "Actualización de usuario",
        target_uid=uid,
        details={"campos": sorted(body.keys())},
    )

    return jsonify({"message": "Usuario actualizado correctamente"}), 200

@users_bp.patch("/<uid>/disable")
@jwt_required
@roles_required(ROLE_ADMIN)
def disable_user(uid):
    service = current_app.extensions["services"]["user_service"]

    if not service.exists(uid):
        return jsonify({"error": "Usuario no encontrado"}), 404

    service.disable_user(uid)
    service.log_activity(_current_actor_uid(), "Cuenta deshabilitada", target_uid=uid)

    return jsonify({"message": "Usuario deshabilitado correctamente"}), 200

@users_bp.patch("/<uid>/enable")
@jwt_required
@roles_required(ROLE_ADMIN)
def enable_user(uid):
    service = current_app.extensions["services"]["user_service"]

    if not service.exists(uid):
        return jsonify({"error": "Usuario no encontrado"}), 404

    service.enable_user(uid)
    service.log_activity(_current_actor_uid(), "Cuenta habilitada", target_uid=uid)

    return jsonify({"message": "Usuario habilitado correctamente"}), 200

@users_bp.delete("/<uid>")
@jwt_required
@roles_required(ROLE_ADMIN)
def delete_user(uid):
    service = current_app.extensions["services"]["user_service"]

    if not service.exists(uid):
        return jsonify({"error": "Usuario no encontrado"}), 404

    service.delete_user(uid)
    service.log_activity(_current_actor_uid(), "Cuenta eliminada", target_uid=uid)

    return jsonify({"message": "Usuario eliminado correctamente"}), 200

"""Ruta para cambiar contraseña de un usuario autenticado."""
@users_bp.patch("/me/password")
@jwt_required
def change_password():
    body = request.get_json(silent=True) or {}

    if not body.get("current_password") or not body.get("new_password"):
        return jsonify({
            "error": "current_password y new_password son obligatorios"
        }), 400

    auth_service = current_app.extensions["services"]["auth_service"]
    crypto_service = current_app.extensions["services"]["crypto_service"]
    user_service = current_app.extensions["services"]["user_service"]

    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    user = auth_service.get_current_user(token)

    db_user = user_service.get_user(user["uid"])

    if db_user is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    if not crypto_service.verify_password(
        body["current_password"],
        db_user["password_hash"],
    ):
        return jsonify({"error": "La contraseña actual es incorrecta"}), 400
    user_service.update_password(
        user["uid"],
        crypto_service.hash_password(body["new_password"]),
    )
    user_service.log_activity(user["uid"], "Cambio de contraseña", target_uid=user["uid"])

    return jsonify({"message": "Contraseña actualizada correctamente"}), 200

@users_bp.get("/me")
@jwt_required
def get_current_user():
    auth_service = current_app.extensions["services"]["auth_service"]

    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    user = auth_service.get_current_user(token)

    return jsonify(user), 200


@users_bp.get("/activity")
@jwt_required
@roles_required(ROLE_ADMIN)
def list_activity():
    service = current_app.extensions["services"]["user_service"]
    return jsonify(service.list_activity(limit=8)), 200


@users_bp.get("/<uid>/activity")
@jwt_required
@roles_required(ROLE_ADMIN)
def list_user_activity(uid):
    service = current_app.extensions["services"]["user_service"]

    if not service.exists(uid):
        return jsonify({"error": "Usuario no encontrado"}), 404

    return jsonify(service.list_activity(uid)), 200