"""
Generación de reportes PDF y Excel para sesiones de EmotionLab.

ETAPA 2:
- Todos los informes incluyen Aviso Legal.
- El PDF de una solicitud muestra el detalle completo de la sesión
  solicitada.
- El PDF incluye además un resumen histórico del participante,
  sin exponer el detalle de las demás sesiones.
- Los valores ausentes se presentan mediante texto legible.
- Los PDF generados se almacenan en S3 privado mediante S3Service.
"""

from __future__ import annotations

from io import BytesIO
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
import os

import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily

from openpyxl import Workbook
from openpyxl.styles import Font

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import (
    ParagraphStyle,
    getSampleStyleSheet,
)
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


# ==========================================================
# AVISO LEGAL
# ==========================================================

# Texto EXACTO del Aviso Legal - Seccion 9.
# NO MODIFICAR sin aprobacion.
_LEGAL_DISCLAIMER_TEXT = (
    "La información presentada corresponde a una sugerencia generada con apoyo de "
    "inteligencia artificial y constituye una herramienta complementaria para la "
    "evaluación profesional. No corresponde a un diagnóstico médico ni reemplaza el "
    "criterio del profesional."
)


def _register_pdf_fonts() -> None:
    """Registra una fuente Unicode incluida con ReportLab, sin depender del SO."""
    try:
        pdfmetrics.getFont("EmotionLabVera")
        return
    except KeyError:
        pass

    fonts_dir = os.path.join(os.path.dirname(reportlab.__file__), "fonts")

    pdfmetrics.registerFont(
        TTFont("EmotionLabVera", os.path.join(fonts_dir, "Vera.ttf"))
    )
    pdfmetrics.registerFont(
        TTFont("EmotionLabVera-Bold", os.path.join(fonts_dir, "VeraBd.ttf"))
    )
    pdfmetrics.registerFont(
        TTFont("EmotionLabVera-Italic", os.path.join(fonts_dir, "VeraIt.ttf"))
    )
    pdfmetrics.registerFont(
        TTFont(
            "EmotionLabVera-BoldItalic",
            os.path.join(fonts_dir, "VeraBI.ttf"),
        )
    )
    registerFontFamily(
        "EmotionLabVera",
        normal="EmotionLabVera",
        bold="EmotionLabVera-Bold",
        italic="EmotionLabVera-Italic",
        boldItalic="EmotionLabVera-BoldItalic",
    )


