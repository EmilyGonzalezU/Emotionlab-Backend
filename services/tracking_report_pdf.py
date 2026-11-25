"""PDF builder for a specialist-owned tracking and its assigned sessions."""

from __future__ import annotations

import math
from decimal import Decimal
from html import escape
from io import BytesIO
from typing import Any, Mapping

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.graphics.shapes import Circle, Drawing, Line, Rect, String

from services.report_service import _register_pdf_fonts
from services.session_parser import SessionParser


MODEL_VERSION = "1.0.0-experimental"
PHASES = ("PRE", "FACIL", "DIFICIL")
PHASE_LABELS = {"PRE": "PRE", "FACIL": "Modo facil", "DIFICIL": "Modo dificil"}
PHASE_COLORS = {
    "PRE": colors.HexColor("#14283F"),
    "FACIL": colors.HexColor("#18A6A0"),
    "DIFICIL": colors.HexColor("#F59E0B"),
}
DIMENSIONS = (
    ("nerviosismo", "Nerviosismo", {"PRE": "nerviosismo_pre", "FACIL": "nerviosismo_facil", "DIFICIL": "nerviosismo_dificil"}),
    ("satisfaccion", "Satisfaccion", {"FACIL": "satisfaccion_facil", "DIFICIL": "satisfaccion_dificil"}),
    ("alivio", "Alivio", {"FACIL": "alivio_facil", "DIFICIL": "alivio_dificil"}),
    ("estado_emocional", "Estado emocional", {"PRE": "estado_pre", "FACIL": "estado_facil", "DIFICIL": "estado_dificil"}),
    ("preocupacion", "Preocupacion", {"PRE": "preocupacion_pre"}),
    ("preparacion", "Preparacion", {"PRE": "preparacion_pre"}),
)
MODEL_VARIABLES = (
    ("estado_pre", "Estado emocional", "Antes"),
    ("nerviosismo_pre", "Nerviosismo", "Antes"),
    ("preocupacion_pre", "Preocupación", "Antes"),
    ("preparacion_pre", "Preparación", "Antes"),
    ("estado_facil", "Estado emocional", "Modo fácil"),
    ("nerviosismo_facil", "Nerviosismo", "Modo fácil"),
    ("satisfaccion_facil", "Satisfacción", "Modo fácil"),
    ("alivio_facil", "Alivio", "Modo fácil"),
    ("estado_dificil", "Estado emocional", "Modo difícil"),
    ("nerviosismo_dificil", "Nerviosismo", "Modo difícil"),
    ("satisfaccion_dificil", "Satisfacción", "Modo difícil"),
    ("alivio_dificil", "Alivio", "Modo difícil"),
)


def _text(value: Any, fallback: str = "No disponible") -> str:
    if value is None or value == "":
        value = fallback
    return escape(str(value)).replace("\n", "<br/>")


def _valid_response(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float, Decimal))
        and math.isfinite(float(value))
        and float(value).is_integer()
        and 1 <= float(value) <= 9
    )


def _observations(session: Mapping[str, Any]) -> dict[tuple[str, str], int]:
    processing = session.get("model_processing")
    if not isinstance(processing, Mapping) or processing.get("model_version") != MODEL_VERSION:
        return {}
    originals = processing.get("variables_originales")
    if not isinstance(originals, Mapping):
        return {}
    estimated = processing.get("estimadas")
    estimated = set(estimated) if isinstance(estimated, (list, tuple, set)) else set()
    found: dict[tuple[str, str], int] = {}
    for dimension_id, _, variables in DIMENSIONS:
        for phase, variable in variables.items():
            value = originals.get(variable)
            if variable in estimated or not _valid_response(value):
                continue
            found[(dimension_id, phase)] = int(value)
    return found


def _session_parser(session: Mapping[str, Any]) -> SessionParser:
    raw = session.get("raw_json")
    return SessionParser(raw if isinstance(raw, Mapping) else {})


