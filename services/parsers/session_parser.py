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
from datetime import datetime
from typing import Any, Mapping, Optional

from core.exceptions import SessionParseError


class SessionParser:
    """Transforma un registro AWS en campos de dominio estables."""

    DEFAULT_AGE = 15
    DEFAULT_GENDER = "Prefiero no decir"

    DATE_FORMATS = (
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

    EVENTOS_FINALIZACION = {
        "session_completed",
        "session_complete",
        "session_finished",
        "session_finish",
        "session_ended",
        "session_end",
        "game_completed",
        "game_finished",
    }

    ESCENAS_EXCLUIDAS = {
        "waiting_room_inicial",
        "waiting_room",
        "retroalimentacion",
        "retroalimentacion_final",
    }

    ESCENAS_VALIDAS = {
        "aula",
        "oficina",
        "consulta_virtual",
    }

    GENDER_MAPPING = {
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

    DIFFICULTY_MAPPING = {
        "facil": "facil",
        "fácil": "facil",
        "medio": "medio",
        "dificil": "dificil",
        "difícil": "dificil",
    }

    def __init__(self, record: Mapping[str, Any]):
        if not isinstance(record, Mapping):
            raise SessionParseError("El registro debe ser un objeto JSON")

        self.record = dict(record)
        session_data = self.record.get("session_data", {})

        if not isinstance(session_data, Mapping):
            raise SessionParseError("session_data debe ser un objeto JSON")

        self.session = dict(session_data)
        self.answers = self.session.get("respuestas", [])
        self.events = self.session.get("eventos", [])

        if not isinstance(self.answers, list):
            raise SessionParseError("respuestas debe ser una lista")

        if not isinstance(self.events, list):
            raise SessionParseError("eventos debe ser una lista")

        # Cachés en memoria por instancia
        self._cached_start_date: Optional[datetime] = None
        self._start_date_computed = False

        self._cached_end_date: Optional[datetime] = None
        self._end_date_computed = False

        self._cached_last_activity: Optional[datetime] = None
        self._last_activity_computed = False

        self._answers_map: Optional[dict] = None

    # =========================================================
    # FECHAS
    # =========================================================

    def _parse_date(self, value) -> Optional[datetime]:
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

        value_str = str(value).strip()
        if not value_str:
            return None

        # Intento rápido con fromisoformat (estándar ISO moderno)
        clean_iso = value_str.rstrip("Z")
        if "+" in clean_iso:
            clean_iso = clean_iso.split("+")[0]

        try:
            return datetime.fromisoformat(clean_iso)
        except ValueError:
            pass

        for fmt in self.DATE_FORMATS:
            try:
                return datetime.strptime(clean_iso, fmt)
            except ValueError:
                continue

        return None

    def get_start_date(self) -> Optional[datetime]:
        """Obtiene la fecha de inicio de la sesión con caché en memoria."""
        if self._start_date_computed:
            return self._cached_start_date

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

        # 1. Buscar en session_data
        for key in keys_to_check:
            parsed = self._parse_date(self.session.get(key))
            if parsed is not None:
                self._cached_start_date = parsed
                self._start_date_computed = True
                return parsed

        # 2. Buscar en la raíz del record
        for key in keys_to_check:
            parsed = self._parse_date(self.record.get(key))
            if parsed is not None:
                self._cached_start_date = parsed
                self._start_date_computed = True
                return parsed

        # 3. Fallback al timestamp del primer evento o respuesta
        for item in (*self.events, *self.answers):
            if isinstance(item, Mapping):
                timestamp = item.get("timestamp") or item.get("fecha") or item.get("date")
                parsed = self._parse_date(timestamp)
                if parsed is not None:
                    self._cached_start_date = parsed
                    self._start_date_computed = True
                    return parsed

        self._cached_start_date = None
        self._start_date_computed = True
        return None

    def _get_completed_event_timestamp(self) -> Optional[datetime]:
        """Busca un evento que indique que la sesión terminó."""
        for event in reversed(self.events):
            if not isinstance(event, Mapping):
                continue

            event_name = str(
                event.get("nombre")
                or event.get("type")
                or event.get("evento")
                or ""
            ).strip().lower()

            if event_name not in self.EVENTOS_FINALIZACION:
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

    def get_end_date(self) -> Optional[datetime]:
        """Obtiene la fecha de término con caché."""
        if self._end_date_computed:
            return self._cached_end_date

        keys_fin = ("fechaFin", "fecha_fin", "end_time", "endTime")
        for key in keys_fin:
            fecha_fin = self._parse_date(self.session.get(key))
            if fecha_fin is not None:
                self._cached_end_date = fecha_fin
                self._end_date_computed = True
                return fecha_fin

        completed = self._get_completed_event_timestamp()
        if completed is not None:
            self._cached_end_date = completed
            self._end_date_computed = True
            return completed

        last_act = self._get_last_activity_timestamp()
        self._cached_end_date = last_act
        self._end_date_computed = True
        return last_act

    def _get_last_activity_timestamp(self) -> Optional[datetime]:
        """Obtiene la última actividad válida para estimar el fin."""
        if self._last_activity_computed:
            return self._cached_last_activity

        timestamps = []
        start = self.get_start_date()
        for item in (*self.events, *self.answers):
            if not isinstance(item, Mapping):
                continue
            timestamp = item.get("timestamp") or item.get("fecha") or item.get("date")
            parsed = self._parse_date(timestamp)
            if parsed is not None and (start is None or parsed > start):
                timestamps.append(parsed)

        result = max(timestamps) if timestamps else None
        self._cached_last_activity = result
        self._last_activity_computed = True
        return result

    def has_real_end_date(self) -> bool:
        return (
            self._parse_date(self.session.get("fechaFin")) is not None
            or self._parse_date(self.session.get("fecha_fin")) is not None
            or self._get_completed_event_timestamp() is not None
        )

    def has_estimated_end_date(self) -> bool:
        return not self.has_real_end_date() and self._get_last_activity_timestamp() is not None

    # =========================================================
    # DURACIÓN
    # =========================================================

    def _calculate_duration(self) -> int:
        start = self.get_start_date()
        end = self.get_end_date()

        if start is None:
            raise ValueError("Fecha de inicio inexistente")

        if end is None:
            raise ValueError("La sesión aún no terminó.")

        seconds = int((end - start).total_seconds())

        if seconds <= 0:
            raise ValueError("Duración inválida.")

        return seconds

    def get_duration(self) -> int:
        return self._calculate_duration()

    # =========================================================
    # RESPUESTAS
    # =========================================================

    def _get_answers_map(self) -> dict:
        if self._answers_map is None:
            mapping = {}
            for answer in self.answers:
                if isinstance(answer, Mapping):
                    qid = str(answer.get("idPregunta", "")).strip().lower()
                    if qid:
                        mapping[qid] = answer.get("respuestaLabel")
            self._answers_map = mapping
        return self._answers_map

    def _answer(self, question: str):
        return self._get_answers_map().get(str(question).strip().lower())

    # =========================================================
    # EDAD
    # =========================================================

    def get_age(self) -> int:
        age = self._answer("edad")

        if age is None:
            return self.DEFAULT_AGE

        age_digits = re.sub(r"\D", "", str(age))

        if not age_digits:
            return self.DEFAULT_AGE

        return int(age_digits)

    # =========================================================
    # GÉNERO
    # =========================================================

    def _normalize_gender(self, value) -> str:
        if value is None:
            return self.DEFAULT_GENDER

        normalized = str(value).strip()
        return self.GENDER_MAPPING.get(normalized, self.DEFAULT_GENDER)

    def get_gender(self) -> str:
        gender = self._answer("genero")

        if gender is None:
            gender = self.session.get("genero")

        if gender is None or not str(gender).strip():
            return self.DEFAULT_GENDER

        return self._normalize_gender(gender)

    # =========================================================
    # DIFICULTAD
    # =========================================================

    def _normalize_difficulty(self, value) -> str:
        if value is None:
            raise ValueError("Dificultad inexistente")

        val_cleaned = str(value).strip().lower()
        if val_cleaned not in self.DIFFICULTY_MAPPING:
            raise ValueError(f"Dificultad inválida: {value}")

        return self.DIFFICULTY_MAPPING[val_cleaned]

    def get_difficulty(self) -> str:
        return self._normalize_difficulty(self.session.get("difficulty_level"))

    # =========================================================
    # HORA
    # =========================================================

    def get_hour(self) -> int:
        start_date = self.get_start_date()

        if start_date is None:
            raise ValueError("Fecha de inicio inexistente")

        return start_date.hour

    # =========================================================
    # DÍA DE LA SEMANA
    # =========================================================

    def get_weekday(self) -> int:
        start_date = self.get_start_date()

        if start_date is None:
            raise ValueError("Fecha de inicio inexistente")

        return start_date.weekday()

    # =========================================================
    # INTENTOS
    # =========================================================

    def get_attempts(self) -> int:
        return 1

    # =========================================================
    # ESCENA
    # =========================================================

    def get_scene(self) -> str:
        for answer in self.answers:
            if not isinstance(answer, Mapping):
                continue

            scene = answer.get("escena")
            if not scene:
                continue

            scene_str = str(scene).strip().lower()
            if scene_str in self.ESCENAS_EXCLUIDAS:
                continue

            if scene_str in self.ESCENAS_VALIDAS:
                return scene_str

        session_scene = self.session.get("escena")
        if session_scene:
            session_scene_str = str(session_scene).strip().lower()
            if session_scene_str in self.ESCENAS_VALIDAS:
                return session_scene_str

        return "consulta_virtual"

    # =========================================================
    # FEATURES
    # =========================================================

    def get_prediction_features(self) -> dict:
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

    def get_prediction_missing_fields(self) -> list[str]:
        missing = []

        if self.get_start_date() is None:
            missing.append("fechaInicio")

        if self.get_end_date() is None:
            missing.append("fechaFin/session_completed/actividad")

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

    def can_predict(self) -> bool:
        return len(self.get_prediction_missing_fields()) == 0

    def get_prediction_status(self) -> dict:
        missing = self.get_prediction_missing_fields()

        if missing:
            return {
                "can_predict": False,
                "missing_fields": missing,
                "reason": "Faltan datos requeridos para ejecutar la predicción",
            }

        return {
            "can_predict": True,
            "missing_fields": [],
            "reason": None,
        }

    # =========================================================
    # DASHBOARD
    # =========================================================

    def get_dashboard_summary(self) -> dict:
        start_date = self.get_start_date()
        end_date = self.get_end_date()

        return {
            "fecha": start_date.strftime("%Y-%m-%d") if start_date else None,
            "hora": start_date.strftime("%H:%M") if start_date else None,
            "edad": self._optional_age(),
            "genero": self._optional_gender(),
            "difficulty": self._optional_difficulty(),
            "duracion": self._optional_duration(end_date),
            "duracion_estimada": self.has_estimated_end_date(),
        }

    # =========================================================
    # MÉTODOS AUXILIARES
    # =========================================================

    def get_answers(self) -> list:
        return list(self.answers)

    def get_events(self) -> list:
        return list(self.events)

    def get_questionnaire(self) -> dict:
        return {"respuestas": self.get_answers()}

    def get_features(self) -> dict:
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