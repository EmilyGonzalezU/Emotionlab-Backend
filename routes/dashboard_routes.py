from flask import Blueprint, current_app, jsonify, request

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

@dashboard_bp.get("")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def summary():
    service = current_app.extensions["services"]["dashboard_service"]
    return jsonify(service.get_date_overview())

@dashboard_bp.get("/recent")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def recent():
    service = current_app.extensions["services"]["dashboard_service"]
    return jsonify(service.get_recent_sessions(limit=request.args.get("limit", 10, type=int)))

@dashboard_bp.get("/chart")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def chart():
    return jsonify(
        current_app.extensions["services"]["dashboard_service"].get_sessions_by_date(
            date_from=request.args.get("date_from"), date_to=request.args.get("date_to")
        )
    )
