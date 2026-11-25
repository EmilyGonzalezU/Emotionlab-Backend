"""Rutas del ciclo de vida de una experiencia de captura.

Flujo principal (recolección por extracción desde AWS):

    POST   /captures                          iniciar experiencia
    GET    /captures/<session_id>             consultar estado
    POST   /captures/<session_id>/finalize    registrar «Finalizar experiencia»
                                              (fija capture_ended_at -> finalizing)
    POST   /captures/<session_id>/complete    transferencia lista: extrae los JSON
                                              desde AWS, centraliza y procesa
                                              (importing -> captured)
    GET    /captures/<session_id>/records     registros originales centralizados

Compatibilidad (ya no es el mecanismo principal de recolección):

    POST   /captures/<session_id>/records     ingesta manual de un lote de registros

Durante la captura NO se descargan datos: los JSON se acumulan en AWS y la
extracción ocurre al confirmar la transferencia. El `session_id` lo genera
EmotionLab. El PIN (`display_code`) lo genera la Lambda del lente; al iniciar
la experiencia se acepta el PIN mostrado por el lente para CONFIRMARLO una
sola vez contra la fuente autorizada y vincular el `lens_session_id` original a la nueva
experiencia. EmotionLab NUNCA genera ni modifica ese código y no lo utiliza
como identidad permanente.
"""

from flask import Blueprint, current_app, g, jsonify, request

from core.roles import ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required
from services.capture_service import (
    CaptureConfirmationPending,
    CaptureNotAuthorized,
    CaptureNotFound,
    DeviceBusy,
    InvalidCapturePayload,
    InvalidDisplayCode,
    SessionClosed,
    SessionClosing,
)
from services.lens_session_service import (
    LensApiError,
    LensApiRateLimited,
    LensApiUnavailable,
    LensSessionConflict,
    LensSessionExpired,
    SessionCodeRateLimitExceeded,
    SessionCodeRateLimitUnavailable,
)


captures_bp = Blueprint(
    "captures",
    __name__,
    url_prefix="/captures",
)


def _get_capture_service():
    service = current_app.extensions["services"].get("capture_service")

    if service is None:
        raise RuntimeError(
            "El ciclo de captura requiere almacenamiento DynamoDB."
        )

    return service


def _actor_context():
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


def _error_response(error):
    """Traduce las excepciones del ciclo de captura a respuestas HTTP."""

    if isinstance(error, CaptureNotFound):
        return jsonify({"error": "EXPERIENCIA_NO_ENCONTRADA",
                        "message": str(error)}), 404

    if isinstance(error, CaptureNotAuthorized):
        return jsonify({"error": "NO_AUTORIZADO",
                        "message": str(error)}), 403

    if isinstance(error, SessionClosed):
        return jsonify({"error": "SESSION_CLOSED",
                        "message": str(error)}), 409

    if isinstance(error, SessionClosing):
        return jsonify({"error": "SESSION_CLOSING",
                        "message": str(error)}), 409

    if isinstance(error, DeviceBusy):
        return jsonify({"error": "DISPOSITIVO_OCUPADO",
                        "message": str(error)}), 409

    if isinstance(error, CaptureConfirmationPending):
        return jsonify({"error": "CONFIRMACION_INDETERMINADA",
                        "message": str(error)}), 409

    if isinstance(error, InvalidDisplayCode):
        return jsonify({"error": "PIN_INVALIDO",
                        "message": str(error)}), 400

    if isinstance(error, LensSessionExpired):
        return jsonify({"error": "PIN_EXPIRADO",
                        "message": str(error)}), 410

    if isinstance(error, LensSessionConflict):
        return jsonify({"error": "PIN_YA_CONFIRMADO",
                        "message": str(error)}), 409

    if isinstance(error, LensApiRateLimited):
        return jsonify({"error": "LENTE_LIMITADO",
                        "message": str(error)}), 429

    if isinstance(error, SessionCodeRateLimitExceeded):
        return jsonify({"error": "LIMITE_INTENTOS_PIN",
                        "message": str(error)}), 429

    if isinstance(error, SessionCodeRateLimitUnavailable):
        return jsonify({"error": "LIMITADOR_NO_DISPONIBLE",
                        "message": str(error)}), 503

    if isinstance(error, LensApiUnavailable):
        return jsonify({"error": "LENTE_NO_DISPONIBLE",
                        "message": str(error)}), 503

    if isinstance(error, LensApiError):
        return jsonify({"error": "LENTE_RESPUESTA_INVALIDA",
                        "message": str(error)}), 502

    if isinstance(error, InvalidCapturePayload):
        return jsonify({"error": "LOTE_INVALIDO",
                        "message": str(error)}), 400

    if isinstance(error, ValueError):
        return jsonify({"error": str(error)}), 400

    if isinstance(error, RuntimeError):
        return jsonify({"error": str(error)}), 503

    current_app.logger.exception("Error en el ciclo de captura.")

    return jsonify({"error": "No se pudo completar la operación."}), 500


