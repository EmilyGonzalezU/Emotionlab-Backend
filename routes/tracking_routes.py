"""Rutas para el seguimiento de experiencias por session_id."""

from flask import Blueprint, current_app, g, jsonify, request
from botocore.exceptions import BotoCoreError, ClientError

from core.roles import ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


trackings_bp = Blueprint(
    "trackings",
    __name__,
    url_prefix="/trackings",
)


def _get_tracking_service():
    service = current_app.extensions["services"].get(
        "tracking_service"
    )

    if service is None:
        raise RuntimeError(
            "El seguimiento requiere almacenamiento DynamoDB."
        )

    return service


def _actor_context():
    """Obtiene el contexto del especialista autenticado para auditoría."""

    user = getattr(g, "current_user", None) or {}
    claims = getattr(g, "current_claims", None) or {}

    return {
        "actor_uid": (
            user.get("uid")
            or user.get("sub")
            or claims.get("sub")
            or ""
        ),
        "actor_email": user.get("email") or claims.get("email") or "",
        "actor_rol": user.get("rol") or "",
    }


def _tracking_public(tracking):
    """Evita exponer campos internos de auditoría en la respuesta."""

    fields = (
        "tracking_id",
        "tracking_code",
        "created_at",
        "updated_at",
    )

    return {
        field: tracking.get(field)
        for field in fields
        if tracking.get(field) is not None
    }


def _handle_service_error(message):
    current_app.logger.exception(message)

    return jsonify(
        {
            "error": message,
        }
    ), 500


# ==========================================================
# LISTAR SEGUIMIENTOS
# ==========================================================

@trackings_bp.get("")
@jwt_required
@roles_required(ROLE_TEACHER)
def list_trackings():
    try:
        trackings = _get_tracking_service().list_trackings(
            actor_ctx=_actor_context(),
        )
        return jsonify([
            _tracking_public(item)
            for item in trackings
        ]), 200

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudieron obtener los seguimientos."
        )


@trackings_bp.get("/code")
@jwt_required
@roles_required(ROLE_TEACHER)
def generate_tracking_code():
    """Devuelve una vista previa no reservada; el alta valida unicidad en DynamoDB."""
    try:
        code = _get_tracking_service().generate_tracking_code()
        return jsonify({"tracking_code": code}), 200
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503
    except (BotoCoreError, ClientError):
        current_app.logger.exception(
            "DynamoDB no pudo comprobar la unicidad del código de seguimiento."
        )
        return jsonify({
            "error": "No se pudo verificar el código porque el almacenamiento de seguimientos no está disponible. Reintenta más tarde o contacta al administrador."
        }), 503
    except Exception:
        return _handle_service_error("No se pudo generar el código del seguimiento.")


# ==========================================================
# CREAR SEGUIMIENTO CON SU PRIMERA SESIÓN
# ==========================================================

@trackings_bp.post("")
@jwt_required
@roles_required(ROLE_TEACHER)
def create_tracking():
    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return jsonify({
            "error": "Debes enviar un objeto JSON."
        }), 400

    allowed_fields = {
        "tracking_code",
        "session_id",
    }

    unexpected_fields = set(body.keys()) - allowed_fields

    if unexpected_fields:
        return jsonify({
            "error": (
                "Solo se permiten los campos "
                "tracking_code y session_id."
            )
        }), 400

    tracking_code = body.get("tracking_code")
    session_id = body.get("session_id")

    if tracking_code is not None and (not isinstance(tracking_code, str) or not tracking_code.strip()):
        return jsonify({
            "error": "tracking_code debe ser texto no vacío si se envía."
        }), 400

    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({
            "error": "session_id es obligatorio."
        }), 400

    try:
        tracking = _get_tracking_service().create_tracking(
            tracking_code=tracking_code,
            first_session_id=session_id,
            actor_ctx=_actor_context(),
        )

        return jsonify(_tracking_public(tracking)), 201

    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudo crear el seguimiento."
        )


# ==========================================================
# LISTAR SESIONES DISPONIBLES
# ==========================================================

@trackings_bp.get("/available-sessions")
@jwt_required
@roles_required(ROLE_TEACHER)
def list_available_sessions():
    try:
        sessions = _get_tracking_service().list_available_sessions(
            actor_ctx=_actor_context(),
        )

        return jsonify(sessions), 200

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudieron obtener las sesiones disponibles."
        )


# ==========================================================
# OBTENER UN SEGUIMIENTO
# ==========================================================

@trackings_bp.get("/<tracking_id>")
@jwt_required
@roles_required(ROLE_TEACHER)
def get_tracking(tracking_id):
    try:
        tracking = _get_tracking_service().get_tracking(
            tracking_id,
            actor_ctx=_actor_context(),
        )

        return jsonify(_tracking_public(tracking)), 200

    except ValueError as error:
        return jsonify({"error": str(error)}), 404

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudo obtener el seguimiento."
        )


# ==========================================================
# LISTAR SESIONES DE UN SEGUIMIENTO
# ==========================================================

