from flask import Blueprint, current_app, jsonify, request

from core.roles import ROLE_ADMIN
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required
from services.import_service import public_import_summary


imports_bp = Blueprint("imports", __name__)


@imports_bp.post("/sessions")
@jwt_required
@roles_required(ROLE_ADMIN)
def import_sessions():
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "El cuerpo debe contener un lote JSON"}), 400
    result = current_app.extensions["services"]["import_service"].import_batch(payload)
    return jsonify(public_import_summary(result)), 201


@imports_bp.post("/sync")
@jwt_required
@roles_required(ROLE_ADMIN)
def sync_aws_sessions():
    services = current_app.extensions["services"]
    result = services["import_service"].import_from_aws(services["aws_service"])
    return jsonify(public_import_summary(result)), 201
