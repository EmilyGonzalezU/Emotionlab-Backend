from flask import Blueprint, current_app, jsonify, request
from uuid import uuid4

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


participants_bp = Blueprint("participants", __name__, url_prefix="/participants")


@participants_bp.get("")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def list_participants():
    service = current_app.extensions["services"]["participant_service"]
    name = request.args.get("name")
    return jsonify(service.search_by_name(name) if name else service.list_participants())


@participants_bp.get("/search")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def search_participants():
    return jsonify(
        current_app.extensions["services"]["participant_service"].search_by_name(
            request.args.get("name", "")
        )
    )


@participants_bp.post("")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def create_participant():
    body = request.get_json(silent=True) or {}
    if not body.get("nombre"):
        return jsonify({"error": "nombre es obligatorio"}), 400
    participant = current_app.extensions["services"]["participant_service"].create_participant(
        body.get("participant_id") or f"participant_{uuid4().hex}",
        body["nombre"], observaciones=body.get("observaciones")
    )
    return jsonify(participant), 201


@participants_bp.get("/<participant_id>")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def get_participant(participant_id):
    participant = current_app.extensions["services"]["participant_service"].get_participant(participant_id)
    if not participant:
        return jsonify({"error": "Participante no encontrado"}), 404
    return jsonify(participant)


@participants_bp.patch("/<participant_id>")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def update_participant(participant_id):
    body = request.get_json(silent=True) or {}
    if "observaciones" not in body:
        return jsonify({"error": "observaciones es obligatoria"}), 400
    current_app.extensions["services"]["participant_service"].update_observations(
        participant_id, body["observaciones"]
    )
    return jsonify({"participant_id": participant_id, "updated": True})