@trackings_bp.get("/<tracking_id>/sessions")
@jwt_required
@roles_required(ROLE_TEACHER)
def get_tracking_sessions(tracking_id):
    try:
        sessions = _get_tracking_service().get_tracking_sessions(
            tracking_id,
            actor_ctx=_actor_context(),
        )

        return jsonify(sessions), 200

    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudieron obtener las sesiones del seguimiento."
        )


# ==========================================================
# SEGUIMIENTO POR ESTUDIANTE Y EXPERIENCIA
# ==========================================================

@trackings_bp.get("/<tracking_id>/follow-up")
@jwt_required
@roles_required(ROLE_TEACHER)
def get_follow_up(tracking_id):
    """Historial cronológico de las experiencias incorporadas."""

    try:
        registros = _get_tracking_service().get_follow_up(
            tracking_id,
            actor_ctx=_actor_context(),
        )

        return jsonify(registros), 200

    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudo obtener el seguimiento del estudiante."
        )


# ==========================================================
# EVOLUCIÓN DEL BIENESTAR
# ==========================================================

@trackings_bp.get("/<tracking_id>/evolution")
@jwt_required
@roles_required(ROLE_TEACHER)
def get_evolution(tracking_id):
    """Evolución cronológica del bienestar promedio de las sesiones."""

    try:
        evolucion = _get_tracking_service().get_evolution(
            tracking_id,
            actor_ctx=_actor_context(),
        )

        return jsonify(evolucion), 200

    except ValueError as error:
        return jsonify({"error": str(error)}), 404

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudo obtener la evolución del bienestar."
        )


# ==========================================================
# AGREGAR SESIONES A UN SEGUIMIENTO (INDIVIDUAL O POR LOTES)
# ==========================================================

@trackings_bp.post("/<tracking_id>/sessions")
@jwt_required
@roles_required(ROLE_TEACHER)
def add_tracking_session(tracking_id):
    """Asocia una o varias sesiones al seguimiento.

    Contrato soportado:

      * Individual (compatible con clientes previos):
            {"session_id": "SESSION-001"}
      * Por lotes (atómico):
            {"session_ids": ["SESSION-001", "SESSION-002"]}

    En la incorporación por lotes todas las sesiones se validan antes de
    escribir: si una falla, no se incorpora ninguna.
    """

    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return jsonify({
            "error": "Debes enviar un objeto JSON."
        }), 400

    if "session_ids" in body:
        if set(body.keys()) != {"session_ids"}:
            return jsonify({
                "error": "Solo se permite el campo session_ids."
            }), 400

        session_ids = body.get("session_ids")

        if not isinstance(session_ids, list) or not session_ids:
            return jsonify({
                "error": (
                    "session_ids debe ser una lista no vacía."
                )
            }), 400

        try:
            result = _get_tracking_service().add_sessions(
                tracking_id=tracking_id,
                session_ids=session_ids,
                actor_ctx=_actor_context(),
            )

            return jsonify(result), 200

        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        except RuntimeError as error:
            return jsonify({"error": str(error)}), 503

        except Exception:
            return _handle_service_error(
                "No se pudieron asociar las sesiones al seguimiento."
            )

    if "session_id" in body:
        if set(body.keys()) != {"session_id"}:
            return jsonify({
                "error": "Solo se permite el campo session_id."
            }), 400

        session_id = body.get("session_id")

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({
                "error": "session_id es obligatorio."
            }), 400

        try:
            result = _get_tracking_service().add_session(
                tracking_id=tracking_id,
                session_id=session_id,
                actor_ctx=_actor_context(),
            )

            return jsonify(result), 200

        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        except RuntimeError as error:
            return jsonify({"error": str(error)}), 503

        except Exception:
            return _handle_service_error(
                "No se pudo asociar la sesión al seguimiento."
            )

    return jsonify({
        "error": (
            "Debes indicar session_id o session_ids."
        )
    }), 400


# ==========================================================
# DESVINCULAR UNA SESIÓN
# ==========================================================

@trackings_bp.delete(
    "/<tracking_id>/sessions/<session_id>"
)
@jwt_required
@roles_required(ROLE_TEACHER)
def remove_tracking_session(tracking_id, session_id):
    try:
        result = _get_tracking_service().remove_session(
            tracking_id=tracking_id,
            session_id=session_id,
            actor_ctx=_actor_context(),
        )

        return jsonify(result), 200

    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudo desvincular la sesión del seguimiento."
        )


# ==========================================================
# ELIMINAR UN SEGUIMIENTO (NUNCA SUS SESIONES)
# ==========================================================

@trackings_bp.delete("/<tracking_id>")
@jwt_required
@roles_required(ROLE_TEACHER)
def delete_tracking(tracking_id):
    try:
        result = _get_tracking_service().delete_tracking(
            tracking_id=tracking_id,
            actor_ctx=_actor_context(),
        )

        return jsonify(result), 200

    except ValueError as error:
        return jsonify({"error": str(error)}), 404

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        return _handle_service_error(
            "No se pudo eliminar el seguimiento."
        )
