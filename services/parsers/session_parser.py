"""
Lectura centralizada del JSON real de AWS para una sesión individual.

El parser no conoce Firebase ni el servicio de predicción.
Su responsabilidad es:
- interpretar el JSON de Unity/AWS
- normalizar los datos
- obtener las features necesarias para el modelo
- indicar qué información falta cuando no puede predecir
"""

import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping

from core.exceptions import SessionParseError


class SessionParser:
    """Transforma un registro AWS en campos de dominio estables."""

    DEFAULT_AGE = 15
    DEFAULT_GENDER = "Prefiero no decir"

    def __init__(self, record: Mapping[str, Any]):
        if not isinstance(record, Mapping):
            raise SessionParseError("El registro debe ser un objeto JSON")

        self.record = deepcopy(dict(record))

        self.session = self.record.get("session_data", {})

        if not isinstance(self.session, Mapping):
            raise SessionParseError(
                "session_data debe ser un objeto JSON"
            )

        self.session = deepcopy(dict(self.session))

        self.answers = self.session.get("respuestas", [])
        self.events = self.session.get("eventos", [])

        if not isinstance(self.answers, list):
            raise SessionParseError(
                "respuestas debe ser una lista"
            )

        if not isinstance(self.events, list):
            raise SessionParseError(
                "eventos debe ser una lista"
            )

    # =========================================================
    # FECHAS
    # =========================================================

    def _parse_date(self, value):
        if value is None:
            return None

        # Soporte para epoch timestamps (segundos o milisegundos)
        if isinstance(value, (int, float)):
            try:
                if value > 1e11:  # Milisegundos
                    value = value / 1000.0
                return datetime.fromtimestamp(value)
            except (ValueError, OSError):
                return None

        value = str(value).strip()

        if not value:
            return None

        # Limpiar sufijo de zona horaria UTC si existe
        if value.endswith("Z"):
            value = value[:-1]

        # Eliminar offset de timezone simple si está presente (+00:00)
        if "+" in value:
            value = value.split("+")[0]

        formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y",
        )

        for fmt in formats:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue

        return None

    def get_start_date(self):
        """
        Obtiene la fecha de inicio de la sesión buscando en múltiples
        variantes de nombres y fallbacks de eventos.
        """
        # 1. Buscar en session_data
        keys_to_check = (
            "fechaInicio",
            "fecha_inicio",
            "fecha",
            "timestamp",
            "date",
            "start_time",
            "startTime",
            "createdAt",
            "created_at",
        )
        for key in keys_to_check:
            parsed = self._parse_date(self.session.get(key))
            if parsed is not None:
                return parsed

        # 2. Buscar en la raíz del record
        for key in keys_to_check:
            parsed = self._parse_date(self.record.get(key))
            if parsed is not None:
                return parsed

        # 3. Fallback al timestamp del primer evento o respuesta
        for item in (*self.events, *self.answers):
            if isinstance(item, Mapping):
                timestamp = item.get("timestamp") or item.get("fecha") or item.get("date")
                parsed = self._parse_date(timestamp)
                if parsed is not None:
                    return parsed

        return None

    def _get_completed_event_timestamp(self):
        """
        Busca un evento que indique que la sesión terminó.
        """
        eventos_finalizacion = {
            "session_completed",
            "session_complete",
            "session_finished",
            "session_finish",
            "session_ended",
            "session_end",
            "game_completed",
            "game_finished",
        }

        for event in reversed(self.events):

            if not isinstance(event, Mapping):
                continue

            event_name = str(
                event.get("nombre")
                or event.get("type")
                or event.get("evento")
                or ""
            ).strip().lower()

            if event_name not in eventos_finalizacion:
                continue

            timestamp = (
                event.get("timestamp")
                or event.get("fecha")
                or event.get("date")
            )

            parsed_timestamp = self._parse_date(timestamp)

            if parsed_timestamp is not None:
                return parsed_timestamp

        return None

    def get_end_date(self):
        """
        Obtiene la fecha de término.

        Prioridad:
        1. fechaFin (o variantes)
        2. evento de finalización
        3. última actividad
        """
        keys_fin = ("fechaFin", "fecha_fin", "end_time", "endTime")
        for key in keys_fin:
            fecha_fin = self._parse_date(self.session.get(key))
            if fecha_fin is not None:
                return fecha_fin

        completed = self._get_completed_event_timestamp()
        if completed is not None:
            return completed

        return self._get_last_activity_timestamp()

    def _get_last_activity_timestamp(self):
        """Obtiene la ultima actividad valida para estimar el fin."""
        timestamps = []
        start = self.get_start_date()
        for item in (*self.events, *self.answers):
            if not isinstance(item, Mapping):
                continue
            timestamp = item.get("timestamp") or item.get("fecha") or item.get("date")
            parsed = self._parse_date(timestamp)
            if parsed is not None and (start is None or parsed > start):
                timestamps.append(parsed)
        return max(timestamps) if timestamps else None

    def has_real_end_date(self):
        return (
            self._parse_date(self.session.get("fechaFin")) is not None
            or self._parse_date(self.session.get("fecha_fin")) is not None
            or self._get_completed_event_timestamp() is not None
        )

    def has_estimated_end_date(self):
        return not self.has_real_end_date() and self._get_last_activity_timestamp() is not None

    # =========================================================
    # DURACIÓN
    # =========================================================

    def _calculate_duration(self):
        start = self.get_start_date()
        end = self.get_end_date()

        if start is None:
            raise ValueError(
                "Fecha de inicio inexistente"
            )

        if end is None:
            raise ValueError(
                "La sesión aún no terminó."
            )

        seconds = int(
            (end - start).total_seconds()
        )

        if seconds <= 0:
            raise ValueError(
                "Duración inválida."
            )

        return seconds

    def get_duration(self):
        return self._calculate_duration()

    # =========================================================
    # RESPUESTAS
    # =========================================================

    def _answer(self, question):
        question = str(question).strip().lower()

        for answer in self.answers:

            if not isinstance(answer, Mapping):
                continue

            current = str(
                answer.get("idPregunta", "")
            ).strip().lower()

            if current == question:
                return answer.get("respuestaLabel")

        return None

    # =========================================================
    # EDAD
    # =========================================================

    def get_age(self):
        age = self._answer("edad")

        # Si Unity no envió edad, usamos DEFAULT_AGE como valor por defecto
        if age is None:
            return self.DEFAULT_AGE

        age = re.sub(r"\D", "", str(age))

        if not age:
            return self.DEFAULT_AGE

        age = int(age)

        return age

    # =========================================================
    # GÉNERO
    # =========================================================

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
            return self.DEFAULT_GENDER

        normalized = str(
            value
        ).strip()

        if not normalized or normalized not in mapping:
            return self.DEFAULT_GENDER

        return mapping[normalized]

    def get_gender(self):
        gender = self._answer("genero")

        if gender is None:
            gender = self.session.get("genero")

        # Si Unity no envió género, usamos la categoría neutral
        if gender is None or not str(gender).strip():
            return self.DEFAULT_GENDER

        return self._normalize_gender(gender)

    # =========================================================
    # DIFICULTAD
    # =========================================================

    def _normalize_difficulty(self, value):

        if value is None:
            raise ValueError(
                "Dificultad inexistente"
            )

        value = str(
            value
        ).strip().lower()

        mapping = {
            "facil": "facil",
            "fácil": "facil",

            "medio": "medio",

            "dificil": "dificil",
            "difícil": "dificil",
        }

        if value not in mapping:
            raise ValueError(
                f"Dificultad inválida: {value}"
            )

        return mapping[value]

    def get_difficulty(self):

        return self._normalize_difficulty(
            self.session.get(
                "difficulty_level"
            )
        )

    # =========================================================
    # HORA
    # =========================================================

    def get_hour(self):

        start_date = self.get_start_date()

        if start_date is None:
            raise ValueError(
                "Fecha de inicio inexistente"
            )

        return start_date.hour

    # =========================================================
    # DÍA DE LA SEMANA
    # =========================================================

    def get_weekday(self):

        start_date = self.get_start_date()

        if start_date is None:
            raise ValueError(
                "Fecha de inicio inexistente"
            )

        return start_date.weekday()

    # =========================================================
    # INTENTOS
    # =========================================================

    def get_attempts(self):
        return 1

    # =========================================================
    # ESCENA
    # =========================================================

    def get_scene(self):
        """
        Obtiene una escena válida para el modelo.

        Las escenas de espera y retroalimentación no representan
        la escena principal de la sesión, por lo que se ignoran.
        """

        escenas_excluidas = {
            "waiting_room_inicial",
            "waiting_room",
            "retroalimentacion",
            "retroalimentacion_final",
        }

        escenas_validas = {
            "aula",
            "oficina",
            "consulta_virtual",
        }

        # Recorremos las respuestas buscando una escena válida
        for answer in self.answers:

            if not isinstance(answer, Mapping):
                continue

            scene = answer.get("escena")

            if not scene:
                continue

            scene = str(scene).strip().lower()

            # Ignorar escenas que no representan la actividad principal
            if scene in escenas_excluidas:
                continue

            # Si encontramos una escena que el modelo conoce
            if scene in escenas_validas:
                return scene

        # Revisar la escena directamente en session_data
        session_scene = self.session.get("escena")

        if session_scene:
            session_scene = str(session_scene).strip().lower()

            if session_scene in escenas_validas:
                return session_scene

        # Si no existe una escena válida, usamos la escena
        # predeterminada del sistema.
        return "consulta_virtual"

    # =========================================================
    # FEATURES
    # =========================================================

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

    # =========================================================
    # VALIDACIÓN DE PREDICCIÓN
    # =========================================================

    def get_prediction_missing_fields(self):
        """
        Devuelve una lista con los datos que faltan para poder
        ejecutar el modelo.

        NO inventa información.
        """

        missing = []

        if self.get_start_date() is None:
            missing.append(
                "fechaInicio"
            )

        if self.get_end_date() is None:
            missing.append(
                "fechaFin/session_completed/actividad"
            )

        try:
            self.get_age()
        except ValueError:
            missing.append("edad")

        try:
            self.get_gender()
        except ValueError:
            missing.append("genero")

        try:
            self.get_difficulty()
        except ValueError:
            missing.append("difficulty_level")

        try:
            self.get_scene()
        except ValueError:
            missing.append("escena")

        return missing

    def can_predict(self):

        return len(
            self.get_prediction_missing_fields()
        ) == 0

    def get_prediction_status(self):

        missing = (
            self.get_prediction_missing_fields()
        )

        if missing:
            return {
                "can_predict": False,
                "missing_fields": missing,
                "reason": (
                    "Faltan datos requeridos "
                    "para ejecutar la predicción"
                ),
            }

        return {
            "can_predict": True,
            "missing_fields": [],
            "reason": None,
        }

    # =========================================================
    # DASHBOARD
    # =========================================================

    def get_dashboard_summary(self):

        start_date = self.get_start_date()
        end_date = self.get_end_date()

        return {
            "fecha": (
                start_date.strftime("%Y-%m-%d")
                if start_date
                else None
            ),

            "hora": (
                start_date.strftime("%H:%M")
                if start_date
                else None
            ),

            "edad": self._optional_age(),

            "genero": self._optional_gender(),

            "difficulty": self._optional_difficulty(),

            "duracion": self._optional_duration(
                end_date
            ),
            "duracion_estimada": self.has_estimated_end_date(),
        }

    # =========================================================
    # MÉTODOS AUXILIARES
    # =========================================================

    def get_answers(self):
        return deepcopy(
            self.answers
        )

    def get_events(self):
        return deepcopy(
            self.events
        )

    def get_questionnaire(self):
        return {
            "respuestas": self.get_answers()
        }

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