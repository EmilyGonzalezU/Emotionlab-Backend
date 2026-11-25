"""
GeneraciÃ³n de reportes PDF y Excel para sesiones de EmotionLab.

ETAPA 2:
- Todos los informes incluyen Aviso Legal.
- El PDF de una solicitud muestra el detalle completo de la sesiÃ³n
  solicitada.
- El PDF incluye ademÃ¡s un resumen histÃ³rico del participante,
  sin exponer el detalle de las demÃ¡s sesiones.
- Los valores ausentes se presentan mediante texto legible.
- Los PDF generados se almacenan en S3 privado mediante S3Service.
"""

from __future__ import annotations

from io import BytesIO
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
import os
import json

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
from services.emotion_classification import clasificacion_binaria
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
    "La informaciÃ³n presentada corresponde a una sugerencia generada con apoyo de "
    "inteligencia artificial y constituye una herramienta complementaria para la "
    "evaluaciÃ³n profesional. No corresponde a un diagnÃ³stico mÃ©dico ni reemplaza el "
    "criterio del profesional."
)


def _register_pdf_fonts() -> None:
    """Registra una fuente Unicode incluida con ReportLab, sin depender del SO."""
    fonts_dir = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    fonts = (
        ("EmotionLabVera", "Vera.ttf"),
        ("EmotionLabVera-Bold", "VeraBd.ttf"),
        ("EmotionLabVera-Italic", "VeraIt.ttf"),
        ("EmotionLabVera-BoldItalic", "VeraBI.ttf"),
    )
    for name, filename in fonts:
        try:
            pdfmetrics.getFont(name)
        except KeyError:
            pdfmetrics.registerFont(TTFont(name, os.path.join(fonts_dir, filename)))

    # Always register the family mapping. The normal font can already exist
    # in a warm worker even if a previous code path never registered its family.
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

    @staticmethod
    def _sessions_for_owner(
        sessions: list[dict],
        owner_id: Optional[str],
    ) -> list[dict]:
        """Limit a participant history to the session owner's scope."""
        if not owner_id:
            return []
        return [session for session in sessions if session.get("owner_id") == owner_id]

    # ==========================================================
    # PDF
    # ==========================================================

    def generate_individual_pdf(
        self,
        session_id: str,
        *,
        owner_id: Optional[str] = None,
        session_data: Optional[Mapping[str, Any]] = None,
    ) -> bytes:
        """
        Genera el PDF correspondiente a una solicitud de reporte.

        El PDF contiene:

        1. Detalle completo de la sesiÃ³n solicitada.
        2. Resultado de IA (modelo V2 K-Means) de esa sesiÃ³n.
        3. Respuestas y eventos de esa sesiÃ³n.
        4. Resumen histÃ³rico del participante:
           ID de sesiÃ³n, fecha, hora, estado, grupo K-Means,
           estado del anÃ¡lisis y versiÃ³n del modelo.
        5. Aviso legal.
        """

        # ======================================================
        # 1. OBTENER SESIÃ“N SOLICITADA
        # ======================================================

        session = (
            dict(session_data)
            if session_data is not None
            else self.session_service.get_session_detail(session_id)
        )

        if not session:
            raise SessionNotFound(
                "La sesiÃ³n no existe"
            )

        session_owner = session.get("owner_id")
        if owner_id and session_owner != owner_id:
            raise SessionNotFound("La sesiÃ³n no estÃ¡ disponible")
        history_owner = owner_id or session_owner

        participant_id = session.get(
            "participant_id"
        )
        if participant_id == "__PENDING__":
            participant_id = None

        # ======================================================
        # 2. OBTENER HISTORIAL, SI LA SESIÃ“N TIENE PARTICIPANTE
        # ======================================================

        historical_sessions = []
        if participant_id and participant_id != "__PENDING__":
            historical_sessions = (
                self.session_service.search_participant_sessions(participant_id)
                or []
            )

        # Participant IDs are pseudonymous but may be shared or reused. A
        # specialist's report must never include another owner's records.
        historical_sessions = self._sessions_for_owner(
            historical_sessions,
            history_owner,
        )

        # Aseguramos que la sesiÃ³n solicitada aparezca.
        session_ids = {
            str(item.get("session_id"))
            for item in historical_sessions
        }

        if str(session_id) not in session_ids:
            historical_sessions.append(session)

        # MÃ¡s reciente primero.
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
                "Reporte de sesiÃ³n",
                subtitle_style,
            )
        )

        header_data = [
            [
                Paragraph(
                    "<b>ID de sesiÃ³n</b>",
                    small_style,
                ),
                self._display(
                    session.get("session_id")
                ),
                Paragraph(
                    "<b>Participante</b>",
                    small_style,
                ),
                self._display(participant_id, "No asignado"),
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

        processing = session.get("model_processing") or {}
        if not isinstance(processing, dict):
            processing = {}

        grupo = self._display(
            clasificacion_binaria(processing),
            "No analizado",
        )

        estado_analisis = self._display(
            processing.get("status"),
            "Pendiente",
        )

        version_modelo = self._display(
            processing.get("model_version"),
            "No registrada",
        )

        ia_table = Table(
            [
                [
                    Paragraph(
                        "<b>ClasificaciÃ³n</b>",
                        small_style,
                    ),
                    grupo,
                    Paragraph(
                        "<b>AnÃ¡lisis</b>",
                        small_style,
                    ),
                    estado_analisis,
                    Paragraph(
                        "<b>VersiÃ³n modelo</b>",
                        small_style,
                    ),
                    version_modelo,
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
        # 6. DETALLE DE LA SESIÃ“N
        # ======================================================

        story.append(
            Paragraph(
                "Detalle de la sesiÃ³n",
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
                    "<b>GÃ©nero</b>",
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
                    "<b>DuraciÃ³n</b>",
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
                "Respuestas de la sesiÃ³n",
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
                    "â€”",
                    "No se registraron respuestas.",
                    "â€”",
                    "â€”",
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
                "Eventos de la sesiÃ³n",
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
                    "â€”",
                    "No se registraron eventos durante la sesiÃ³n.",
                    "â€”",
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
        # 9. RESUMEN HISTÃ“RICO
        # ======================================================

        story.append(PageBreak())

        story.append(
            Paragraph(
                "Resumen histÃ³rico de sesiones",
                section_style,
            )
        )

        story.append(
            Paragraph(
                (
                    "La siguiente tabla resume las sesiones asociadas a esta solicitud. El detalle de las demás sesiones no forma parte de este reporte."
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
                "ID de sesiÃ³n",
                "Fecha",
                "Hora",
                "Estado",
                "ClasificaciÃ³n",
                "AnÃ¡lisis",
                "VersiÃ³n modelo",
            ]
        ]

        for historical in historical_sessions:

            historical_processing = (
                historical.get(
                    "model_processing"
                )
                or {}
            )

            if not isinstance(
                historical_processing,
                dict,
            ):
                historical_processing = {}

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
                        clasificacion_binaria(
                            historical_processing
                        ),
                        "No analizado",
                    ),
                    self._display(
                        historical_processing.get(
                            "status"
                        ),
                        "Pendiente",
                    ),
                    self._display(
                        historical_processing.get(
                            "model_version"
                        ),
                        "No registrada",
                    ),
                ]
            )

        history_table = self._table(
            history,
            [
                120,  # ID de sesiÃ³n
                70,   # Fecha
                50,   # Hora
                70,   # Estado
                65,   # ClasificaciÃ³n
                75,   # AnÃ¡lisis
                70,   # VersiÃ³n modelo
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
        owner_id: Optional[str] = None,
    ) -> tuple[bytes, str]:
        """
        Genera el PDF de la sesiÃ³n y lo almacena en S3 privado.
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

        if owner_id and session_data.get("owner_id") != owner_id:
            raise SessionNotFound("La sesiÃ³n no estÃ¡ disponible")

        pdf_bytes = self.generate_individual_pdf(
            session_id,
            owner_id=owner_id or session_data.get("owner_id"),
            session_data=session_data,
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
                f"reporte_sesion_{session_id}_{fecha_stamp}.pdf"
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

    def generate_and_upload_tracking_pdf(
        self,
        tracking: dict,
        associated_sessions: list[dict],
        *,
        request_id: str,
        owner_id: str,
    ) -> tuple[bytes, str]:
        """Generate a tracking PDF from its currently assigned, owner-verified sessions."""
        if self.s3 is None:
            raise RuntimeError("S3Service no inyectado en ReportService")
        tracking_id = str(tracking.get("tracking_id") or "")
        if not tracking_id or not owner_id or not associated_sessions:
            raise ValueError("El seguimiento no tiene sesiones disponibles para el informe.")

        full_sessions = []
        seen = set()
        for association in associated_sessions:
            session_id = str(association.get("session_id") or "").strip()
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            # ReportRequestService passes the current, owner-verified session
            # snapshot. Reuse it without another DynamoDB lookup. Keep the
            # legacy direct-call path for entries that contain only a session ID.
            if association.get("owner_id") is None:
                session = self.session_service.get_session_detail(session_id)
            elif association.get("owner_id") != owner_id:
                session = dict(association)
            else:
                materialize = getattr(self.session_service, "_materialize_raw_json", None)
                session = materialize(association) if callable(materialize) else dict(association)
                session = session or dict(association)
            if (
                not session
                or session.get("owner_id") != owner_id
            ):
                raise SessionNotFound("El seguimiento no estÃ¡ disponible")
            full_sessions.append(session)

        if not full_sessions:
            raise ValueError("El seguimiento no tiene sesiones disponibles para el informe.")

        full_sessions.sort(key=lambda item: (
            str(item.get("fecha") or item.get("capture_started_at") or ""),
            str(item.get("hora") or ""),
            str(item.get("session_id") or ""),
        ))

        from services.tracking_report_pdf import build_tracking_pdf

        pdf_bytes = build_tracking_pdf(tracking, full_sessions, owner_id)
        if not pdf_bytes:
            raise RuntimeError("El PDF generado estÃ¡ vacÃ­o")

        date_stamp = datetime.utcnow().strftime("%Y-%m-%d")
        tracking_code = str(tracking.get("tracking_code") or tracking_id)
        filename = f"reporte_seguimiento_{tracking_code}_{date_stamp}.pdf"
        s3_key = f"reports/{date_stamp}/{request_id}/{filename}"
        self.s3.put_report_pdf(s3_key, pdf_bytes)
        return pdf_bytes, s3_key

    def generate_and_upload_tracking_excel(
        self,
        tracking: dict,
        associated_sessions: list[dict],
        *,
        request_id: str,
        owner_id: str,
    ) -> tuple[bytes, str]:
        """Build and store an owner-verified workbook for a tracking request."""
        if self.s3 is None:
            raise RuntimeError("S3Service no inyectado en ReportService")
        if not owner_id or not associated_sessions:
            raise ValueError("El seguimiento no tiene sesiones disponibles para el informe.")
        workbook = Workbook()
        sessions_sheet = workbook.active
        sessions_sheet.title = "Sesiones"
        sessions_sheet.append(["Sesión", "Fecha", "Hora", "Estado", "Grupo", "Versión del modelo"])
        answers_sheet = workbook.create_sheet("Respuestas")
        answers_sheet.append(["Sesión", "Etapa", "Pregunta", "Respuesta"])
        model_sheet = workbook.create_sheet("Respuestas del cálculo")
        model_sheet.append(["Sesión", "Dimensión", "Fase", "Variable", "Valor original"])
        for sheet in (sessions_sheet, answers_sheet, model_sheet):
            for cell in sheet[1]:
                cell.font = Font(bold=True)
        seen = set()
        full_sessions = []
        for association in associated_sessions:
            session_id = str(association.get("session_id") or "").strip()
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            session = self.session_service.get_session_detail(session_id)
            if not session or session.get("owner_id") != owner_id:
                raise SessionNotFound("El seguimiento no está disponible")
            full_sessions.append(session)
        full_sessions.sort(key=lambda item: (str(item.get("fecha") or item.get("capture_started_at") or ""), str(item.get("hora") or ""), str(item.get("session_id") or "")))
        for session in full_sessions:
            detail = session.get("model_processing") if isinstance(session.get("model_processing"), dict) else {}
            prediction = detail.get("prediction") if isinstance(detail.get("prediction"), dict) else {}
            sessions_sheet.append([session.get("session_id"), session.get("fecha") or session.get("capture_started_at"), session.get("hora"), session.get("status") or session.get("estado"), prediction.get("group", detail.get("kmeans_group", detail.get("grupo", detail.get("group")))), detail.get("model_version")])
            original_variables = detail.get("variables_originales") if isinstance(detail.get("variables_originales"), dict) else {}
            estimated = set(detail.get("estimadas", [])) if isinstance(detail.get("estimadas"), (list, tuple, set)) else set()
            for variable, value in original_variables.items():
                if variable in estimated or isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 9:
                    continue
                normalized = str(variable).lower()
                phase = "PRE" if normalized.endswith("_pre") or normalized.endswith("_inicial") else "FACIL" if "facil" in normalized else "DIFICIL" if "dificil" in normalized else "Sin identificar"
                dimension = next((name for name in ("estado_emocional", "nerviosismo", "preocupacion", "preparacion", "satisfaccion", "alivio") if name in normalized), "Sin identificar")
                model_sheet.append([session.get("session_id"), dimension, phase, variable, value])
            raw = session.get("raw_json") or session.get("respuestas_originales") or session.get("responses") or session.get("respuestas") or []
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    raw = []
            if isinstance(raw, dict):
                nested = raw.get("experiences") or raw.get("experiencias") or raw.get("experiences_data")
                if isinstance(nested, list) and nested:
                    records = nested
                else:
                    records = [raw]
            elif isinstance(raw, list):
                records = raw
            else:
                records = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                raw_answers = record.get("respuestas") or record.get("responses") or record.get("answers") or []
                if isinstance(raw_answers, list):
                    for answer in raw_answers:
                        if not isinstance(answer, dict):
                            continue
                        question_id = str(answer.get("idPregunta") or answer.get("id_pregunta") or answer.get("question_id") or answer.get("id") or "").strip().lower()
                        question = str(answer.get("pregunta") or answer.get("question") or "").strip().lower()
                        personal_ids = {"edad", "age", "student_age", "genero", "género", "gender", "student_gender", "nombre", "name", "student_name", "nombre_alumno"}
                        if question_id in personal_ids or any(term in question for term in ("edad", "age", "género", "genero", "gender", "nombre", "name")):
                            continue
                        answers_sheet.append([session.get("session_id"), answer.get("etapa") or answer.get("fase") or answer.get("scene"), answer.get("pregunta") or answer.get("question") or answer.get("idPregunta") or answer.get("question_id"), answer.get("respuestaLabel", answer.get("respuesta", answer.get("answer", answer.get("value"))))])
        for sheet in (sessions_sheet, answers_sheet, model_sheet):
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                letter = column[0].column_letter
                sheet.column_dimensions[letter].width = min(52, max(14, max((len(str(cell.value or "")) for cell in column), default=10) + 2))
        output = BytesIO()
        workbook.save(output)
        file_bytes = output.getvalue()
        date_stamp = datetime.utcnow().strftime("%Y-%m-%d")
        tracking_code = str(tracking.get("tracking_code") or tracking.get("tracking_id") or "seguimiento")
        filename = f"reporte_seguimiento_{tracking_code}_{date_stamp}.xlsx"
        s3_key = f"reports/{date_stamp}/{request_id}/{filename}"
        self.s3.put_report_excel(s3_key, file_bytes)
        return file_bytes, s3_key

    # ==========================================================
    # EXCEL GLOBAL
    # ==========================================================

    def generate_sessions_excel(
        self,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> bytes:
        """Genera un Excel general usando campos indexables."""

        sessions = self.session_service.list_sessions(
            date_from=date_from,
            date_to=date_to,
        )
        if owner_id is not None:
            sessions = [session for session in sessions if session.get("owner_id") == owner_id]

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
            "clasificacion",
            "analisis",
            "model_version",
        ]

        worksheet.append(headers)

        for cell in worksheet[1]:
            cell.font = Font(bold=True)

        for session in sessions:

            processing = (
                session.get("model_processing")
                or {}
            )

            if not isinstance(
                processing,
                dict,
            ):
                processing = {}

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
                    clasificacion_binaria(processing),
                    processing.get("status"),
                    processing.get("model_version"),
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
        Convierte valores vacÃ­os o ausentes en texto legible.
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

        Los IDs de sesiÃ³n pueden ser cadenas largas sin espacios.
        Las celdas se convierten en Paragraph para permitir saltos
        de lÃ­nea y evitar que el contenido se monte sobre columnas
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

        El carÃ¡cter \\u200b no se imprime, pero permite a ReportLab
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

        # TambiÃ©n acepta una fecha ISO aunque venga
        # acompaÃ±ada de texto adicional.
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