class ReportService:
    """Construye reportes a partir de sesiones persistidas e indexadas."""

    def __init__(
        self,
        session_service: Optional[SessionService] = None,
        s3_service: Optional["S3Service"] = None,
    ):
        self.session_service = (
            session_service or SessionService()
        )
        self.s3 = s3_service

    # ==========================================================
    # PDF
    # ==========================================================

    def generate_individual_pdf(
        self,
        session_id: str,
    ) -> bytes:
        """
        Genera el PDF correspondiente a una solicitud de reporte.

        El PDF contiene:

        1. Detalle completo de la sesión solicitada.
        2. Resultado de IA de esa sesión.
        3. Respuestas y eventos de esa sesión.
        4. Resumen histórico del participante:
           ID de sesión, fecha, hora, estado, bienestar,
           probabilidad y confianza.
        5. Aviso legal.
        """

        # ======================================================
        # 1. OBTENER SESIÓN SOLICITADA
        # ======================================================

        session = self.session_service.get_session_detail(
            session_id
        )

        if not session:
            raise SessionNotFound(
                "La sesión no existe"
            )

        participant_id = session.get(
            "participant_id"
        )

        if (
            not participant_id
            or participant_id == "__PENDING__"
        ):
            raise ValueError(
                "La sesión no tiene un participante identificado. "
                "No se puede generar el reporte."
            )

        # ======================================================
        # 2. OBTENER HISTORIAL DEL PARTICIPANTE
        # ======================================================

        historical_sessions = (
            self.session_service.search_participant_sessions(
                participant_id
            )
            or []
        )

        # Aseguramos que la sesión solicitada aparezca.
        session_ids = {
            str(item.get("session_id"))
            for item in historical_sessions
        }

        if str(session_id) not in session_ids:
            historical_sessions.append(session)

        # Más reciente primero.
        historical_sessions.sort(
            key=lambda item: (
                str(item.get("fecha") or ""),
                str(item.get("hora") or ""),
                str(item.get("session_id") or ""),
            ),
            reverse=True,
        )

        # ======================================================
        # 3. CONFIGURAR DOCUMENTO
        # ======================================================

        buffer = BytesIO()

        document = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            leftMargin=42,
            rightMargin=42,
            topMargin=42,
            bottomMargin=42,
        )

        _register_pdf_fonts()
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "EmotionLabTitle",
            parent=styles["Title"],
            fontName="EmotionLabVera",
            fontSize=22,
            leading=26,
            spaceAfter=8,
            alignment=0,
        )

        subtitle_style = ParagraphStyle(
            "EmotionLabSubtitle",
            parent=styles["Normal"],
            fontName="EmotionLabVera",
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#666666"),
            spaceAfter=14,
        )

        section_style = ParagraphStyle(
            "EmotionLabSection",
            parent=styles["Heading2"],
            fontName="EmotionLabVera",
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#17315E"),
            spaceBefore=12,
            spaceAfter=8,
        )

        small_style = ParagraphStyle(
            "EmotionLabSmall",
            parent=styles["Normal"],
            fontName="EmotionLabVera",
            fontSize=8.5,
            leading=11,
        )

        story: list[Any] = []

        # ======================================================
        # 4. CABECERA
        # ======================================================

        story.append(
            Paragraph(
                "EmotionLab",
                title_style,
            )
        )

        story.append(
            Paragraph(
                "Reporte de sesión",
                subtitle_style,
            )
        )

        header_data = [
            [
                Paragraph(
                    "<b>ID de sesión</b>",
                    small_style,
                ),
                self._display(
                    session.get("session_id")
                ),
                Paragraph(
                    "<b>Participante</b>",
                    small_style,
                ),
                self._display(
                    participant_id
                ),
            ],
            [
                Paragraph(
                    "<b>Fecha</b>",
                    small_style,
                ),
                self._display(
                    session.get("fecha")
                ),
                Paragraph(
                    "<b>Hora</b>",
                    small_style,
                ),
                self._display(
                    session.get("hora")
                ),
            ],
        ]

        header_table = Table(
            header_data,
            colWidths=[
                85,
                170,
                85,
                170,
            ],
        )

        header_table.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, -1),
                        colors.HexColor("#F4F7FB"),
                    ),
                    (
                        "BOX",
                        (0, 0),
                        (-1, -1),
                        0.8,
                        colors.HexColor("#D8E0EA"),
                    ),
                    (
                        "INNERGRID",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        colors.HexColor("#E2E7ED"),
                    ),
                    (
                        "VALIGN",
                        (0, 0),
                        (-1, -1),
                        "MIDDLE",
                    ),
                    (
                        "LEFTPADDING",
                        (0, 0),
                        (-1, -1),
                        8,
                    ),
                    (
                        "RIGHTPADDING",
                        (0, 0),
                        (-1, -1),
                        8,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                ]
            )
        )

        story.append(header_table)

        # ======================================================
        # 5. RESULTADO IA
        # ======================================================

        story.append(
            Paragraph(
                "Resultado de la inteligencia artificial",
                section_style,
            )
        )

        prediction = (
            session.get("prediction")
            or {}
        )

        bienestar = self._display(
            prediction.get("bienestar"),
            "Pendiente",
        )

        probabilidad = self._percentage(
            prediction.get("probabilidad")
        )

        confianza = self._percentage(
            prediction.get("confianza")
        )

        ia_table = Table(
            [
                [
                    Paragraph(
                        "<b>Bienestar</b>",
                        small_style,
                    ),
                    bienestar,
                    Paragraph(
                        "<b>Probabilidad</b>",
                        small_style,
                    ),
                    probabilidad,
                    Paragraph(
                        "<b>Confianza</b>",
                        small_style,
                    ),
                    confianza,
                ]
            ],
            colWidths=[
                70,
                90,
                80,
                90,
                70,
                100,
            ],
        )

        ia_table.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, -1),
                        colors.HexColor("#EEF4FF"),
                    ),
                    (
                        "BOX",
                        (0, 0),
                        (-1, -1),
                        0.8,
                        colors.HexColor("#B8CBE5"),
                    ),
                    (
                        "VALIGN",
                        (0, 0),
                        (-1, -1),
                        "MIDDLE",
                    ),
                    (
                        "ALIGN",
                        (1, 0),
                        (-1, -1),
                        "CENTER",
                    ),
                    (
                        "LEFTPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                    (
                        "RIGHTPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        9,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        9,
                    ),
                ]
            )
        )

        story.append(ia_table)

        # ======================================================
        # 6. DETALLE DE LA SESIÓN
        # ======================================================

        story.append(
            Paragraph(
                "Detalle de la sesión",
                section_style,
            )
        )

        parser = SessionParser(
            session.get("raw_json") or {}
        )

        dificultad = (
            parser.get_difficulty()
            or session.get("difficulty")
        )

        duracion = parser.get_duration()

        detalle = [
            [
                Paragraph(
                    "<b>Estado</b>",
                    small_style,
                ),
                self._display(
                    session.get("status")
                    or session.get("estado")
                ),
                Paragraph(
                    "<b>Nombre</b>",
                    small_style,
                ),
                self._display(
                    session.get("nombre"),
                    "Sin identificar",
                ),
            ],
            [
                Paragraph(
                    "<b>Edad</b>",
                    small_style,
                ),
                self._display(
                    session.get("edad")
                ),
                Paragraph(
                    "<b>Género</b>",
                    small_style,
                ),
                self._display(
                    session.get("genero")
                ),
            ],
            [
                Paragraph(
                    "<b>Dificultad</b>",
                    small_style,
                ),
                self._display(
                    dificultad
                ),
                Paragraph(
                    "<b>Duración</b>",
                    small_style,
                ),
                (
                    f"{duracion} segundos"
                    if duracion not in (
                        None,
                        "",
                        0,
                    )
                    else "No registrada"
                ),
            ],
            [
                Paragraph(
                    "<b>Intentos</b>",
                    small_style,
                ),
                self._display(
                    session.get("intentos"),
                    "No registrados",
                ),
                Paragraph(
                    "<b>Observaciones</b>",
                    small_style,
                ),
                self._display(
                    session.get("observaciones"),
                    "Sin observaciones",
                ),
            ],
        ]

        detalle_table = Table(
            detalle,
            colWidths=[
                75,
                165,
                85,
                175,
            ],
        )

        detalle_table.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (0, -1),
                        colors.HexColor("#F4F7FB"),
                    ),
                    (
                        "BACKGROUND",
                        (2, 0),
                        (2, -1),
                        colors.HexColor("#F4F7FB"),
                    ),
                    (
                        "GRID",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        colors.HexColor("#D8E0EA"),
                    ),
                    (
                        "VALIGN",
                        (0, 0),
                        (-1, -1),
                        "TOP",
                    ),
                    (
                        "LEFTPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                    (
                        "RIGHTPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                ]
            )
        )

        story.append(detalle_table)

        # ======================================================
        # 7. RESPUESTAS
        # ======================================================

        story.append(
            Paragraph(
                "Respuestas de la sesión",
                section_style,
            )
        )

        answers = [
            [
                "Pregunta",
                "Respuesta",
                "Escena",
                "Fecha",
            ]
        ]

        parsed_answers = (
            parser.get_answers()
            or []
        )

        if parsed_answers:
            for answer in parsed_answers:
                answers.append(
                    [
                        self._display(
                            answer.get(
                                "textoPregunta"
                            )
                            or answer.get(
                                "idPregunta"
                            ),
                            "Pregunta no registrada",
                        ),
                        self._display(
                            answer.get(
                                "respuestaLabel"
                            ),
                            "No realizada",
                        ),
                        self._display(
                            answer.get("escena"),
                            "No registrada",
                        ),
                        self._display(
                            answer.get(
                                "timestamp"
                            ),
                            "No registrada",
                        ),
                    ]
                )
        else:
            answers.append(
                [
                    "—",
                    "No se registraron respuestas.",
                    "—",
                    "—",
                ]
            )

        story.append(
            self._table(
                answers,
                [
                    180,
                    105,
                    105,
                    120,
                ],
            )
        )

        # ======================================================
        # 8. EVENTOS
        # ======================================================

        story.append(
            Paragraph(
                "Eventos de la sesión",
                section_style,
            )
        )

        events = [
            [
                "Evento",
                "Fase",
                "Fecha",
            ]
        ]

        parsed_events = (
            parser.get_events()
            or []
        )

        if parsed_events:
            for event in parsed_events:
                events.append(
                    [
                        self._display(
                            event.get("nombre"),
                            "Evento no identificado",
                        ),
                        self._display(
                            event.get("fase"),
                            "No registrada",
                        ),
                        self._display(
                            event.get(
                                "timestamp"
                            ),
                            "No registrada",
                        ),
                    ]
                )
        else:
            events.append(
                [
                    "—",
                    "No se registraron eventos durante la sesión.",
                    "—",
                ]
            )

        story.append(
            self._table(
                events,
                [
                    220,
                    110,
                    180,
                ],
            )
        )

        # ======================================================
        # 9. RESUMEN HISTÓRICO
        # ======================================================

        story.append(PageBreak())

        story.append(
            Paragraph(
                "Resumen histórico del participante",
                section_style,
            )
        )

        story.append(
            Paragraph(
                (
                    "La siguiente tabla muestra únicamente un "
                    "resumen de las sesiones asociadas al mismo "
                    "participante. El detalle de las demás sesiones "
                    "no forma parte de este reporte."
                ),
                ParagraphStyle(
                    "EmotionLabBodyUnicode",
                    parent=styles["BodyText"],
                    fontName="EmotionLabVera",
                ),
            )
        )

        story.append(
            Spacer(1, 10)
        )

        history = [
            [
                "ID de sesión",
                "Fecha",
                "Hora",
                "Estado",
                "Bienestar",
                "Probabilidad",
                "Confianza",
            ]
        ]

        for historical in historical_sessions:

            historical_prediction = (
                historical.get(
                    "prediction"
                )
                or {}
            )

            history.append(
                [
                    self._display(
                        historical.get(
                            "session_id"
                        ),
                        "No registrada",
                    ),
                    self._format_date(
                        historical.get(
                            "fecha"
                        ),
                        "No registrada",
                    ),
                    self._display(
                        historical.get(
                            "hora"
                        ),
                        "No registrada",
                    ),
                    self._display(
                        historical.get(
                            "status"
                        )
                        or historical.get(
                            "estado"
                        ),
                        "No registrado",
                    ),
                    self._display(
                        historical_prediction.get(
                            "bienestar"
                        ),
                        "Pendiente",
                    ),
                    self._percentage(
                        historical_prediction.get(
                            "probabilidad"
                        )
                    ),
                    self._percentage(
                        historical_prediction.get(
                            "confianza"
                        )
                    ),
                ]
            )

        history_table = self._table(
            history,
            [
                120,  # ID de sesión
                70,   # Fecha
                50,   # Hora
                70,   # Estado
                65,   # Bienestar
                75,   # Probabilidad
                70,   # Confianza
            ],
        )

        story.append(history_table)

        # ======================================================
        # 10. AVISO LEGAL
        # ======================================================

        story.append(PageBreak())

        story.append(
            Paragraph(
                "Aviso Legal",
                ParagraphStyle(
                    "EmotionLabLegalHeading",
                    parent=styles["Heading1"],
                    fontName="EmotionLabVera",
                ),
            )
        )

        story.append(
            Spacer(1, 10)
        )

        legal_style = ParagraphStyle(
            "EmotionLabLegal",
            parent=styles["BodyText"],
            fontName="EmotionLabVera",
            textColor=colors.HexColor("#8B0000"),
        )

        legal_border = Table(
            [
                [
                    Paragraph(
                        _LEGAL_DISCLAIMER_TEXT,
                        legal_style,
                    )
                ]
            ],
            colWidths=[490],
        )

        legal_border.setStyle(
            TableStyle(
                [
                    (
                        "BOX",
                        (0, 0),
                        (-1, -1),
                        1.5,
                        colors.HexColor("#8B0000"),
                    ),
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, -1),
                        colors.HexColor("#FFF5F5"),
                    ),
                    (
                        "LEFTPADDING",
                        (0, 0),
                        (-1, -1),
                        12,
                    ),
                    (
                        "RIGHTPADDING",
                        (0, 0),
                        (-1, -1),
                        12,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        12,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        12,
                    ),
                ]
            )
        )

        story.append(legal_border)

        document.build(story)

        return buffer.getvalue()

    # ==========================================================
    # GENERAR + SUBIR PDF
    # ==========================================================

    def generate_and_upload_pdf(
        self,
        session_id: str,
        *,
        request_id: Optional[str] = None,
        filename_hint: Optional[str] = None,
    ) -> tuple[bytes, str]:
        """
        Genera el PDF de la sesión y lo almacena en S3 privado.
        """

        if self.s3 is None:
            raise RuntimeError(
                "S3Service no inyectado en ReportService: "
                "no se puede subir PDF"
            )

        session_data = (
            self.session_service.get_session(
                session_id
            )
            or {}
        )

        participant_id = session_data.get(
            "participant_id"
        )

        if (
            not participant_id
            or participant_id == "__PENDING__"
        ):
            raise ValueError(
                "La sesión no tiene un participante identificado."
            )

        pdf_bytes = self.generate_individual_pdf(
            session_id
        )

        fecha_stamp = (
            self.session_service._get_date(
                session_data
            )
            or "nodate"
        )

        fname = (
            filename_hint
            or (
                f"reporte_sesion_{session_id}_"
                f"participante_{participant_id}_"
                f"{fecha_stamp}.pdf"
            )
        )

        prefix = request_id or "adhoc"

        s3_key = (
            f"reports/{fecha_stamp}/"
            f"{prefix}/{fname}"
        )

        self.s3.put_report_pdf(
            s3_key,
            pdf_bytes,
        )

        return pdf_bytes, s3_key

    # ==========================================================
    # EXCEL GLOBAL
    # ==========================================================

    def generate_sessions_excel(
        self,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> bytes:
        """Genera un Excel general usando campos indexables."""

        sessions = self.session_service.list_sessions(
            date_from=date_from,
            date_to=date_to,
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

            prediction = (
                session.get("prediction")
                or {}
            )

            worksheet.append(
                [
                    session.get("session_id"),
                    session.get("participant_id"),
                    session.get("nombre"),
                    session.get("status")
                    or session.get("estado"),
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
        worksheet.auto_filter.ref = (
            worksheet.dimensions
        )

        for column in worksheet.columns:
            worksheet.column_dimensions[
                column[0].column_letter
            ].width = min(
                max(
                    len(
                        str(
                            cell.value or ""
                        )
                    )
                    for cell in column
                )
                + 2,
                32,
            )

        buffer = BytesIO()

        workbook.save(buffer)

        return buffer.getvalue()

    # ==========================================================
    # HELPERS
    # ==========================================================

    @staticmethod
    def _display(
        value,
        fallback: str = "No registrado",
    ) -> str:
        """
        Convierte valores vacíos o ausentes en texto legible.
        """

        if value is None:
            return fallback

        if isinstance(value, str):

            value = value.strip()

            if not value:
                return fallback

            if value.lower() in {
                "none",
                "null",
                "undefined",
            }:
                return fallback

            return value

        if isinstance(
            value,
            (list, tuple, set, dict),
        ):

            if not value:
                return fallback

            return str(value)

        return str(value)

    @staticmethod
    def _percentage(
        value,
    ) -> str:
        try:
            return (
                f"{float(value) * 100:.2f}%"
                if value is not None
                else "Pendiente"
            )
        except (
            TypeError,
            ValueError,
        ):
            return "Pendiente"

    @staticmethod
    def _table(
        data: list[list[str]],
        widths: list[int],
    ) -> Table:
        """
        Construye tablas PDF con texto envolvente.

        Los IDs de sesión pueden ser cadenas largas sin espacios.
        Las celdas se convierten en Paragraph para permitir saltos
        de línea y evitar que el contenido se monte sobre columnas
        vecinas.
        """

        header_style = ParagraphStyle(
            "EmotionLabTableHeader",
            fontName="EmotionLabVera-Bold",
            fontSize=7.5,
            leading=9,
            textColor=colors.white,
            wordWrap="CJK",
            spaceAfter=0,
            spaceBefore=0,
        )

        body_style = ParagraphStyle(
            "EmotionLabTableBody",
            fontName="EmotionLabVera",
            fontSize=7.5,
            leading=9,
            wordWrap="CJK",
            spaceAfter=0,
            spaceBefore=0,
        )

        formatted_data: list[list[Any]] = []

        for row_index, row in enumerate(data):
            formatted_row: list[Any] = []

            for value in row:
                display_value = ReportService._display(
                    value
                )

                display_value = (
                    ReportService._soft_wrap(
                        display_value
                    )
                )

                style = (
                    header_style
                    if row_index == 0
                    else body_style
                )

                formatted_row.append(
                    Paragraph(
                        display_value,
                        style,
                    )
                )

            formatted_data.append(formatted_row)

        table = Table(
            formatted_data,
            colWidths=widths,
            repeatRows=1 if len(data) > 1 else 0,
        )

        table.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, 0),
                        colors.HexColor("#1F4E78"),
                    ),
                    (
                        "TEXTCOLOR",
                        (0, 0),
                        (-1, 0),
                        colors.white,
                    ),
                    (
                        "GRID",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        colors.grey,
                    ),
                    (
                        "VALIGN",
                        (0, 0),
                        (-1, -1),
                        "TOP",
                    ),
                    (
                        "LEFTPADDING",
                        (0, 0),
                        (-1, -1),
                        5,
                    ),
                    (
                        "RIGHTPADDING",
                        (0, 0),
                        (-1, -1),
                        5,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        5,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        5,
                    ),
                ]
            )
        )

        return table

    @staticmethod
    def _soft_wrap(
        value: str,
        chunk_size: int = 10,
    ) -> str:
        """
        Inserta puntos de corte invisibles en cadenas largas.

        El carácter \\u200b no se imprime, pero permite a ReportLab
        dividir correctamente IDs largos sin espacios.
        """

        if not value:
            return value

        parts = [
            value[index:index + chunk_size]
            for index in range(
                0,
                len(value),
                chunk_size,
            )
        ]

        return "\u200b".join(parts)

    @staticmethod
    def _format_date(
        value,
        fallback: str = "No registrada",
    ) -> str:
        """
        Convierte fechas ISO/timestamp a DD-MM-YYYY.

        Ejemplos:
            2026-09-29 -> 29-09-2026
            2026-09-29T12:03:00 -> 29-09-2026
        """

        if value is None:
            return fallback

        text = str(value).strip()

        if not text:
            return fallback

        if text.lower() in {
            "none",
            "null",
            "undefined",
        }:
            return fallback

        try:
            normalized = text.replace(
                "Z",
                "+00:00",
            )

            parsed = datetime.fromisoformat(
                normalized
            )

            return parsed.strftime(
                "%d-%m-%Y"
            )

        except ValueError:
            pass

        # También acepta una fecha ISO aunque venga
        # acompañada de texto adicional.
        import re

        match = re.match(
            r"^(\d{4})-(\d{2})-(\d{2})",
            text,
        )

        if match:
            return (
                f"{match.group(3)}-"
                f"{match.group(2)}-"
                f"{match.group(1)}"
            )

        return text

