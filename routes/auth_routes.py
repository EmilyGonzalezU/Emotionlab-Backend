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
