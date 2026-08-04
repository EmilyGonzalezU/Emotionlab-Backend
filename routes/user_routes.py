from flask import Blueprint, current_app, jsonify, request

from core.roles import ROLE_ADMIN
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


users_bp = Blueprint("users", __name__, url_prefix="/users")


@users_bp.get("")
@jwt_required
@roles_required(ROLE_ADMIN)
def list_users():
    return jsonify(current_app.extensions["services"]["user_service"].list_users())


@users_bp.post("")
@jwt_required
@roles_required(ROLE_ADMIN)
def create_user():
    body = request.get_json(silent=True) or {}
    required = ("uid", "nombre", "email", "rol", "password")
    if any(not body.get(field) for field in required):
        return jsonify({"error": "uid, nombre, email, rol y password son obligatorios"}), 400
    crypto = current_app.extensions["services"]["crypto_service"]
    user = current_app.extensions["services"]["user_service"].create_user(
        body["uid"], body["nombre"], body["email"], body["rol"], crypto.hash_password(body["password"])
    )
    user.pop("password_hash", None)
    return jsonify(user), 201

@users_bp.put("/<uid>")
@jwt_required
@roles_required(ROLE_ADMIN)
def update_user(uid):
    body = request.get_json(silent=True) or {}

    current_app.extensions["services"]["user_service"].update_user(uid, body)

    return jsonify({"message": "Usuario actualizado correctamente"}), 200

@users_bp.patch("/<uid>/disable")
@jwt_required
@roles_required(ROLE_ADMIN)
def disable_user(uid):
    current_app.extensions["services"]["user_service"].disable_user(uid)

    return jsonify({"message": "Usuario deshabilitado correctamente"}), 200

@users_bp.patch("/<uid>/enable")
@jwt_required
@roles_required(ROLE_ADMIN)
def enable_user(uid):
    current_app.extensions["services"]["user_service"].enable_user(uid)

    return jsonify({"message": "Usuario habilitado correctamente"}), 200

@users_bp.delete("/<uid>")
@jwt_required
@roles_required(ROLE_ADMIN)
def delete_user(uid):
    current_app.extensions["services"]["user_service"].delete_user(uid)

    return jsonify({"message": "Usuario eliminado correctamente"}), 200