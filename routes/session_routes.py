from flask import Blueprint, current_app, jsonify, request

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


sessions_bp = Blueprint("sessions", __name__, url_prefix="/sessions")


@sessions_bp.get("")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def list_sessions():
    service = current_app.extensions["services"]["session_service"]
    name = request.args.get("name")
    if name is not None:
        return jsonify([_session_summary(item) for item in service.search_by_name(name)])
    fecha = request.args.get("date")
    sessions = service.list_sessions_by_date(fecha) if fecha else service.list_sessions(
        date_from=request.args.get("date_from"), date_to=request.args.get("date_to"),
        status=request.args.get("status"),
    )
    return jsonify([_session_summary(session) for session in sessions])


@sessions_bp.get("/<session_id>")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def get_session(session_id):
    session = current_app.extensions["services"]["session_service"].get_session_detail(session_id)
    if not session:
        return jsonify({"error": "Sesion no encontrada"}), 404
    return jsonify({
        "session_id": session.get("session_id"),
        "nombre": session.get("nombre"),
        "observaciones": session.get("observaciones"),
        "estado": session.get("estado", session.get("status")),
        "status": session.get("status"),
        "prediction": session.get("prediction"),
        "fecha": session.get("fecha"),
        "hora": session.get("hora"),
        "edad": session.get("edad"),
        "genero": session.get("genero"),
        "difficulty": session.get("difficulty"),
        "raw_data": session.get("raw_json"),
    })


@sessions_bp.patch("/<session_id>/status")
@jwt_required
@roles_required(ROLE_ADMIN)
def update_status(session_id):
    status = (request.get_json(silent=True) or {}).get("status")
    if not status:
        return jsonify({"error": "status es obligatorio"}), 400
    current_app.extensions["services"]["session_service"].update_status(session_id, status)
    return jsonify({"session_id": session_id, "status": status})


@sessions_bp.patch("/<session_id>/prediction")
@jwt_required
@roles_required(ROLE_ADMIN)
def update_prediction(session_id):
    prediction = (request.get_json(silent=True) or {}).get("prediction")
    if not prediction:
        return jsonify({"error": "prediction es obligatoria"}), 400
    current_app.extensions["services"]["session_service"].update_prediction(session_id, prediction)
    return jsonify({"session_id": session_id, "updated": True})


@sessions_bp.patch("/<session_id>")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def update_session(session_id):
    body = request.get_json(silent=True) or {}
    current_app.extensions["services"]["session_service"].update_session(session_id, body)
    return jsonify({"session_id": session_id, "updated": True})


def _session_summary(session):
    prediction = session.get("prediction") or {}
    return {
        "session_id": session.get("session_id"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "hora": session.get("hora"),
        "edad": session.get("edad"),
        "genero": session.get("genero"),
        "bienestar": prediction.get("bienestar"),
        "nombre": session.get("nombre"),
        "estado": session.get("estado", session.get("status")),
    }