# ==========================================================
# INICIAR EXPERIENCIA
# ==========================================================

@captures_bp.post("")
@jwt_required
@roles_required(ROLE_TEACHER)
def start_experience():
    body = request.get_json(silent=True)

    if body is None:
        body = {}

    if not isinstance(body, dict):
        return jsonify({"error": "Debes enviar un objeto JSON."}), 400

    allowed = {"request_token", "organization_id", "device_id", "display_code"}
    unknown = set(body.keys()) - allowed

    if unknown:
        return jsonify({
            "error": "Campos no permitidos: "
                     + ", ".join(sorted(unknown))
        }), 400

    try:
        result = _get_capture_service().start_experience(
            actor_ctx=_actor_context(),
            organization_id=body.get("organization_id"),
            device_id=body.get("device_id"),
            request_token=body.get("request_token"),
            display_code=body.get("display_code"),
            client_ip=request.remote_addr or "",
        )

    except Exception as error:
        return _error_response(error)

    return jsonify(result), 201


# ==========================================================
# CONSULTAR ESTADO
# ==========================================================

@captures_bp.get("/<session_id>")
@jwt_required
@roles_required(ROLE_TEACHER)
def get_experience(session_id):
    try:
        result = _get_capture_service().get_experience(
            session_id,
            actor_ctx=_actor_context(),
        )

    except Exception as error:
        return _error_response(error)

    return jsonify(result), 200


# ==========================================================
# REGISTROS ORIGINALES DE UNITY
# ==========================================================

@captures_bp.get("/<session_id>/records")
@jwt_required
@roles_required(ROLE_TEACHER)
def list_records(session_id):
    try:
        records = _get_capture_service().list_original_records(
            session_id,
            actor_ctx=_actor_context(),
        )

    except Exception as error:
        return _error_response(error)

    return jsonify({"session_id": session_id, "records": records}), 200


# ==========================================================
# RECIBIR Y CENTRALIZAR REGISTROS
# ==========================================================

@captures_bp.post("/<session_id>/records")
@jwt_required
@roles_required(ROLE_TEACHER)
def ingest_records(session_id):
    payload = request.get_json(silent=True)

    if payload is None:
        return jsonify({
            "error": "El cuerpo debe contener los registros."
        }), 400

    try:
        result = _get_capture_service().ingest_records(
            session_id,
            payload,
            actor_ctx=_actor_context(),
        )

    except Exception as error:
        return _error_response(error)

    return jsonify(result), 202


# ==========================================================
# FINALIZAR EXPERIENCIA
# ==========================================================

@captures_bp.post("/<session_id>/finalize")
@jwt_required
@roles_required(ROLE_TEACHER)
def finalize_experience(session_id):
    try:
        result = _get_capture_service().finalize_experience(
            session_id,
            actor_ctx=_actor_context(),
        )

    except Exception as error:
        return _error_response(error)

    return jsonify(result), 200


# ==========================================================
# CONFIRMAR CIERRE (DISPOSITIVO)
# ==========================================================

@captures_bp.post("/<session_id>/complete")
@jwt_required
@roles_required(ROLE_TEACHER)
def complete_experience(session_id):
    try:
        result = _get_capture_service().complete_experience(
            session_id,
            actor_ctx=_actor_context(),
        )

    except Exception as error:
        return _error_response(error)

    return jsonify(result), 200


# ==========================================================
# REPROCESAMIENTO CONTROLADO DE IA (MODELO V2)
# ==========================================================

@captures_bp.post("/<session_id>/analyze")
@jwt_required
@roles_required(ROLE_TEACHER)
def analyze_experience(session_id):
    """Recalcula el análisis del modelo V2 sobre la MISMA experiencia.

    No crea una sesión nueva ni duplica el JSON original de Unity. Con
    ``force`` se reconoce un reprocesamiento explícito; sin él, si la
    versión de modelo y las entradas coinciden, se reutiliza el resultado.
    """

    body = request.get_json(silent=True)

    if body is None:
        body = {}

    if not isinstance(body, dict):
        return jsonify({"error": "Debes enviar un objeto JSON."}), 400

    unknown = set(body.keys()) - {"force"}

    if unknown:
        return jsonify({
            "error": "Campos no permitidos: "
                     + ", ".join(sorted(unknown))
        }), 400

    try:
        result = _get_capture_service().analyze_experience(
            session_id,
            actor_ctx=_actor_context(),
            force=bool(body.get("force")),
        )

    except Exception as error:
        return _error_response(error)

    return jsonify(result), 200
