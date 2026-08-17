"""Persistencia de sesiones en Firestore.

Este servicio administra la colección de sesiones y mantiene compatibilidad
con sesiones antiguas cuya fecha puede estar únicamente dentro de raw_json.
"""

from copy import deepcopy
from datetime import datetime
import re
from typing import TYPE_CHECKING, Any, Mapping, Optional

from core.constants import COLLECTION_SESSIONS
from core.exceptions import SessionNotFound
from services.cache_service import (
    IMPORT_INDEX_CACHE,
    SESSION_CACHE,
    invalidate_sessions_cache,
)

if TYPE_CHECKING:
    from services.firebase_service import FirebaseService


STATUS_IMPORTED = "imported"
STATUS_PENDING_PREDICTION = "pending_prediction"
STATUS_PREDICTED = "predicted"
STATUS_PENDING_ASSIGNMENT = "pending_assignment"
STATUS_COMPLETED = "completed"
STATUS_IMPORT_ERROR = "import_error"
STATUS_PENDING_IDENTIFICATION = "pendiente_identificacion"
STATUS_IDENTIFIED = "identificada"

VALID_SESSION_STATUSES = frozenset(
    {
        STATUS_IMPORTED,
        STATUS_PENDING_PREDICTION,
        STATUS_PREDICTED,
        STATUS_PENDING_ASSIGNMENT,
        STATUS_COMPLETED,
        STATUS_IMPORT_ERROR,
        STATUS_PENDING_IDENTIFICATION,
        STATUS_IDENTIFIED,
    }
)

# Regex precompiladas para optimizar el rendimiento en loops y queries
_RE_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
_RE_TIME_HM = re.compile(r"^(\d{2}):(\d{2})")
_RE_DATETIME_TIME = re.compile(r"(?:T|\s)(\d{2}):(\d{2})")
_RE_S3_DATE = re.compile(r"(?:^|/)(\d{4}-\d{2}-\d{2})/")
_RE_S3_TIME = re.compile(r"_(\d{8})(?:_[^.]+)?\.json$")


