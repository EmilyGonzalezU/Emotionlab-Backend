"""Generacion de reportes PDF y Excel para sesiones de EmotionLab."""

from io import BytesIO
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib import colors

from core.exceptions import SessionNotFound
from services.session_parser import SessionParser
from services.session_service import SessionService


class ReportService:
    """Construye reportes a partir de sesiones persistidas e indexadas."""

    def __init__(self, session_service: Optional[SessionService] = None):
        self.session_service = session_service or SessionService()

    def generate_individual_pdf(self, session_id: str) -> bytes:
        """Genera un PDF de una sesion, incluidas sus respuestas y eventos."""
        session = self.session_service.get_session_detail(session_id)
        if not session:
            raise SessionNotFound("La sesion no existe")

        parser = SessionParser(session["raw_json"])
        buffer = BytesIO()
        document = SimpleDocTemplate(buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        prediction = session.get("prediction") or {}
        summary = [
            ["ID de sesion", session.get("session_id", "")],
            ["Estado", session.get("status", "")],
            ["Nombre", session.get("nombre") or "Pendiente de identificacion"],
            ["Observaciones", session.get("observaciones") or ""],
            ["Fecha", session.get("fecha") or ""],
            ["Hora", session.get("hora") or ""],
            ["Duracion (seg.)", str(parser.get_duration() or "")],
            ["Dificultad", parser.get_difficulty() or ""],
            ["Bienestar", prediction.get("bienestar", "Pendiente")],
            ["Probabilidad", self._percentage(prediction.get("probabilidad"))],
            ["Confianza", self._percentage(prediction.get("confianza"))],
        ]
        story = [Paragraph("Reporte individual de sesion", styles["Title"]), Spacer(1, 12)]
        story.append(self._table(summary, [120, 390]))
        story.extend([Spacer(1, 16), Paragraph("Respuestas", styles["Heading2"])])
        answers = [["Pregunta", "Respuesta", "Escena", "Fecha"]]
        for answer in parser.get_answers():
            answers.append(
                [
                    str(answer.get("textoPregunta") or answer.get("idPregunta", "")),
                    str(answer.get("respuestaLabel", "")),
                    str(answer.get("escena", "")),
                    str(answer.get("timestamp", "")),
                ]
            )
        story.append(self._table(answers, [180, 90, 120, 120]))
        story.extend([Spacer(1, 16), Paragraph("Eventos", styles["Heading2"])])
        events = [["Evento", "Fase", "Fecha"]]
        for event in parser.get_events():
            events.append(
                [
                    str(event.get("nombre", "")),
                    str(event.get("fase", "")),
                    str(event.get("timestamp", "")),
                ]
            )
        story.append(self._table(events, [220, 100, 190]))
        document.build(story)
        return buffer.getvalue()

    def generate_sessions_excel(
        self, *, date_from: Optional[str] = None, date_to: Optional[str] = None
    ) -> bytes:
        """Genera un Excel general usando campos indexables de las sesiones."""
        sessions = self.session_service.list_sessions(date_from=date_from, date_to=date_to)
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Sesiones"
        headers = [
            "session_id",
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
            worksheet.append(
                [
                    session.get("session_id"),
                    session.get("nombre"),
                    session.get("status"),
                    session.get("fecha"),
                    session.get("hora"),
                    session.get("edad"),
                    session.get("genero"),
                    session.get("difficulty"),
                    prediction.get("bienestar"),
                    prediction.get("probabilidad"),
                    prediction.get("confianza"),
                ]
            )
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column in worksheet.columns:
            worksheet.column_dimensions[column[0].column_letter].width = min(
                max(len(str(cell.value or "")) for cell in column) + 2, 32
            )

        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    @staticmethod
    def _percentage(value) -> str:
        return f"{float(value) * 100:.2f}%" if value is not None else "Pendiente"

    @staticmethod
    def _table(data: list[list[str]], widths: list[int]) -> Table:
        table = Table(data, colWidths=widths, repeatRows=1 if len(data) > 1 else 0)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return table