def _model_result(session: Mapping[str, Any]) -> Mapping[str, Any]:
    processing = session.get("model_processing")
    if isinstance(processing, Mapping) and processing.get("model_version") == MODEL_VERSION:
        return processing
    return {}


def _number_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return "Sin dato"
    numeric = float(value)
    if not math.isfinite(numeric):
        return "Sin dato"
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:.2f}"


def _measurement_summary(sessions: list[dict]) -> list[list[Any]]:
    rows: list[list[Any]] = [["Aspecto", "Momento", "Sesiones con dato", "Promedio", "Mínimo", "Máximo"]]
    variable_dimensions = {
        variable: (dimension_id, phase)
        for dimension_id, _, variables in DIMENSIONS
        for phase, variable in variables.items()
    }
    observations = [_observations(session) for session in sessions]
    for variable, aspect, phase in MODEL_VARIABLES:
        dimension, phase_key = variable_dimensions[variable]
        values = [row[(dimension, phase_key)] for row in observations if (dimension, phase_key) in row]
        rows.append([
            aspect, phase, str(len(values)),
            f"{sum(values) / len(values):.2f}" if values else "Sin datos",
            str(min(values)) if values else "-",
            str(max(values)) if values else "-",
        ])
    return rows


def _coverage_chart(sessions: list[dict]) -> Drawing:
    width, height = 500, 155
    drawing = Drawing(width, height)
    left, right, bottom, top = 42, 488, 30, 120
    count = max(1, len(sessions))
    x = lambda index: left + ((index - 0.5) / count) * (right - left)
    y = lambda value: bottom + (max(0, min(12, value)) / 12) * (top - bottom)
    drawing.add(String(left, height - 13, "Respuestas originales disponibles por sesión (máximo 12)", fontName="EmotionLabVera-Bold", fontSize=9, fillColor=colors.HexColor("#14283F")))
    for value in (0, 3, 6, 9, 12):
        yy = y(value)
        drawing.add(Line(left, yy, right, yy, strokeColor=colors.HexColor("#E2E8F0"), strokeWidth=0.5))
        drawing.add(String(22, yy - 3, str(value), fontName="EmotionLabVera", fontSize=7, fillColor=colors.HexColor("#64748B")))
    slot = (right - left) / count
    for index, session in enumerate(sessions, start=1):
        amount = len(_observations(session))
        if amount:
            drawing.add(Rect(
                x(index) - min(15, slot * 0.32), bottom,
                min(30, slot * 0.64), y(amount) - bottom,
                fillColor=colors.HexColor("#18A6A0"), strokeColor=None,
            ))
        drawing.add(String(x(index) - 5, 12, f"S{index}", fontName="EmotionLabVera", fontSize=6.5, fillColor=colors.HexColor("#64748B")))
    return drawing


def _general_points(sessions: list[dict]) -> list[dict]:
    points = []
    for index, session in enumerate(sessions, start=1):
        values = _observations(session)
        dimension_means = []
        coverage = []
        for dimension_id, _, variables in DIMENSIONS:
            phase_values = [values[(dimension_id, phase)] for phase in variables if (dimension_id, phase) in values]
            if phase_values:
                dimension_means.append(sum(phase_values) / len(phase_values))
                coverage.extend(f"{dimension_id}:{phase}" for phase in variables if (dimension_id, phase) in values)
        points.append({
            "session_number": index,
            "value": sum(dimension_means) / len(dimension_means) if dimension_means else None,
            "coverage": "|".join(sorted(coverage)),
        })
    return points


