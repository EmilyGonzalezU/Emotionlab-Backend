"""Generacion de reportes PDF y Excel para sesiones de EmotionLab.

ETAPA 2:
- Seccion 9: Todos los informes incluyen Aviso Legal (IA herramienta complementaria,
  NO diagnostico medico). Texto exacto especificado en la seccion 9.
- Opcion de almacenar PDF generado directamente en S3 privado report-pdfs via S3Service.
- Mantiene una API pública estable para la generación de reportes.
"""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Any, Optional

from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)

from core.exceptions import SessionNotFound
from services.session_parser import SessionParser
from services.session_service import SessionService

if TYPE_CHECKING:
    from services.s3_service import S3Service

# Texto EXACTO del Aviso Legal - Seccion 9. NO MODIFICAR sin aprobacion.
_LEGAL_DISCLAIMER_TEXT = (
    "La información presentada corresponde a una sugerencia generada con apoyo de "
    "inteligencia artificial y constituye una herramienta complementaria para la "
    "evaluación profesional. No corresponde a un diagnóstico médico ni reemplaza el "
    "criterio del profesional."
)


class ReportService:
    """Construye reportes a partir de sesiones persistidas e indexadas."""

    def __init__(
        self,
        session_service: Optional[SessionService] = None,
        s3_service: Optional["S3Service"] = None,
    ):
        self.session_service = session_service or SessionService()
        self.s3 = s3_service

    # ==========================================================
    # PDF INDIVIDUAL
    # ==========================================================

    def generate_individual_pdf(self, session_id: str) -> bytes:
        """Genera un PDF de una sesion, incluyendo aviso legal (Seccion 9)."""
        session = self.session_service.get_session_detail(session_id)
        if not session:
            raise SessionNotFound("La sesion no existe")

        parser = SessionParser(session.get("raw_json") or {})
        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50,
        )
        styles = getSampleStyleSheet()
        prediction = session.get("prediction") or {}
        summary = [
            ["ID de sesion", session.get("session_id", "")],
            ["Estado", session.get("status", "") or session.get("estado", "")],
            ["Nombre", session.get("nombre") or "Pendiente de identificacion"],
            ["Observaciones", session.get("observaciones") or ""],
            ["Fecha", session.get("fecha") or ""],
            ["Hora", session.get("hora") or ""],
            ["Edad", str(session.get("edad") or "")],
            ["Genero", session.get("genero") or ""],
            ["Dificultad", parser.get_difficulty() or session.get("difficulty") or ""],
            ["Duracion (seg.)", str(parser.get_duration() or "")],
            ["Intentos", str(session.get("intentos") or "")],
            ["Bienestar", prediction.get("bienestar", "Pendiente")],
            ["Probabilidad", self._percentage(prediction.get("probabilidad"))],
            ["Confianza", self._percentage(prediction.get("confianza"))],
            ["Participante ID", session.get("participant_id") or ""],
        ]
        story: list[Any] = []
        story.append(Paragraph("Reporte individual de sesion", styles["Title"]))
        story.append(Spacer(1, 12))
        story.append(self._table(summary, [140, 370]))

        # Respuestas
        story.extend([Spacer(1, 16), Paragraph("Respuestas", styles["Heading2"])])
        answers: list[list[str]] = [["Pregunta", "Respuesta", "Escena", "Fecha"]]
        for answer in parser.get_answers():
            answers.append([
                str(answer.get("textoPregunta") or answer.get("idPregunta", "")),
                str(answer.get("respuestaLabel", "")),
                str(answer.get("escena", "")),
                str(answer.get("timestamp", "")),
            ])
        story.append(self._table(answers, [180, 90, 120, 120]))

        # Eventos
        story.extend([Spacer(1, 16), Paragraph("Eventos", styles["Heading2"])])
        events: list[list[str]] = [["Evento", "Fase", "Fecha"]]
        for event in parser.get_events():
            events.append([
                str(event.get("nombre", "")),
                str(event.get("fase", "")),
                str(event.get("timestamp", "")),
            ])
        story.append(self._table(events, [220, 100, 190]))

        # ==========================================================
        # SECCION 9 — AVISO LEGAL (NO MODIFICAR TEXTO)
        # ==========================================================
        story.append(PageBreak())
        story.append(Paragraph("Aviso Legal", styles["Heading1"]))
        story.append(Spacer(1, 10))
        legal_style = styles["BodyText"]
        legal_style.textColor = colors.HexColor("#8B0000")
        legal_border = Table(
            [[Paragraph(_LEGAL_DISCLAIMER_TEXT, legal_style)]],
            colWidths=[490],
        )
        legal_border.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 1.5, colors.HexColor("#8B0000")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF5F5")),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(legal_border)

        document.build(story)
        return buffer.getvalue()

    def generate_and_upload_pdf(
        self,
        session_id: str,
        *,
        request_id: Optional[str] = None,
        filename_hint: Optional[str] = None,
    ) -> tuple[bytes, str]:
        """Genera PDF y sube a S3 bucket report-pdfs. Retorna (bytes, s3_key)."""
        if self.s3 is None:
            raise RuntimeError(
                "S3Service no inyectado en ReportService: no se puede subir PDF"
            )
        pdf_bytes = self.generate_individual_pdf(session_id)
        fecha_stamp = (
            self.session_service._get_date(
                self.session_service.get_session(session_id) or {}
            )
            or SessionService.now_iso_partial_date()
            if hasattr(SessionService, "now_iso_partial_date")
            else "nodate"
        )
        import datetime as _dt
        fecha_stamp = fecha_stamp or _dt.datetime.utcnow().strftime("%Y-%m-%d")
        fname = filename_hint or f"reporte_{session_id}_{fecha_stamp}.pdf"
        prefix = request_id or "adhoc"
        s3_key = f"reports/{fecha_stamp}/{prefix}/{fname}"
        self.s3.put_report_pdf(s3_key, pdf_bytes)
        return pdf_bytes, s3_key

    # ==========================================================
    # EXCEL GLOBAL (se mantiene igual, no requiere S3)
    # ==========================================================

    def generate_sessions_excel(
        self, *, date_from: Optional[str] = None, date_to: Optional[str] = None
    ) -> bytes:
        """Genera un Excel general usando campos indexables de las sesiones."""
        sessions = self.session_service.list_sessions(
            date_from=date_from, date_to=date_to
        )
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Sesiones"
        headers = [
            "session_id",
            "participant_id",
            "nombre",
            "status",
            "fecha",
            "hora",
            "edad",
            "genero",
            "difficulty",
            "bienestar",
            "probabilidad",
            "confianza",
        ]
        worksheet.append(headers)
        for cell in worksheet[1]:
            cell.font = Font(bold=True)

        for session in sessions:
            prediction = session.get("prediction") or {}
            worksheet.append([
                session.get("session_id"),
                session.get("participant_id"),
                session.get("nombre"),
                session.get("status") or session.get("estado"),
                session.get("fecha"),
                session.get("hora"),
                session.get("edad"),
                session.get("genero"),
                session.get("difficulty"),
                prediction.get("bienestar"),
                prediction.get("probabilidad"),
                prediction.get("confianza"),
            ])
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column in worksheet.columns:
            worksheet.column_dimensions[column[0].column_letter].width = min(
                max(len(str(cell.value or "")) for cell in column) + 2, 32
            )
        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    # ==========================================================
    # HELPERS (iguales a legacy)
    # ==========================================================

    @staticmethod
    def _percentage(value) -> str:
        try:
            return f"{float(value) * 100:.2f}%" if value is not None else "Pendiente"
        except (TypeError, ValueError):
            return "Pendiente"

    @staticmethod
    def _table(data: list[list[str]], widths: list[int]) -> Table:
        table = Table(data, colWidths=widths, repeatRows=1 if len(data) > 1 else 0)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        return table
