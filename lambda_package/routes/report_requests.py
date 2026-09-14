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
    except Exception as exc:
        return jsonify({"error": f"Error creando solicitud: {exc}"}), 500

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

    items, _next_key = _svc().list_requests(status=status, limit=limit)

    # Si es profesor y pide "mine" o simplemente es profesor sin permiso admin, filtramos.
    if actor_rol != ROLE_ADMIN:
        items = [i for i in items if i.get("requester_uid") == actor_uid]
    elif mine:
        # Admin pide explícitamente las suyas
        items = [i for i in items if i.get("requester_uid") == actor_uid]

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

    # Profesor solo puede ver sus propias solicitudes
    if actor_rol != ROLE_ADMIN and req.get("requester_uid") != actor_uid:
        from core.exceptions import AuthorizationError
        raise AuthorizationError("No puede ver solicitudes de otros especialistas")

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
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        return jsonify({"error": f"Error revisando solicitud: {exc}"}), 500

    return jsonify(result), 200


# ============================================================
# 5. OBTENER URL DE DESCARGA (presigned S3)
# ============================================================

@report_requests_bp.get("/<request_id>/download")
@jwt_required
@roles_required(ROLE_TEACHER, ROLE_ADMIN)
def download_request(request_id):
    """Retorna una URL temporal de S3 para descargar el PDF.

    Reglas de acceso:
      - Administrador: cualquier solicitud en estado APPROVED o COMPLETED.
      - Especialista: solo sus propias solicitudes ya aprobadas/completadas.

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
    except PermissionError as pe:
        from core.exceptions import AuthorizationError
        raise AuthorizationError(str(pe))
    except Exception as exc:
        return jsonify({"error": f"Error generando URL de descarga: {exc}"}), 500

    return jsonify({"download_url": url, "expires_in_seconds": 300}), 200