def _chart(
    title: str,
    sessions: list[dict],
    *,
    dimension: tuple[str, str, Mapping[str, str]] | None = None,
) -> Drawing:
    width, height = 500, 185
    drawing = Drawing(width, height)
    left, right, bottom, top = 36, 488, 28, 151
    count = max(1, len(sessions))
    x = lambda number: left + ((number - 1) / max(1, count - 1)) * (right - left)
    y = lambda value: bottom + ((value - 1) / 8) * (top - bottom)
    drawing.add(String(left, height - 13, title, fontName="EmotionLabVera-Bold", fontSize=10, fillColor=colors.HexColor("#14283F")))
    for value in range(1, 10):
        yy = y(value)
        drawing.add(Line(left, yy, right, yy, strokeColor=colors.HexColor("#E2E8F0"), strokeWidth=0.45))
        drawing.add(String(16, yy - 3, str(value), fontName="EmotionLabVera", fontSize=7, fillColor=colors.HexColor("#64748B")))
    drawing.add(Line(left, bottom, left, top, strokeColor=colors.HexColor("#64748B"), strokeWidth=0.7))
    drawing.add(Line(left, bottom, right, bottom, strokeColor=colors.HexColor("#64748B"), strokeWidth=0.7))
    stride = max(1, math.ceil(count / 18))
    for number in range(1, count + 1, stride):
        drawing.add(String(x(number) - 5, 12, f"S{number}", fontName="EmotionLabVera", fontSize=6.5, fillColor=colors.HexColor("#64748B")))

    if dimension is None:
        points = _general_points(sessions)
        for index in range(1, len(points)):
            previous, current = points[index - 1], points[index]
            if previous["value"] is not None and current["value"] is not None and previous["coverage"] == current["coverage"]:
                drawing.add(Line(x(previous["session_number"]), y(previous["value"]), x(current["session_number"]), y(current["value"]), strokeColor=colors.HexColor("#18A6A0"), strokeWidth=1.8))
        for point in points:
            if point["value"] is not None:
                drawing.add(Circle(x(point["session_number"]), y(point["value"]), 3, fillColor=colors.HexColor("#18A6A0"), strokeColor=colors.white, strokeWidth=0.7))
        return drawing

    dimension_id, _, variables = dimension
    points = [_observations(session) for session in sessions]
    legend_x = left
    for phase in PHASES:
        if phase not in variables:
            continue
        phase_values = [(index + 1, values.get((dimension_id, phase))) for index, values in enumerate(points)]
        phase_color = PHASE_COLORS[phase]
        for i in range(1, len(phase_values)):
            prev_num, prev_value = phase_values[i - 1]
            cur_num, cur_value = phase_values[i]
            if prev_value is not None and cur_value is not None:
                drawing.add(Line(x(prev_num), y(prev_value), x(cur_num), y(cur_value), strokeColor=phase_color, strokeWidth=1.5))
        for session_number, value in phase_values:
            if value is not None:
                drawing.add(Circle(x(session_number), y(value), 2.6, fillColor=phase_color, strokeColor=colors.white, strokeWidth=0.5))
        drawing.add(Line(legend_x, height - 14, legend_x + 12, height - 14, strokeColor=phase_color, strokeWidth=1.8))
        drawing.add(String(legend_x + 15, height - 17, PHASE_LABELS[phase], fontName="EmotionLabVera", fontSize=7, fillColor=colors.HexColor("#64748B")))
        legend_x += 82
    return drawing


