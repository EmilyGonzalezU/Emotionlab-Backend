"""
Rutas de reportes EmotionLab.

IMPORTANTE:
El PDF individual NO se genera directamente desde /reports/pdf/<session_id>.

El flujo oficial es:

    POST /report-requests
        ↓
    PENDING
        ↓
    Admin revisa
        ↓
    APPROVED
        ↓
    genera PDF + S3
        ↓
    COMPLETED
        ↓
    GET /report-requests/<request_id>/download

Se mantiene /reports/excel para el reporte general.
"""

from flask import Blueprint, current_app, request, send_file
from io import BytesIO

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


reports_bp = Blueprint(
    "reports",
    __name__,
    url_prefix="/reports",
)


# ==========================================================
# EXCEL GENERAL
# ==========================================================

@reports_bp.get("/excel")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def sessions_excel():
    """
    Genera el Excel general de sesiones.

    Este endpoint no corresponde al flujo de aprobación
    de informes PDF individuales.
    """

    data = current_app.extensions[
        "services"
    ]["report_service"].generate_sessions_excel(
        date_from=(
            request.args.get("from")
            or request.args.get("date_from")
        ),
        date_to=(
            request.args.get("to")
            or request.args.get("date_to")
        ),
    )

    return send_file(
        BytesIO(data),
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        as_attachment=True,
        download_name="sesiones.xlsx",
    )