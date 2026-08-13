from flask import Blueprint, current_app, request, send_file
from io import BytesIO

from core.roles import ROLE_ADMIN, ROLE_TEACHER
from middleware.jwt_required import jwt_required
from middleware.roles_required import roles_required


reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


@reports_bp.get("/pdf/<session_id>")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def session_pdf(session_id):
    data = current_app.extensions["services"]["report_service"].generate_individual_pdf(session_id)
    return send_file(
        BytesIO(data), mimetype="application/pdf", as_attachment=True,
        download_name=f"sesion-{session_id}.pdf",
    )


@reports_bp.get("/excel")
@jwt_required
@roles_required(ROLE_ADMIN, ROLE_TEACHER)
def sessions_excel():
    data = current_app.extensions["services"]["report_service"].generate_sessions_excel(
        date_from=request.args.get("from") or request.args.get("date_from"),
        date_to=request.args.get("to") or request.args.get("date_to")
    )
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="sesiones.xlsx",
    )