def _table(rows: list[list[Any]], widths: list[float] | None = None) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14283F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "EmotionLabVera-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "EmotionLabVera"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F8")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def build_tracking_pdf(tracking: Mapping[str, Any], sessions: list[dict], owner_id: str) -> bytes:
    """Build a detailed, descriptive PDF from owner-verified assigned sessions."""
    tracking_id = str(tracking.get("tracking_id") or "")
    if not tracking_id or not owner_id:
        raise ValueError("No se pudo validar el seguimiento solicitado.")
    _register_pdf_fonts()

    dates = sorted(
        str(session.get("fecha") or session.get("capture_started_at") or "")
        for session in sessions
        if session.get("fecha") or session.get("capture_started_at")
    )
    title_style = ParagraphStyle("TrackingReportTitle", fontName="EmotionLabVera", fontSize=22, leading=27, textColor=colors.HexColor("#14283F"), spaceAfter=5)
    subtitle_style = ParagraphStyle("TrackingReportSubtitle", fontName="EmotionLabVera", fontSize=9, leading=13, textColor=colors.HexColor("#64748B"), spaceAfter=8)
    section_style = ParagraphStyle("TrackingReportSection", fontName="EmotionLabVera", fontSize=13, leading=17, textColor=colors.HexColor("#14283F"), spaceBefore=11, spaceAfter=7)
    body_style = ParagraphStyle("TrackingReportBody", fontName="EmotionLabVera", fontSize=8, leading=11, textColor=colors.HexColor("#172B3A"))
    small_style = ParagraphStyle("TrackingReportSmall", parent=body_style, fontSize=7, leading=9)

    parsers = [_session_parser(session) for session in sessions]
    observed = [_observations(session) for session in sessions]
    answer_counts = [len(parser.get_answers()) for parser in parsers]
    event_counts = [len(parser.get_events()) for parser in parsers]
    model_results = [_model_result(session) for session in sessions]
    analyzed_count = sum(1 for result in model_results if result.get("grupo") in (0, 1))
    group_counts = {
        group: sum(1 for result in model_results if result.get("grupo") == group)
        for group in (0, 1)
    }
    observation_count = sum(len(values) for values in observed)
    response_count = sum(answer_counts)
    event_count = sum(event_counts)

    def cell(value: Any) -> Paragraph:
        return Paragraph(_text(value), small_style)

    story: list[Any] = [
        Paragraph("<b>EmotionLab</b>", title_style),
        Paragraph("Informe completo del seguimiento", subtitle_style),
        Paragraph(f"C\u00f3digo: {_text(tracking.get('tracking_code'))} &nbsp;&nbsp; Sesiones incluidas: {len(sessions)}", body_style),
        Paragraph(f"Per\u00edodo registrado: {_text(dates[0] if dates else None)} - {_text(dates[-1] if dates else None)}", body_style),
        Spacer(1, 8),
        Paragraph("Este informe presenta las sesiones actualmente asociadas al seguimiento, sus respuestas, eventos y resultados disponibles. La asociaci\u00f3n no confirma que todas correspondan a una misma persona.", body_style),
        Paragraph("Los gr\u00e1ficos y promedios son descriptivos de los datos registrados. No representan un diagn\u00f3stico ni un resultado cl\u00ednico.", body_style),
        Spacer(1, 8),
        _table([
            ["Sesiones", "Con grupo asignado", "Respuestas registradas", "Eventos registrados"],
            [str(len(sessions)), str(analyzed_count), str(response_count), str(event_count)],
        ], [95, 125, 150, 158]),
        Spacer(1, 6),
        _table([
            ["Grupo 0 asignado por IA", "Grupo 1 asignado por IA", "Medidas originales disponibles"],
            [str(group_counts[0]), str(group_counts[1]), f"{observation_count} de {len(sessions) * len(MODEL_VARIABLES)} posibles"],
        ], [176, 176, 176]),
        Paragraph("Los grupos se muestran tal como fueron asignados por el modelo; este informe no les atribuye un significado cl\u00ednico.", small_style),
        Paragraph("<b>Evoluci\u00f3n general de las respuestas</b>", section_style),
        Paragraph("Cada punto resume las medidas originales disponibles en una sesi\u00f3n. La escala de las medidas del modelo va de 1 a 9. El resumen combina los aspectos disponibles y solo conecta sesiones con la misma cobertura.", body_style),
        _chart("Resumen descriptivo por experiencia", sessions),
        Paragraph("<b>Cobertura de medidas por sesi\u00f3n</b>", section_style),
        Paragraph("Muestra cu\u00e1ntas de las doce medidas originales del c\u00e1lculo est\u00e1n disponibles en cada sesi\u00f3n. Las estimaciones no se cuentan aqu\u00ed.", body_style),
        _coverage_chart(sessions),
        Paragraph("<b>Evoluci\u00f3n por aspecto y fase</b>", section_style),
        Paragraph("Cada l\u00ednea conserva su aspecto y momento de registro. Los datos faltantes permanecen vac\u00edos y no se presentan como observaciones.", body_style),
    ]
    for dimension in DIMENSIONS:
        story.append(_chart(dimension[1], sessions, dimension=dimension))
        story.append(Spacer(1, 4))

    story.extend([
        PageBreak(),
        Paragraph("<b>Resumen de medidas originales</b>", section_style),
        Paragraph("Promedios calculados solo con respuestas originales disponibles. Las estimaciones del modelo se excluyen de esta tabla.", body_style),
        _table(_measurement_summary(sessions), [146, 92, 92, 68, 62, 68]),
        PageBreak(),
        Paragraph("<b>Sesiones incluidas</b>", section_style),
    ])
    session_rows = [["N\u00b0", "ID de sesi\u00f3n", "Fecha", "Estado", "Dificultad", "Grupo IA", "Resp.", "Eventos"]]
    for index, session in enumerate(sessions, start=1):
        result = model_results[index - 1]
        session_rows.append([
            str(index), cell(session.get("session_id")),
            cell(session.get("fecha") or session.get("capture_started_at")),
            cell(session.get("status")), cell(session.get("difficulty")),
            cell(result.get("grupo") if result.get("grupo") in (0, 1) else None),
            str(answer_counts[index - 1]), str(event_counts[index - 1]),
        ])
    story.append(_table(session_rows, [22, 100, 82, 62, 67, 52, 65, 62]))

    model_status_labels = {
        "analyzed": "C\u00e1lculo disponible",
        "insufficient_data": "Informaci\u00f3n insuficiente",
        "processing_error": "No se pudo calcular",
        "calculado_sin_estimacion": "C\u00e1lculo sin estimaciones",
        "calculado_con_estimacion": "C\u00e1lculo con estimaciones",
        "informacion_insuficiente": "Informaci\u00f3n insuficiente",
        "sin_datos_suficientes": "Datos insuficientes",
        "error_entrada": "Datos no compatibles",
    }
    for index, session in enumerate(sessions, start=1):
        story.append(PageBreak())
        parser = parsers[index - 1]
        result = model_results[index - 1]
        values = observed[index - 1]
        raw_data = session.get("raw_json") if isinstance(session.get("raw_json"), Mapping) else {}
        raw_session = raw_data.get("session_data") if isinstance(raw_data.get("session_data"), Mapping) else {}
        start_date = parser.get_start_date()
        end_date = parser.get_end_date()
        try:
            duration = parser.get_duration()
        except (TypeError, ValueError):
            duration = None
        estimated = result.get("estimadas") if isinstance(result.get("estimadas"), (list, tuple, set)) else []
        originals = result.get("variables_originales") if isinstance(result.get("variables_originales"), Mapping) else {}
        processed = result.get("variables_procesadas") if isinstance(result.get("variables_procesadas"), Mapping) else {}
        group = result.get("grupo")

        story.extend([
            Paragraph(f"<b>Sesi\u00f3n {index}: {_text(session.get('session_id'))}</b>", section_style),
            _table([
                ["Fecha guardada", "Inicio", "T\u00e9rmino", "Duraci\u00f3n", "Estado", "Dificultad"],
                [cell(session.get("fecha") or session.get("capture_started_at")),
                 cell(start_date.isoformat(sep=" ", timespec="seconds") if start_date else None),
                 cell(end_date.isoformat(sep=" ", timespec="seconds") if end_date else None),
                 cell(f"{duration} s" if duration is not None else None),
                 cell(session.get("status")), cell(session.get("difficulty"))],
            ], [88, 105, 105, 62, 78, 82]),
            Paragraph("Resultado del c\u00e1lculo", subtitle_style),
            _table([
                ["Estado", "Grupo asignado", "Versi\u00f3n", "Medidas originales", "Estimadas"],
                [cell(model_status_labels.get(result.get("status") or result.get("estado"), result.get("status") or result.get("estado") or "No disponible")),
                 cell(f"Grupo {group}" if group in (0, 1) else None),
                 cell(result.get("model_version")), str(len(values)), str(len(estimated))],
            ], [130, 100, 92, 105, 93]),
        ])

        if result:
            variable_rows = [["Aspecto", "Momento", "Respuesta original", "Valor usado", "Origen"]]
            for variable, aspect, phase in MODEL_VARIABLES:
                original = originals.get(variable)
                used = processed.get(variable) if variable in processed else original
                is_estimated = variable in estimated
                variable_rows.append([
                    aspect, phase, _number_text(original), _number_text(used),
                    "Estimado" if is_estimated else "Medido" if _valid_response(original) else "Sin dato",
                ])
            story.extend([
                Paragraph("Detalle de las doce medidas del modelo", subtitle_style),
                _table(variable_rows, [152, 90, 100, 90, 88]),
                Paragraph("Los valores estimados completan datos faltantes para el c\u00e1lculo y no son respuestas registradas. Los gr\u00e1ficos y promedios usan solo valores originales medidos.", small_style),
            ])
        else:
            story.append(Paragraph("No hay un resultado V8 disponible para esta sesi\u00f3n.", small_style))

        answers = [["Pregunta", "Respuesta registrada", "Escena", "Fecha / hora"]]
        for answer in parser.get_answers() or []:
            if not isinstance(answer, Mapping):
                continue
            answers.append([
                cell(answer.get("textoPregunta") or answer.get("idPregunta")),
                cell(answer.get("respuestaLabel")),
                cell(answer.get("escena")),
                cell(answer.get("timestamp")),
            ])
        if len(answers) == 1:
            answers.append(["-", "No se registraron respuestas.", "-", "-"])
        story.extend([
            Paragraph(f"Respuestas registradas ({answer_counts[index - 1]})", subtitle_style),
            _table(answers, [190, 124, 90, 116]),
        ])

        events = [["Evento registrado", "Fase", "Fecha / hora"]]
        for event in parser.get_events() or []:
            if not isinstance(event, Mapping):
                continue
            events.append([cell(event.get("nombre")), cell(event.get("fase")), cell(event.get("timestamp"))])
        if len(events) == 1:
            events.append(["-", "No se registraron eventos.", "-"])
        story.extend([
            Spacer(1, 6),
            Paragraph(f"Eventos registrados ({event_counts[index - 1]})", subtitle_style),
            _table(events, [220, 120, 180]),
        ])

        activities = raw_session.get("actividades") if isinstance(raw_session.get("actividades"), list) else []
        if activities:
            activity_rows = [["Actividad", "Estado", "Fecha / hora"]]
            for activity in activities:
                if not isinstance(activity, Mapping):
                    activity_rows.append([cell(activity), "-", "-"])
                    continue
                activity_rows.append([
                    cell(activity.get("nombre") or activity.get("name") or activity.get("tipo") or activity.get("type")),
                    cell(activity.get("estado") or activity.get("status")),
                    cell(activity.get("timestamp") or activity.get("fecha") or activity.get("fechaInicio")),
                ])
            story.extend([
                Spacer(1, 6),
                Paragraph(f"Actividades registradas ({len(activities)})", subtitle_style),
                _table(activity_rows, [220, 120, 180]),
            ])

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=letter, leftMargin=42, rightMargin=42,
        topMargin=42, bottomMargin=42,
        title=f"Seguimiento {tracking.get('tracking_code') or tracking_id}",
        author="EmotionLab",
    )

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D8E1EA"))
        canvas.setLineWidth(0.5)
        canvas.line(doc.leftMargin, 29, letter[0] - doc.rightMargin, 29)
        canvas.setFont("EmotionLabVera", 7)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawString(doc.leftMargin, 18, f"Seguimiento {tracking.get('tracking_code') or ''}")
        canvas.drawRightString(letter[0] - doc.rightMargin, 18, f"P\u00e1gina {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
