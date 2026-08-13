"""Persistencia de sesiones en Firestore.

Este servicio no interpreta JSON ni calcula predicciones: recibe datos ya
normalizados desde ImportService y solamente administra la coleccion sessions.
"""

from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping, Optional

from core.constants import COLLECTION_SESSIONS
from core.exceptions import SessionNotFound

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
    ) -> dict:
        """Crea una sesion con campos indexables y conserva su JSON original."""
        self._validate_status(status)
        if not isinstance(raw_json, Mapping):
            raise ValueError("raw_json debe ser un objeto JSON")
        if prediction is not None and not isinstance(prediction, Mapping):
            raise ValueError("prediction debe ser un objeto")
        fecha, hora = self._normalise_date_time(fecha, hora, fecha_inicio)

        data = {
            "session_id": session_id,
            "nombre": nombre.strip() if isinstance(nombre, str) and nombre.strip() else None,
            "observaciones": observaciones,
            "source_key": source_key,
            "prediction": deepcopy(dict(prediction)) if prediction else None,
            "status": status,
            "estado": status,
            # Son los campos de consulta del dominio. El detalle completo se
            # conserva en raw_json; no es necesario abrirlo para el dashboard.
            "fecha": fecha,
            "hora": hora,
            "edad": edad,
            "genero": genero,
            "difficulty": difficulty,
            "raw_json": deepcopy(dict(raw_json)),
            "created_at": self.firebase.server_timestamp(),
            "updated_at": self.firebase.server_timestamp(),
        }
        self.firebase.create_document(self.collection, session_id, data)
        return data

    def get_session(self, session_id: str) -> Optional[dict]:
        return self.firebase.get_document(self.collection, session_id)

    def get_session_by_source_key(self, source_key: str) -> Optional[dict]:
        """Localiza un archivo AWS ya importado, sin comparar sesiones entre si."""
        if not source_key:
            return None
        matches = self.firebase.query_documents(
            collection=self.collection, field="source_key", operator="==", value=source_key
        )
        return matches[0] if matches else None

    def get_session_detail(self, session_id: str) -> Optional[dict]:
        """Retorna el documento completo, incluido su JSON original."""
        return self.get_session(session_id)

    def list_sessions(
        self,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        """Lista sesiones y aplica filtros sobre los campos indexables."""
        if status is not None:
            self._validate_status(status)
            sessions = self.firebase.query_documents(
                collection=self.collection,
                field="status",
                operator="==",
                value=status,
            )
        else:
            sessions = self.firebase.list_documents(self.collection)

        filtered = [
            session
            for session in sessions
            if self._is_in_date_range(self._get_date(session), date_from, date_to)
        ]
        return sorted(filtered, key=self._date_time_sort_key, reverse=True)

    def list_sessions_by_date(
        self, fecha: str
    ) -> list[dict]:
        """Lista las sesiones de un dia concreto para el detalle del dashboard."""
        self._validate_date(fecha)
        return [session for session in self.list_sessions() if self._get_date(session) == fecha]

    def list_dates(self) -> list[str]:
        """Devuelve los dias con sesiones, ordenados del mas reciente al mas antiguo."""
        return sorted(
            {date for session in self.list_sessions() if (date := self._get_date(session))},
            reverse=True,
        )

    def list_pending_sessions(self) -> list[dict]:
        """Devuelve sesiones que necesitan prediccion o asignacion."""
        pending = []
        for status in (STATUS_PENDING_PREDICTION, STATUS_PENDING_ASSIGNMENT):
            pending.extend(self.list_sessions(status=status))
        return sorted(pending, key=self._date_time_sort_key, reverse=True)

    def list_unassigned_sessions(self) -> list[dict]:
        """Sesiones que aun requieren una asignacion manual de participante."""
        return [
            session
            for session in self.list_sessions()
            if not session.get("nombre")
            and session.get("estado", session.get("status")) == STATUS_PENDING_IDENTIFICATION
        ]

    def list_pending_assignment(self) -> list[dict]:
        """Lista sesiones con prediccion que aun no tienen participante."""
        return self.list_unassigned_sessions()

    def assign_participant(self, session_id: str, participant_id: str) -> None:
        """Compatibilidad: la identificacion ahora se guarda como nombre."""
        self.update_name(session_id, participant_id)

    def update_name(self, session_id: str, nombre: str) -> None:
        """Identifica manualmente una sesion sin relacionarla a otra coleccion."""
        if not isinstance(nombre, str) or not nombre.strip():
            raise ValueError("nombre debe ser texto no vacio")
        if not self.get_session(session_id):
            raise SessionNotFound("La sesion no existe")
        self.firebase.update_document(
            self.collection,
            session_id,
            {
                "nombre": nombre.strip(),
                "status": STATUS_IDENTIFIED,
                "estado": STATUS_IDENTIFIED,
                "updated_at": self.firebase.server_timestamp(),
            },
        )

    def update_session(self, session_id: str, data: Mapping[str, Any]) -> None:
        """Actualiza los datos manuales permitidos por el profesor."""
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
            changes["nombre"] = nombre.strip() if isinstance(nombre, str) else None
            changes["status"] = (
                STATUS_IDENTIFIED if changes["nombre"] else STATUS_PENDING_IDENTIFICATION
            )
            changes["estado"] = changes["status"]
        if "observaciones" in changes and changes["observaciones"] is not None:
            if not isinstance(changes["observaciones"], str):
                raise ValueError("observaciones debe ser texto o null")
        if not self.get_session(session_id):
            raise SessionNotFound("La sesion no existe")
        changes["updated_at"] = self.firebase.server_timestamp()
        self.firebase.update_document(self.collection, session_id, changes)

    def update_prediction(self, session_id: str, prediction: Mapping[str, Any]) -> None:
        """Guarda una prediccion calculada y deja la sesion lista para asignar."""
        if not isinstance(prediction, Mapping):
            raise ValueError("prediction debe ser un objeto")
        self.firebase.update_document(
            self.collection,
            session_id,
            {
                "prediction": deepcopy(dict(prediction)),
                "status": STATUS_PENDING_IDENTIFICATION,
                "estado": STATUS_PENDING_IDENTIFICATION,
                "updated_at": self.firebase.server_timestamp(),
            },
        )

    def update_status(self, session_id: str, status: str) -> None:
        """Actualiza el estado explicito del flujo de procesamiento."""
        self._validate_status(status)
        self.firebase.update_document(
            self.collection,
            session_id,
            {"status": status, "estado": status, "updated_at": self.firebase.server_timestamp()},
        )

    def search_by_name(self, nombre: str) -> list[dict]:
        """Obtiene el historial por el nombre escrito manualmente por el profesor."""
        if not isinstance(nombre, str) or not nombre.strip():
            return []
        return self.firebase.query_documents(
            collection=self.collection, field="nombre", operator="==", value=nombre.strip()
        )

    def delete_session(self, session_id: str) -> None:
        self.firebase.delete_document(self.collection, session_id)

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_SESSION_STATUSES:
            raise ValueError(f"Estado de sesion no valido: {status}")

    @staticmethod
    def _is_in_date_range(
        fecha: Optional[str], date_from: Optional[str], date_to: Optional[str]
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
        fecha: Optional[str], hora: Optional[str], fecha_inicio: Optional[str]
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

    @staticmethod
    def _validate_date(fecha: str) -> None:
        try:
            datetime.fromisoformat(fecha).date()
        except (TypeError, ValueError) as error:
            raise ValueError("fecha debe usar formato ISO valido") from error

    @staticmethod
    def _get_date(session: Mapping[str, Any]) -> Optional[str]:
        # Compatibilidad de lectura para documentos anteriores a esta migracion.
        return session.get("fecha") or (session.get("fecha_inicio") or "")[:10] or None

    @classmethod
    def _date_time_sort_key(cls, session: Mapping[str, Any]) -> str:
        return f"{cls._get_date(session) or ''}T{session.get('hora') or ''}"
