from flask import Blueprint, current_app, g, jsonify, request

from core.roles import ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

def _scope():
    user = getattr(g, "current_user", None) or {}
    claims = getattr(g, "current_claims", None) or {}
    uid = str(user.get("uid") or user.get("sub") or claims.get("sub") or "")
    return uid or "__NO_OWNER__"

@dashboard_bp.get("")
@jwt_required
@roles_required(ROLE_TEACHER)
def summary():
    service = current_app.extensions["services"]["dashboard_service"]
    return jsonify(service.get_date_overview(owner_id=_scope()))

@dashboard_bp.get("/recent")
@jwt_required
@roles_required(ROLE_TEACHER)
def recent():
    service = current_app.extensions["services"]["dashboard_service"]
    return jsonify(service.get_recent_sessions(limit=request.args.get("limit", 10, type=int), owner_id=_scope()))

@dashboard_bp.get("/chart")
@jwt_required
@roles_required(ROLE_TEACHER)
def chart():
    return jsonify(
        current_app.extensions["services"]["dashboard_service"].get_sessions_by_date(
            date_from=request.args.get("date_from"), date_to=request.args.get("date_to"), owner_id=_scope()
        )
    )
