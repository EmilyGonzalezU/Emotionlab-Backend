"""Rutas para solicitudes de informes PDF — EmotionLab ETAPA 2 AWS.

Flujo completo:
  1. POST   /report-requests                 -> Especialista crea solicitud PENDING
  2. GET    /report-requests?status=&mine=   -> Listar (admin todas / profesor solo suyas)
  3. GET    /report-requests/<id>            -> Detalle
  4. PATCH  /report-requests/<id>/review     -> Admin aprueba/rechaza
  5. GET    /report-requests/<id>/download   -> Presigned URL S3 (solo si aprobada)

Todas las operaciones quedan registradas en auditoría automáticamente por
ReportRequestService.
"""

import os
from datetime import datetime, timezone

from flask import Blueprint, current_app, g, jsonify, request

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


report_requests_bp = Blueprint(
    "report_requests",
    __name__,
    url_prefix="/report-requests",
)


def _svc():
    return current_app.extensions["services"]["report_request_service"]


def _current_actor() -> tuple[str, str, str]:
    u = getattr(g, "current_user", {}) or {}
    uid = u.get("uid") or u.get("id") or ""
    email = u.get("email") or u.get("correo") or ""
    rol = u.get("rol") or ""
    if not uid or not email:
        from core.exceptions import AuthenticationError
        raise AuthenticationError("Usuario autenticado sin uid/email válidos")
    return uid, email, rol


_ADMIN_REQUEST_FIELDS = (
    "request_id",
    "report_request_id",
    "report_type",
    "report_format",
    "session_id",
    "tracking_id",
    "tracking_code",
    "session_count",
    "requester_uid",
    "motivo",
    "status",
    "created_at",
    "updated_at",
    "reviewer_uid",
    "reviewer_rol",
    "reviewed_at",
    "rejection_reason",
    "review_comments",
)


def _admin_request_public(item):
    """Expose only the administrative metadata approved for report review."""
    return {
        field: item[field]
        for field in _ADMIN_REQUEST_FIELDS
        if item.get(field) is not None
    }


# ============================================================
# 1. CREAR SOLICITUD DE INFORME (especialista)
# ============================================================

@report_requests_bp.post("")
@jwt_required
@roles_required(ROLE_TEACHER)
def create_request():
    """Crea una nueva solicitud PENDING para un session_id dado.

    Body:
        session_id (str): Obligatorio.
        motivo (str):     Opcional.
    """
    actor_uid, actor_email, actor_rol = _current_actor()
    body = request.get_json(silent=True) or {}
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id es obligatorio"}), 400
    motivo = body.get("motivo") or None

    try:
        req = _svc().create_request(
            session_id,
            requester_uid=actor_uid,
            requester_email=actor_email,
            requester_rol=actor_rol,
            motivo=motivo,
        )
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except PermissionError:
        # Same response for missing and foreign sessions; do not expose an
        # existence oracle for session_id values.
        return jsonify({"error": "Sesión no disponible para solicitar informe."}), 404
    except Exception as exc:
        current_app.logger.error("Error creando solicitud de informe (tipo=%s)", type(exc).__name__)
        return jsonify({"error": "No se pudo crear la solicitud de informe."}), 500

    return jsonify(req), 201


# ============================================================
# 2. LISTAR SOLICITUDES
# ============================================================

@report_requests_bp.get("")
@jwt_required
@roles_required(ROLE_TEACHER, ROLE_ADMIN)
def list_requests():
    """Lista solicitudes.

    Query params:
        status  (str):    PENDING | APPROVED | REJECTED | COMPLETED | ERROR. Opcional.
        mine    (1|true): (Solo profesor) filtrar solo sus solicitudes.
        limit   (int):    por defecto 100.
    """
    actor_uid, actor_email, actor_rol = _current_actor()
    status = (request.args.get("status") or None) or None
    limit = int(request.args.get("limit") or 100)
    mine = request.args.get("mine", "").lower() in {"1", "true", "yes", "si"}

    # Si el usuario NO es admin, SIEMPRE se le devuelven solo sus
    # solicitudes. Para garantizar que REJECTED / APPROVED /
    # COMPLETED antiguos aparezcan incluso si el total supera el
    # limit del scan global, usamos list_my_requests (query por PK
    # requester_uid en vez de scan). Un profesor nunca debe ver las
    # solicitudes de otros.
    if actor_rol != ROLE_ADMIN or mine:
        items, _next_key = _svc().list_my_requests(
            actor_uid,
            status=status,
            limit=max(limit, 500),
        )
        if not mine and actor_rol == ROLE_ADMIN:
            # Admin pidió "mine", pero list_my_requests ya filtra por
            # requester_uid así que no necesitamos filtrar extra.
            pass
    else:
        items, _next_key = _svc().list_requests(
            status=status,
            limit=limit,
        )

    if actor_rol == ROLE_ADMIN:
        items = [_admin_request_public(item) for item in items]

    return jsonify({"items": items, "count": len(items)}), 200


