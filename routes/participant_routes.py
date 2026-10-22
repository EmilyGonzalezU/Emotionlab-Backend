"""Rutas para la administración de participantes."""

from flask import Blueprint, current_app, jsonify, request

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


participants_bp = Blueprint(
    "participants",
    __name__,
    url_prefix="/participants",
)


def _get_service():
    """Obtiene el ParticipantService registrado en Flask."""
    return current_app.extensions["services"]["participant_service"]


# ==========================================================
# LISTAR PARTICIPANTES
# ==========================================================

@participants_bp.get("")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def list_participants():
    """
    Lista todos los participantes.

    También permite buscar mediante:

        GET /participants?name=juan
    """

    name = request.args.get("name", "").strip()

    service = _get_service()

    if name:
        participants = service.search_by_name(name)
    else:
        participants = service.list_participants()

    return jsonify(participants), 200


# ==========================================================
# BUSCAR PARTICIPANTES
# ==========================================================

@participants_bp.get("/search")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def search_participants():
    """
    Busca participantes por coincidencia parcial de nombre.

    Ejemplo:

        GET /participants/search?name=juan
    """

    name = request.args.get("name", "").strip()

    if not name:
        return jsonify([]), 200

    participants = _get_service().search_by_name(name)

    return jsonify(participants), 200


# ==========================================================
# CREAR / SELECCIONAR PARTICIPANTE
# ==========================================================

@participants_bp.post("")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def create_participant():
    """
    Selecciona un participante existente mediante participant_id o crea uno
    nuevo. Un nombre coincidente no selecciona automáticamente una identidad.

    Para crear, el participant_id no debe ser enviado por el frontend. Para
    seleccionar un candidato ya existente, debe enviarse explícitamente.

    Ejemplo:

        POST /participants

        {
            "nombre": "Juan Pérez González Ulloa"
        }

    Si existe:

        {"participant_id": "JPGU482731", "nombre": "Juan Pablo González Ulloa"}

    Sin participant_id, ParticipantService genera un identificador nuevo.
    """

    body = request.get_json(silent=True) or {}

    nombre = body.get("nombre")
    if not isinstance(nombre, str) or not nombre.strip():
        return jsonify(
            {
                "error": "nombre es obligatorio"
            }
        ), 400

    participant_id = body.get("participant_id")

    if participant_id is not None and (
        not isinstance(participant_id, str) or not participant_id.strip()
    ):
        return jsonify(
            {
                "error": "participant_id debe ser texto no vacio"
            }
        ), 400

    observaciones = body.get("observaciones")

    if observaciones is not None and not isinstance(
        observaciones,
        str
    ):
        return jsonify(
            {
                "error": "observaciones debe ser texto o null"
            }
        ), 400

    try:
        participant = _get_service().find_or_create(
            nombre=nombre,
            participant_id=participant_id,
            observaciones=observaciones,
        )

        return jsonify(participant), 200

    except ValueError as error:
        return jsonify(
            {
                "error": str(error)
            }
        ), 400

    except Exception:
        current_app.logger.exception(
            "Error creando/encontrando participante"
        )

        return jsonify(
            {
                "error": "No se pudo crear o encontrar el participante"
            }
        ), 500


# ==========================================================
# OBTENER PARTICIPANTE
# ==========================================================

@participants_bp.get("/<participant_id>")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def get_participant(participant_id):
    """
    Obtiene un participante mediante su ID anonimizado.
    """

    participant_id = participant_id.strip().upper()

    if not participant_id:
        return jsonify(
            {
                "error": "participant_id es obligatorio"
            }
        ), 400

    participant = _get_service().get_participant(
        participant_id
    )

    if not participant:
        return jsonify(
            {
                "error": "Participante no encontrado"
            }
        ), 404

    return jsonify(participant), 200


# ==========================================================
# ACTUALIZAR PARTICIPANTE
# ==========================================================

@participants_bp.patch("/<participant_id>")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def update_participant(participant_id):
    """
    Actualiza las observaciones de un participante.
    """

    participant_id = participant_id.strip().upper()

    body = request.get_json(silent=True) or {}

    if "observaciones" not in body:
        return jsonify(
            {
                "error": "observaciones es obligatoria"
            }
        ), 400

    try:
        _get_service().update_observations(
            participant_id,
            body["observaciones"],
        )

        return jsonify(
            {
                "participant_id": participant_id,
                "updated": True,
            }
        ), 200

    except ValueError as error:
        return jsonify(
            {
                "error": str(error)
            }
        ), 400

    except Exception:
        current_app.logger.exception(
            "Error actualizando participante"
        )

        return jsonify(
            {
                "error": "No se pudo actualizar el participante"
            }
        ), 500