class SessionService:
    """Encapsula las operaciones CRUD de documentos de sesiones."""

    def __init__(self, firebase_service: Optional["FirebaseService"] = None):
        if firebase_service is None:
            from services.firebase_service import firebase
            firebase_service = firebase

        self.firebase = firebase_service
        self.collection = COLLECTION_SESSIONS

    def create_session(
        self,
        session_id: str,
        raw_json: Mapping[str, Any],
        *,
        nombre: Optional[str] = None,
        observaciones: Optional[str] = None,
        source_key: Optional[str] = None,
        prediction: Optional[Mapping[str, Any]] = None,
        status: str = STATUS_PENDING_IDENTIFICATION,
        fecha: Optional[str] = None,
        hora: Optional[str] = None,
        fecha_inicio: Optional[str] = None,
        fecha_fin: Optional[str] = None,
        edad: Optional[int] = None,
        genero: Optional[str] = None,
        difficulty: Optional[str] = None,
        duracion_estimada: bool = False,
    ) -> dict:

        self._validate_status(status)

        if not isinstance(raw_json, Mapping):
            raise ValueError("raw_json debe ser un objeto JSON")

        if prediction is not None and not isinstance(prediction, Mapping):
            raise ValueError("prediction debe ser un objeto")

        # ==========================================================
        # FECHA Y HORA
        # ==========================================================
        #
        # Si no vienen normalizadas, intentamos obtenerlas
        # directamente desde el JSON original / s3_key.
        #
        # ==========================================================

        fecha, hora = self._normalise_date_time(fecha, hora, fecha_inicio)

        if not fecha:
            fecha = self._get_date_from_raw_json(raw_json, source_key)

        if not hora:
            hora = self._get_time_from_raw_json(raw_json, source_key)

        data = {
            "session_id": session_id,
            "nombre": nombre.strip() if isinstance(nombre, str) and nombre.strip() else None,
            "observaciones": observaciones,
            "source_key": source_key,
            "prediction": deepcopy(dict(prediction)) if prediction else None,
            "status": status,
            "estado": status,
            "fecha": fecha,
            "hora": hora,
            "edad": edad,
            "genero": genero,
            "difficulty": difficulty,
            "duracion_estimada": bool(duracion_estimada),
            "raw_json": deepcopy(dict(raw_json)),
            "created_at": self.firebase.server_timestamp(),
            "updated_at": self.firebase.server_timestamp(),
        }

        self.firebase.create_document(self.collection, session_id, data)
        invalidate_sessions_cache()
        return data

    def get_session(self, session_id: str) -> Optional[dict]:
        return self.firebase.get_document(self.collection, session_id)

    def get_session_by_source_key(self, source_key: str) -> Optional[dict]:
        if not source_key:
            return None

        matches = self.firebase.query_documents(
            collection=self.collection,
            field="source_key",
            operator="==",
            value=source_key
        )
        return matches[0] if matches else None

    def build_import_index(self) -> tuple[dict[str, dict], dict[str, dict]]:
        cache_key = ("import_index",)
        cached = IMPORT_INDEX_CACHE.get(cache_key)

        if isinstance(cached, tuple) and len(cached) == 2:
            by_source_key, by_session_id = cached
            if isinstance(by_source_key, dict) and isinstance(by_session_id, dict):
                return dict(by_source_key), dict(by_session_id)

        by_source_key: dict[str, dict] = {}
        by_session_id: dict[str, dict] = {}

        for session in self.firebase.list_documents(self.collection):
            summary = {
                "id": session.get("id"),
                "session_id": session.get("session_id") or session.get("id"),
                "source_key": session.get("source_key"),
                "status": session.get("status") or session.get("estado"),
                "estado": session.get("estado") or session.get("status"),
            }

            if summary["source_key"]:
                by_source_key[summary["source_key"]] = summary

            if summary["session_id"]:
                by_session_id[summary["session_id"]] = summary

        IMPORT_INDEX_CACHE.set(cache_key, (dict(by_source_key), dict(by_session_id)))
        return by_source_key, by_session_id

    def get_session_detail(self, session_id: str) -> Optional[dict]:
        return self.get_session(session_id)

    def list_sessions(
        self,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:

        if status is not None:
            self._validate_status(status)

        cache_key = ("sessions", status, date_from, date_to)
        cached = SESSION_CACHE.get(cache_key)

        if isinstance(cached, list):
            return [dict(item) for item in cached]

        if status is not None:
            sessions = self.firebase.query_documents(
                collection=self.collection,
                field="status",
                operator="==",
                value=status,
            )
        else:
            sessions = self.firebase.list_documents(self.collection)

        filtered = [
            session for session in sessions
            if self._is_in_date_range(self._get_date(session), date_from, date_to)
        ]

        result = sorted(filtered, key=self._date_time_sort_key, reverse=True)
        SESSION_CACHE.set(cache_key, [dict(item) for item in result])
        return result

    def list_sessions_by_date(self, fecha: str) -> list[dict]:
        """
        Lista todas las sesiones de un día.

        Combina:
        - sesiones con fecha normalizada (query directo, barato)
        - sesiones antiguas con fecha_inicio / raw_json / s3_key

        Optimizaciones respecto a la versión anterior:
        1. No recorre TODA la colección con list_documents() directamente;
           utiliza list_sessions() que se sirve de SESSION_CACHE (~20s TTL)
           y a su vez filtra por status cuando es posible.
        2. Cachea este resultado por fecha para no repetir lecturas si el
           usuario abre/cierra el listado varias veces en pocos segundos.
        """
        self._validate_date(fecha)

        # ==========================================================
        # CACHÉ ESPECÍFICO POR FECHA
        # ==========================================================
        cache_key = ("sessions_by_date", fecha)
        cached = SESSION_CACHE.get(cache_key)
        if isinstance(cached, list):
            return [dict(item) for item in cached]

        # ==========================================================
        # 1. RESULTADOS DIRECTOS (sesiones modernas con campo 'fecha')
        # ==========================================================
        exact_match = self.firebase.query_documents(
            collection=self.collection,
            field="fecha",
            operator="==",
            value=fecha,
        )

        combined: dict[str, dict] = {
            self._session_unique_key(session): session for session in exact_match
        }

        # ==========================================================
        # 2. COMPATIBILIDAD HACIA ATRÁS
        #    (sesiones antiguas con fecha dentro de raw_json / source_key)
        #
        #    Reutilizamos list_sessions() en lugar de list_documents()
        #    para aprovechar SESSION_CACHE y evitar una lectura full
        #    duplicada de la colección si este listado ya se consultó
        #    hace poco (ej. panel principal).
        # ==========================================================
        all_sessions = self.list_sessions()

        for session in all_sessions:
            if self._get_date(session) == fecha:
                combined[self._session_unique_key(session)] = session

        result = sorted(combined.values(), key=self._date_time_sort_key, reverse=True)
        SESSION_CACHE.set(cache_key, [dict(item) for item in result], ttl_seconds=60)
        return result

    def list_dates(self) -> list[str]:
        return sorted(
            {date for session in self.list_sessions() if (date := self._get_date(session))},
            reverse=True
        )

    def list_pending_sessions(self) -> list[dict]:
        try:
            pending = self.firebase.query_documents(
                collection=self.collection,
                field="status",
                operator="in",
                value=[STATUS_PENDING_PREDICTION, STATUS_PENDING_ASSIGNMENT],
            )
        except Exception:
            pending = []
            for status in (STATUS_PENDING_PREDICTION, STATUS_PENDING_ASSIGNMENT):
                pending.extend(self.list_sessions(status=status))

        return sorted(pending, key=self._date_time_sort_key, reverse=True)

    def list_unassigned_sessions(self) -> list[dict]:
        return [
            session for session in self.list_sessions()
            if not session.get("nombre")
            and session.get("estado", session.get("status")) == STATUS_PENDING_IDENTIFICATION
        ]

    def list_pending_assignment(self) -> list[dict]:
        return self.list_unassigned_sessions()

    def assign_participant(self, session_id: str, participant_id: str) -> None:
        self.update_name(session_id, participant_id)

    def update_name(self, session_id: str, nombre: str) -> None:
        if not isinstance(nombre, str) or not nombre.strip():
            raise ValueError("nombre debe ser texto no vacio")

        payload = {
            "nombre": nombre.strip(),
            "status": STATUS_IDENTIFIED,
            "estado": STATUS_IDENTIFIED,
            "updated_at": self.firebase.server_timestamp(),
        }

        try:
            self.firebase.update_document(self.collection, session_id, payload)
            invalidate_sessions_cache()
        except Exception as error:
            message = str(error).lower()
            if "not found" in message or "no existe" in message or "document" in message:
                raise SessionNotFound("La sesion no existe") from error
            raise

    def update_session(self, session_id: str, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping):
            raise ValueError("data debe ser un objeto")

        allowed = {"nombre", "observaciones"}
        changes = {key: data[key] for key in allowed if key in data}

        if not changes:
            raise ValueError("Solo se pueden actualizar nombre u observaciones")

        if "nombre" in changes:
            nombre = changes["nombre"]
            if nombre is not None and (not isinstance(nombre, str) or not nombre.strip()):
                raise ValueError("nombre debe ser texto no vacio o null")

            clean_name = nombre.strip() if isinstance(nombre, str) else None
            status_val = STATUS_IDENTIFIED if clean_name else STATUS_PENDING_IDENTIFICATION
            changes["nombre"] = clean_name
            changes["status"] = status_val
            changes["estado"] = status_val

        if "observaciones" in changes and changes["observaciones"] is not None:
            if not isinstance(changes["observaciones"], str):
                raise ValueError("observaciones debe ser texto o null")

        changes["updated_at"] = self.firebase.server_timestamp()

        try:
            self.firebase.update_document(self.collection, session_id, changes)
            invalidate_sessions_cache()
        except Exception as error:
            message = str(error).lower()
            if "not found" in message or "no existe" in message or "document" in message:
                raise SessionNotFound("La sesion no existe") from error
            raise

    def update_prediction(self, session_id: str, prediction: Mapping[str, Any]) -> None:
        if not isinstance(prediction, Mapping):
            raise ValueError("prediction debe ser un objeto")

        self.firebase.update_document(
            self.collection,
            session_id,
            {
                "prediction": deepcopy(dict(prediction)),
                "status": STATUS_PENDING_IDENTIFICATION,
                "estado": STATUS_PENDING_IDENTIFICATION,
                "prediction_error": None,
                "prediction_missing_fields": [],
                "updated_at": self.firebase.server_timestamp(),
            },
        )
        invalidate_sessions_cache()

    def update_status(self, session_id: str, status: str) -> None:
        self._validate_status(status)
        self.firebase.update_document(
            self.collection,
            session_id,
            {
                "status": status,
                "estado": status,
                "updated_at": self.firebase.server_timestamp(),
            },
        )
        invalidate_sessions_cache()

    def search_by_name(self, nombre: str) -> list[dict]:
        if not isinstance(nombre, str) or not nombre.strip():
            return []

        return self.firebase.query_documents(
            collection=self.collection,
            field="nombre",
            operator="==",
            value=nombre.strip()
        )

    def delete_session(self, session_id: str) -> None:
        self.firebase.delete_document(self.collection, session_id)
        invalidate_sessions_cache()

    # ==========================================================
    # VALIDACIONES
    # ==========================================================

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_SESSION_STATUSES:
            raise ValueError(f"Estado de sesion no valido: {status}")

    @staticmethod
    def _validate_date(fecha: str) -> None:
        try:
            datetime.fromisoformat(fecha).date()
        except (TypeError, ValueError) as error:
            raise ValueError("fecha debe usar formato ISO valido") from error

    @staticmethod
    def _is_in_date_range(
        fecha: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str]
    ) -> bool:
        if not date_from and not date_to:
            return True
        if not fecha:
            return False

        try:
            session_date = datetime.fromisoformat(fecha).date()
            start_date = datetime.fromisoformat(date_from).date() if date_from else None
            end_date = datetime.fromisoformat(date_to).date() if date_to else None
        except (TypeError, ValueError) as error:
            raise ValueError("Las fechas deben usar formato ISO valido") from error

        return (start_date is None or session_date >= start_date) and (
            end_date is None or session_date <= end_date
        )

    @staticmethod
    def _normalise_date_time(
        fecha: Optional[str],
        hora: Optional[str],
        fecha_inicio: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:

        if fecha is None and fecha_inicio:
            try:
                start = datetime.fromisoformat(fecha_inicio)
            except ValueError as error:
                raise ValueError("fecha_inicio debe usar formato ISO valido") from error

            return start.date().isoformat(), start.strftime("%H:%M")

        if fecha is not None:
            SessionService._validate_date(fecha)

        if hora is not None:
            try:
                datetime.strptime(hora, "%H:%M")
            except ValueError as error:
                raise ValueError("hora debe usar formato HH:MM") from error

        return fecha, hora

    # ==========================================================
    # OBTENER FECHA
    # ==========================================================

    @classmethod
    def _get_date(cls, session: Mapping[str, Any]) -> Optional[str]:
        # ------------------------------------------------------
        # 1. CAMPO NORMALIZADO
        # ------------------------------------------------------
        if fecha := session.get("fecha"):
            return cls._clean_date(fecha)

        # ------------------------------------------------------
        # 2. fecha_inicio
        # ------------------------------------------------------
        if fecha_inicio := session.get("fecha_inicio"):
            return cls._clean_date(fecha_inicio)

        # ------------------------------------------------------
        # 3. raw_json
        # ------------------------------------------------------
        raw_json = session.get("raw_json")
        if isinstance(raw_json, Mapping):
            session_data = raw_json.get("session_data")
            if isinstance(session_data, Mapping):
                if fecha_inicio := session_data.get("fechaInicio"):
                    return cls._clean_date(fecha_inicio)

            # --------------------------------------------------
            # 4. s3_key
            # --------------------------------------------------
            if s3_key := raw_json.get("s3_key"):
                if date_from_key := cls._date_from_s3_key(s3_key):
                    return date_from_key

        # ------------------------------------------------------
        # 5. source_key
        # ------------------------------------------------------
        if source_key := session.get("source_key"):
            if date_from_key := cls._date_from_s3_key(source_key):
                return date_from_key

        return None

    @classmethod
    def _get_time(cls, session: Mapping[str, Any]) -> Optional[str]:
        # ------------------------------------------------------
        # 1. HORA NORMALIZADA
        # ------------------------------------------------------
        if hora := session.get("hora"):
            return cls._clean_time(hora)

        # ------------------------------------------------------
        # 2. fecha_inicio
        # ------------------------------------------------------
        if fecha_inicio := session.get("fecha_inicio"):
            if time_value := cls._time_from_datetime(fecha_inicio):
                return time_value

        # ------------------------------------------------------
        # 3. raw_json
        # ------------------------------------------------------
        raw_json = session.get("raw_json")
        if isinstance(raw_json, Mapping):
            session_data = raw_json.get("session_data")
            if isinstance(session_data, Mapping):
                if fecha_inicio := session_data.get("fechaInicio"):
                    if time_value := cls._time_from_datetime(fecha_inicio):
                        return time_value

            # --------------------------------------------------
            # 4. s3_key
            # --------------------------------------------------
            if s3_key := raw_json.get("s3_key"):
                if time_value := cls._time_from_s3_key(s3_key):
                    return time_value

        # ------------------------------------------------------
        # 5. source_key
        # ------------------------------------------------------
        if source_key := session.get("source_key"):
            if time_value := cls._time_from_s3_key(source_key):
                return time_value

        return None

    @classmethod
    def _get_date_from_raw_json(
        cls,
        raw_json: Mapping[str, Any],
        source_key: Optional[str]
    ) -> Optional[str]:
        if isinstance(raw_json, Mapping):
            if date_value := cls._get_date({"raw_json": raw_json}):
                return date_value

        if source_key:
            return cls._date_from_s3_key(source_key)

        return None

    @classmethod
    def _get_time_from_raw_json(
        cls,
        raw_json: Mapping[str, Any],
        source_key: Optional[str]
    ) -> Optional[str]:
        if isinstance(raw_json, Mapping):
            if time_value := cls._get_time({"raw_json": raw_json}):
                return time_value

        if source_key:
            return cls._time_from_s3_key(source_key)

        return None

    @staticmethod
    def _clean_date(value: Any) -> Optional[str]:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None

        # YYYY-MM-DD...
        return value[:10] if _RE_DATE_ISO.match(value) else None

    @staticmethod
    def _clean_time(value: Any) -> Optional[str]:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None

        match = _RE_TIME_HM.match(value)
        return f"{match.group(1)}:{match.group(2)}" if match else None

    @staticmethod
    def _time_from_datetime(value: Any) -> Optional[str]:
        if value is None:
            return None
        value = str(value).strip()
        match = _RE_DATETIME_TIME.search(value)
        return f"{match.group(1)}:{match.group(2)}" if match else None

    @staticmethod
    def _date_from_s3_key(s3_key: Any) -> Optional[str]:
        if not s3_key:
            return None
        match = _RE_S3_DATE.search(str(s3_key))
        return match.group(1) if match else None

    @staticmethod
    def _time_from_s3_key(s3_key: Any) -> Optional[str]:
        if not s3_key:
            return None
        match = _RE_S3_TIME.search(str(s3_key))
        if not match:
            return None
        value = match.group(1)
        return f"{value[0:2]}:{value[2:4]}"

    @classmethod
    def _date_time_sort_key(cls, session: Mapping[str, Any]) -> str:
        return f"{cls._get_date(session) or ''}T{cls._get_time(session) or ''}"

    @staticmethod
    def _session_unique_key(session: Mapping[str, Any]) -> str:
        if session_id := (session.get("session_id") or session.get("id")):
            return str(session_id)

        if source_key := session.get("source_key"):
            return f"source:{source_key}"

        return repr(sorted((str(k), repr(v)) for k, v in session.items()))

    def update_document_if_supported(self, session_id: str, data: dict) -> None:
        self.firebase.update_document(
            self.collection,
            session_id,
            {**data, "updated_at": self.firebase.server_timestamp()},
        )
        invalidate_sessions_cache()

    def update_session_fields(self, session_id: str, data: dict) -> None:
        self.update_document_if_supported(session_id, data)

    def update_prediction_pending_info(
        self,
        session_id: str,
        missing_fields: list[str],
    ) -> None:
        self.firebase.update_document(
            self.collection,
            session_id,
            {
                "prediction_error": "Faltan datos requeridos para ejecutar la predicción",
                "prediction_missing_fields": missing_fields,
                "updated_at": self.firebase.server_timestamp(),
            },
        )
        invalidate_sessions_cache()

    def get_session_by_session_id(self, session_id: str):
        if not session_id:
            return None
        try:
            return self.get_session(session_id)
        except Exception as error:
            print(f"Error buscando sesión por session_id {session_id}: {error}")
            return None