# ============================================================
# 3. OBTENER DETALLE DE UNA SOLICITUD
# ============================================================

@report_requests_bp.get("/<request_id>")
@jwt_required
@roles_required(ROLE_TEACHER, ROLE_ADMIN)
def get_request(request_id):
    actor_uid, actor_email, actor_rol = _current_actor()
    req = _svc().get_request(request_id)
    if not req:
        return jsonify({"error": "Solicitud no encontrada"}), 404

    try:
        req = _svc().authorize_request_access(
            req,
            actor_uid=actor_uid,
            actor_rol=actor_rol,
        )
    except PermissionError:
        # Uniform 404 for missing, foreign, or detached session requests.
        return jsonify({"error": "Solicitud no encontrada"}), 404

    if actor_rol == ROLE_ADMIN:
        req = _admin_request_public(req)

    return jsonify(req), 200


# ============================================================
# 4. REVISAR (aprobar/rechazar) — ADMINISTRADOR
# ============================================================

@report_requests_bp.patch("/<request_id>/review")
@jwt_required
@roles_required(ROLE_ADMIN)
def review_request(request_id):
    """Aprueba o rechaza una solicitud.

    Body:
        approved (bool):  True = aprobar, False = rechazar.
        comments (str):   (Opcional) Notas al aprobar.
        reason (str):     (Requerido si approved=False) Motivo de rechazo.
    """
    reviewer_uid, reviewer_email, reviewer_rol = _current_actor()
    current_app.logger.info(
        "report_approval_diagnostic event=received timestamp=%s request_id=%s pid=%s method=%s path=%s",
        datetime.now(timezone.utc).isoformat(), request_id, os.getpid(), request.method, request.path,
    )
    body = request.get_json(silent=True) or {}
    approved = body.get("approved")
    if approved is None:
        return jsonify({"error": "approved (bool) es obligatorio"}), 400

    comments = body.get("comments") or None
    reason = body.get("reason") or None

    try:
        if approved:
            result = _svc().approve_request(
                request_id,
                reviewer_uid=reviewer_uid,
                reviewer_email=reviewer_email,
                reviewer_rol=reviewer_rol,
                comments=comments,
            )
        else:
            if not reason:
                return jsonify({
                    "error": "reason es obligatorio cuando approved=False",
                }), 400
            result = _svc().reject_request(
                request_id,
                reviewer_uid=reviewer_uid,
                reviewer_email=reviewer_email,
                reviewer_rol=reviewer_rol,
                reason=reason,
            )
        current_app.logger.info(
            "report_approval_diagnostic event=completed timestamp=%s request_id=%s pid=%s method=%s path=%s approved=%s result_status=%s",
            datetime.now(timezone.utc).isoformat(), request_id, os.getpid(), request.method, request.path,
            bool(approved), result.get("status", "UNKNOWN"),
        )
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except PermissionError:
        return jsonify({"error": "Solicitud no encontrada"}), 404
    except Exception as exc:
        current_app.logger.error(
            "report_approval_diagnostic event=failed timestamp=%s request_id=%s pid=%s method=%s path=%s error_type=%s",
            datetime.now(timezone.utc).isoformat(), request_id, os.getpid(), request.method, request.path, type(exc).__name__,
        )
        current_app.logger.error("Error revisando solicitud de informe (tipo=%s)", type(exc).__name__)
        return jsonify({"error": "No se pudo revisar la solicitud de informe."}), 500

    return jsonify(_admin_request_public(result)), 200


# ============================================================
# 5. OBTENER URL DE DESCARGA (presigned S3)
# ============================================================

@report_requests_bp.get("/<request_id>/download")
@jwt_required
@roles_required(ROLE_TEACHER)
def download_request(request_id):
    """Retorna una URL temporal de S3 para descargar el PDF.

    Solo el especialista solicitante puede descargar el PDF aprobado y
    generado. El administrador revisa metadatos, pero no descarga informes.

    Registra AUDIT_REPORT_DOWNLOADED automáticamente.
    """
    actor_uid, actor_email, actor_rol = _current_actor()

    try:
        url = _svc().get_download_url(
            request_id,
            actor_uid=actor_uid,
            actor_email=actor_email,
            actor_rol=actor_rol,
            expires_in=300,
        )
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except PermissionError:
        # Avoid distinguishing another specialist's request from an unknown
        # request id.
        return jsonify({"error": "Solicitud no encontrada"}), 404
    except Exception as exc:
        current_app.logger.error("Error generando URL de descarga (tipo=%s)", type(exc).__name__)
        return jsonify({"error": "No se pudo generar el enlace de descarga."}), 500

    return jsonify({"download_url": url, "expires_in_seconds": 300}), 200
