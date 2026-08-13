"""Lectura centralizada del JSON real de AWS para una sesion individual.

El parser no conoce Firebase ni el servicio de prediccion. Su trabajo es
normalizar el registro de entrada y exponer la informacion que consumen
Firestore, el dashboard y el modelo.
"""

import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping

from core.exceptions import SessionParseError


class SessionParser:
    """Transforma un registro AWS en campos de dominio estables."""

    def __init__(self, record: Mapping[str, Any]):
        if not isinstance(record, Mapping):
            raise SessionParseError("El registro debe ser un objeto JSON")

        self.record = deepcopy(dict(record))
        self.session = self.record.get("session_data", {})
        if not isinstance(self.session, Mapping):
            raise SessionParseError("session_data debe ser un objeto JSON")

        self.session = deepcopy(dict(self.session))
        self.answers = self.session.get("respuestas", [])
        self.events = self.session.get("eventos", [])

        if not isinstance(self.answers, list):
            raise SessionParseError("respuestas debe ser una lista")
        if not isinstance(self.events, list):
            raise SessionParseError("eventos debe ser una lista")

    def _parse_date(self, value):
        if not value:
            return None

        value = str(value).strip()
        formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        )

        for fmt in formats:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue

        return None

    def _calculate_duration(self):
        start = self.get_start_date()
        end = self.get_end_date()

        if start is None or end is None:
            raise ValueError("La sesión aún no terminó.")

        seconds = int((end - start).total_seconds())
        if seconds <= 0:
            raise ValueError("Duración inválida.")

        return seconds

    def _normalize_difficulty(self, value):
        if value is None:
            raise ValueError("Dificultad inválida")

        value = str(value).strip().lower()
        mapping = {
            "facil": "facil",
            "fácil": "facil",
            "medio": "medio",
            "dificil": "dificil",
            "difícil": "dificil",
        }

        if value not in mapping:
            raise ValueError("Dificultad inválida")

        return mapping[value]

    def _normalize_gender(self, value):
        mapping = {
            "M": "Masculino",
            "F": "Femenino",
            "NB": "No binario",
            "Masculino": "Masculino",
            "Femenino": "Femenino",
            "Hombre": "Masculino",
            "Mujer": "Femenino",
            "No binario": "No binario",
            "Prefiero no decir": "Prefiero no decir",
        }

        if value is None:
            raise ValueError("Género inválido")

        normalized = str(value).strip()
        if normalized not in mapping:
            raise ValueError("Género inválido")

        return mapping[normalized]

    def _answer(self, question):
        question = str(question).strip().lower()

        for answer in self.answers:
            current = str(answer.get("idPregunta", "")).strip().lower()
            if current == question:
                return answer.get("respuestaLabel")

        return None

    def _get_completed_event_timestamp(self):
        for event in reversed(self.events):
            if not isinstance(event, Mapping):
                continue

            event_name = str(
                event.get("nombre") or event.get("type") or event.get("evento") or ""
            ).strip().lower()
            if event_name != "session_completed":
                continue

            timestamp = event.get("timestamp") or event.get("fecha") or event.get("date")
            parsed_timestamp = self._parse_date(timestamp)
            if parsed_timestamp is not None:
                return parsed_timestamp

        return None

    def get_start_date(self):
        return self._parse_date(self.session.get("fechaInicio"))

    def get_end_date(self):
        end_date = self._parse_date(self.session.get("fechaFin"))
        if end_date is not None:
            return end_date
        return self._get_completed_event_timestamp()

    def get_age(self):
        age = self._answer("edad")

        if age is None:
            raise ValueError("Edad inexistente")

        age = re.sub(r"\D", "", str(age))
        if not age:
            raise ValueError("Edad inexistente")

        return int(age)

    def get_gender(self):
        gender = self._answer("genero")
        if gender is None:
            gender = self.session.get("genero")

        return self._normalize_gender(gender)

    def get_difficulty(self):
        return self._normalize_difficulty(self.session.get("difficulty_level"))

    def get_hour(self):
        start_date = self.get_start_date()
        if start_date is None:
            raise ValueError("Fecha de inicio inexistente")
        return start_date.hour

    def get_weekday(self):
        start_date = self.get_start_date()
        if start_date is None:
            raise ValueError("Fecha de inicio inexistente")
        return start_date.weekday()

    def get_duration(self):
        return self._calculate_duration()

    def get_attempts(self):
        return 1

    def get_scene(self):
        for answer in self.answers:
            scene = answer.get("escena")
            if not scene:
                continue

            scene = str(scene).strip()
            if scene in {"waiting_room_inicial", "retroalimentacion_final"}:
                continue
            return scene

        return self.session.get("escena") or "consulta_virtual"

    def get_prediction_features(self):
        return {
            "edad": self.get_age(),
            "hora": self.get_hour(),
            "dia_semana": self.get_weekday(),
            "tiempo_total_vr": self.get_duration(),
            "intentos": self.get_attempts(),
            "difficulty_level": self.get_difficulty(),
            "escena": self.get_scene(),
            "genero": self.get_gender(),
        }

    def get_dashboard_summary(self):
        start_date = self.get_start_date()
        end_date = self.get_end_date()

        return {
            "fecha": start_date.strftime("%Y-%m-%d") if start_date else None,
            "hora": start_date.strftime("%H:%M") if start_date else None,
            "edad": self._optional_age(),
            "genero": self._optional_gender(),
            "difficulty": self._optional_difficulty(),
            "duracion": self._optional_duration(end_date),
        }

    def can_predict(self):
        return self.get_end_date() is not None

    def get_answers(self):
        return deepcopy(self.answers)

    def get_events(self):
        return deepcopy(self.events)

    def get_questionnaire(self):
        return {"respuestas": self.get_answers()}

    def get_features(self):
        return self.get_prediction_features()

    def _optional_age(self):
        try:
            return self.get_age()
        except (TypeError, ValueError):
            return None

    def _optional_gender(self):
        try:
            return self.get_gender()
        except (TypeError, ValueError):
            return None

    def _optional_difficulty(self):
        try:
            return self.get_difficulty()
        except (TypeError, ValueError):
            return None

    def _optional_duration(self, end_date):
        if end_date is None:
            return None
        try:
            return self.get_duration()
        except (TypeError, ValueError):
            return None
