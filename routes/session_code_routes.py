"""Rutas para identificar la experiencia del lente mediante su displayCode."""

from flask import Blueprint, current_app, g, jsonify, request

from core.roles import ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required
from services.lens_session_service import (
    LensApiError,
    LensApiRateLimited,
    LensApiUnavailable,
    LensSessionConflict,
    LensSessionExpired,
    LensSessionNotFound,
    SessionCodeRateLimitExceeded,
    SessionCodeRateLimitUnavailable,
)


session_codes_bp = Blueprint(
    "session_codes",
    __name__,
    url_prefix="/sessions",
)


def _get_lens_session_service():
    service = current_app.extensions["services"].get(
        "lens_session_service"
    )

    if service is None:
        raise RuntimeError(
            "La identificación de sesiones del lente requiere "
            "almacenamiento DynamoDB."
        )

    return service


def _actor_context():
    """Contexto del especialista autenticado para auditoría."""

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


def _client_ip():
    """
    Origen real de la petición.

    API Gateway entrega su IP de origen en `requestContext.http.sourceIp`
    y Mangum/a2wsgi la exponen como REMOTE_ADDR. No se usa
    `X-Forwarded-For` porque el cliente podría falsificarla.
    """

    return request.remote_addr or ""


# ==========================================================
# IDENTIFICAR EXPERIENCIA DEL LENTE
# ==========================================================

@session_codes_bp.post("/resolve-display-code")
@jwt_required
@roles_required(ROLE_TEACHER)
def resolve_display_code():
    """
    Traduce el código mostrado por el lente en el `sessionId` original de
    la experiencia creada por `createSession`.

    Respuesta 200 (contrato explícito):

        session_id     -> `sessionId` original del lente. Es el identificador
                          único de toda la experiencia y de sus datos
                          (importación, procesamiento, almacenamiento y
                          consulta del detalle).
        display_code   -> código de 6 dígitos validado (solo conexión inicial).
        capture_status -> estado del lente para la experiencia; no es un estado
                          interno de EmotionLab.
        created_at     -> creación de la experiencia según el lente.
        participant_id -> `idParticipante` del lente, si corresponde.

    La validación NO consume el PIN: se consulta el API Gateway de los lentes
    en modo `validateOnly`, sin modificar la sesión original. La confirmación
    real ocurre al iniciar la experiencia (`POST /captures`).

    No se emite `processed_session_id` ni `tracking_id`: el procesamiento
    **enriquece** la misma sesión conservando el `sessionId`, y la asociación
    a un seguimiento es una etapa independiente que esta ruta no habilita.
    Nunca se expone `sessionToken`.
    """
    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return jsonify({
            "error": "Debes enviar un objeto JSON."
        }), 400

    if set(body.keys()) != {"display_code"}:
        return jsonify({
            "error": "Solo se permite el campo display_code."
        }), 400

    display_code = body.get("display_code")

    if not isinstance(display_code, str) or not display_code.strip():
        return jsonify({
            "error": "display_code es obligatorio."
        }), 400

    try:
        session = (
            _get_lens_session_service()
            .resolve_by_display_code(
                display_code,
                client_ip=_client_ip(),
                actor_ctx=_actor_context(),
            )
        )

    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    except SessionCodeRateLimitExceeded:
        return jsonify({
            "error": (
                "Demasiados intentos de validación. "
                "Espera unos minutos e inténtalo de nuevo."
            )
        }), 429

    except SessionCodeRateLimitUnavailable:
        return jsonify({
            "error": "El limitador de intentos no está disponible."
        }), 503

    except LensSessionNotFound:
        return jsonify({
            "error": "El código no corresponde a una sesión vigente."
        }), 404

    except LensSessionExpired:
        return jsonify({
            "error": "El código del lente expiró."
        }), 410

    except LensSessionConflict:
        return jsonify({
            "error": "El código ya fue confirmado para otra captura."
        }), 409

    except LensApiRateLimited:
        return jsonify({
            "error": (
                "El servicio del lente limitó los intentos. "
                "Espera unos minutos e inténtalo de nuevo."
            )
        }), 429

    except LensApiUnavailable:
        return jsonify({
            "error": "El servicio del lente no está disponible."
        }), 503

    except LensApiError:
        return jsonify({
            "error": "El servicio del lente devolvió una respuesta inválida."
        }), 502

    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    except Exception:
        current_app.logger.exception(
            "No se pudo resolver el código de sesión."
        )
        return jsonify({
            "error": "No se pudo resolver el código de sesión."
        }), 500

    # La respuesta entrega el `sessionId` original de la experiencia, que es
    # el mismo identificador que usa el resto del backend (importación,
    # procesamiento, detalle y seguimiento). No se emite ningún
    # identificador de "sesión procesada" ni un `tracking_id` artificial: el
    # procesamiento enriquece la misma sesión y la asociación a un
    # seguimiento es una etapa independiente que esta ruta no habilita.
    return jsonify(session), 200
