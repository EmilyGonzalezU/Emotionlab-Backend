from flask import Blueprint, current_app, jsonify, request

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

@auth_bp.post("/login")
def login():
    body = request.get_json(silent=True) or {}
    email, password = body.get("email"), body.get("password")
    if not email or not password:
        return jsonify({"error": "email y password son obligatorios"}), 400
    result = current_app.extensions["services"]["auth_service"].login(email, password)
    return jsonify(result), 200

@auth_bp.post("/forgot-password")
def forgot_password():
    body = request.get_json(silent=True) or {}
    email = body.get("email")
    if not email:
        return jsonify({"error": "El email es obligatorio"}), 400
    result = current_app.extensions["services"]["auth_service"].forgot_password(email)
    return jsonify(result), 200

@auth_bp.post("/reset-password")
def reset_password():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    new_password = body.get("new_password")
    if not token or not new_password:
        return jsonify({"error": "Faltan datos"}), 400
    try:
        result = current_app.extensions["services"]["auth_service"].reset_password(token, new_password)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400
