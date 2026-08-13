from flask import Blueprint, current_app, jsonify, request

from core.exceptions import SessionNotFound
from middleware.jwt_required import jwt_required

sessions_bp = Blueprint("sessions", __name__, url_prefix="/sessions")


@sessions_bp.get("")
@jwt_required
def list_sessions():
    """Lista las sesiones almacenadas con filtros opcionales por fecha y estado."""
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    status = request.args.get("status")
    session_service = current_app.extensions["services"]["session_service"]
    return jsonify(
        session_service.list_sessions(
            date_from=date_from,
            date_to=date_to,
            status=status,
        )
    ), 200


@sessions_bp.get("/dates")
@jwt_required
def list_dates():
    """Obtiene los días que tienen sesiones registradas."""
    session_service = current_app.extensions["services"]["session_service"]
    return jsonify(session_service.list_dates()), 200


@sessions_bp.get("/by-date/<fecha>")
@jwt_required
def list_by_date(fecha: str):
    """Obtiene las sesiones asociadas a un día en concreto."""
    session_service = current_app.extensions["services"]["session_service"]
    return jsonify(session_service.list_sessions_by_date(fecha)), 200


@sessions_bp.get("/<session_id>")
@jwt_required
def get_session(session_id: str):
    """Obtiene el detalle completo de una sesión por su ID."""
    session_service = current_app.extensions["services"]["session_service"]
    session = session_service.get_session_detail(session_id)
    if session is None:
        raise SessionNotFound("La sesión no existe")
    return jsonify(session), 200


@sessions_bp.patch("/<session_id>")
@jwt_required
def update_session(session_id: str):
    """Actualiza datos manuales permitidos de la sesión (nombre, observaciones)."""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "El cuerpo debe ser un JSON válido"}), 400
    session_service = current_app.extensions["services"]["session_service"]
    session_service.update_session(session_id, data)
    return jsonify({"message": "Sesión actualizada correctamente"}), 200


@sessions_bp.route("/import", methods=["POST"])
@jwt_required
def import_sessions_direct():
    """Endpoint llamado por el frontend React al presionar 'Importar nuevas sesiones'."""
    try:
        services = current_app.extensions["services"]
        import_service = services["import_service"]
        aws_service = services.get("aws_service")

        # 1. Si AWS está configurado y funcional, intenta la sincronización remota
        if aws_service is not None:
            result = import_service.import_from_aws(aws_service)
            return jsonify(result), 200

        # 2. Modo Solo-Firebase: sincroniza índices sin arrojar error
        import_service.session_service.build_import_index()
        return jsonify({
            "nuevas": 0,
            "imported": 0,
            "skipped_existing": [],
            "mensaje": "Base de datos sincronizada correctamente con Firebase."
        }), 200

    except Exception as error:
        print(f"❌ Error en la importación directa de sesiones: {error}")
        return jsonify({"error": "No se pudieron importar las sesiones.", "detalles": str(error)}), 500