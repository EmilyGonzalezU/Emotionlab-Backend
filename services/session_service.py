from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import re
from typing import TYPE_CHECKING, Any, Mapping, Optional

from boto3.dynamodb.conditions import Attr, Key

from core.constants import (
    DYNAMODB_TABLE_SESSIONS,
    PK_TYPE_SESSION,
)
from core.exceptions import SessionNotFound

from services.cache_service import (
    IMPORT_INDEX_CACHE,
    SESSION_CACHE,
    invalidate_sessions_cache,
)

from services.dynamodb_service import (
    DynamoDBService,
    dynamodb,
)


# ==========================================================
# SERVICIOS OPCIONALES
# ==========================================================

try:
    from services.audit_service import (
        AuditService,
        audit_service,
    )
except Exception:  # pragma: no cover

    AuditService = None  # type: ignore

    def audit_service():
        return None


try:
    from services.s3_service import (
        S3Service,
        s3_service as _s3_service_default,
    )
except Exception:  # pragma: no cover

    S3Service = None  # type: ignore

    def _s3_service_default():
        return None


if TYPE_CHECKING:
    from services.s3_service import S3Service as _S3ServiceT


# ==========================================================
# CONSTANTES
# ==========================================================

# Partición técnica utilizada para sesiones todavía
# no asignadas a un participante.
PENDING_PARTICIPANT_ID = "__PENDING__"


# ==========================================================
# ESTADOS
# ==========================================================

STATUS_IMPORTED = "imported"

STATUS_PENDING_PREDICTION = "pending_prediction"

STATUS_PREDICTED = "predicted"

STATUS_PENDING_ASSIGNMENT = "pending_assignment"

STATUS_COMPLETED = "completed"

STATUS_IMPORT_ERROR = "import_error"

STATUS_PENDING_IDENTIFICATION = "pendiente_identificacion"

STATUS_IDENTIFIED = "identificada"

STATUS_PROCESSING_ERROR = "processing_error"


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
        STATUS_PROCESSING_ERROR,
    }
)


# ==========================================================
# RAW JSON
# ==========================================================

# DynamoDB permite items de aproximadamente 400 KB.
# Dejamos margen para el resto de atributos.
_RAW_JSON_INLINE_THRESHOLD_BYTES = 350 * 1024


# ==========================================================
# REGEX
# ==========================================================

_RE_DATE_ISO = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
)

_RE_TIME_HM = re.compile(
    r"^(\d{2}):(\d{2})"
)

_RE_DATETIME_TIME = re.compile(
    r"(?:T|\s)(\d{2}):(\d{2})"
)

_RE_S3_DATE = re.compile(
    r"(?:^|/)(\d{4}-\d{2}-\d{2})/"
)

_RE_S3_TIME = re.compile(
    r"_(\d{8})(?:_[^.]+)?\.json$"
)


# ==========================================================
# SESSION SERVICE
# ==========================================================

class SessionService:
    """
    Encapsula las operaciones CRUD de sesiones en DynamoDB.

    Tabla real:

        PK = participant_id
        SK = sk

    session_id se utiliza como identificador lógico y se consulta
    mediante el GSI gsi-sessions-sessionid.
    """

    def __init__(
        self,
        dynamodb_service: Optional[DynamoDBService] = None,
        s3_service: Optional["_S3ServiceT"] = None,
        audit_svc: Optional["AuditService"] = None,  # type: ignore
        legacy_data_service: Optional[Any] = None,
    ):

        self.dynamo: DynamoDBService = (
            dynamodb_service or dynamodb
        )

        self.table = DYNAMODB_TABLE_SESSIONS

        self.audit = (
            audit_svc
            if audit_svc is not None
            else audit_service()
        )

        self.s3 = (
            s3_service
            if s3_service is not None
            else _s3_service_default()
        )

        self.legacy_data_service = legacy_data_service


    # ======================================================
    # HELPERS S3
    # ======================================================

    def _maybe_store_raw_json_externally(
        self,
        raw_json: Mapping[str, Any],
        fecha: Optional[str],
        session_id: str,
    ) -> tuple[Any, Optional[str]]:

        inline = deepcopy(dict(raw_json))

        try:
            size_bytes = len(
                json.dumps(
                    inline,
                    default=str,
                    ensure_ascii=False,
                )
            )
        except Exception:
            size_bytes = len(repr(inline))

        # Cabe inline
        if (
            size_bytes <= _RAW_JSON_INLINE_THRESHOLD_BYTES
            or self.s3 is None
        ):
            return inline, None

        fecha_ok = (
            fecha
            or datetime.utcnow().strftime("%Y-%m-%d")
        )

        s3_key = (
            f"raw_sessions/"
            f"{fecha_ok}/"
            f"{session_id}.json"
        )

        try:

            self.s3.put_session_raw_json(
                s3_key,
                inline,
            )

            return (
                {
                    "__s3_key": s3_key,
                    "__bytes": size_bytes,
                },
                s3_key,
            )

        except Exception:

            # Si S3 falla, intentamos mantenerlo inline.
            return inline, None


    def _materialize_raw_json(
        self,
        item: Optional[Mapping[str, Any]],
    ) -> Optional[dict]:

        if not item:
            return None

        data = dict(item)

        raw = data.get("raw_json")

        if (
            isinstance(raw, dict)
            and raw.get("__s3_key")
        ):

            if self.s3 is None:

                data["raw_json"] = {}

            else:

                try:

                    data["raw_json"] = (
                        self.s3.get_session_raw_json(
                            raw["__s3_key"]
                        )
                        or {}
                    )

                except Exception:

                    data["raw_json"] = {
                        "__s3_key_remaining":
                            raw["__s3_key"]
                    }

        return data


    # ======================================================
    # RESOLVER CLAVE REAL
    # ======================================================

    def _resolve_session_key(
        self,
        session_id: str,
    ) -> Optional[dict[str, Any]]:
        """
        Resuelve la clave primaria real de DynamoDB.

        Busca por session_id utilizando:

            gsi-sessions-sessionid

        y devuelve:

            {
                "participant_id": "...",
                "sk": "..."
            }
        """

        if not session_id:
            return None

        try:

            matches = self.dynamo.query_all(
                self.table,
                Key("session_id").eq(
                    str(session_id)
                ),
                index_name="gsi-sessions-sessionid",
                max_items=1,
            )

        except Exception as error:

            print(
                "Error resolviendo clave de sesión "
                f"(tipo={type(error).__name__})"
            )

            return None

        if not matches:
            return None

        item = matches[0]

        participant_id = item.get(
            "participant_id"
        )

        sk = item.get("sk")

        if participant_id is None or sk is None:
            return None

        return {
            "participant_id": participant_id,
            "sk": sk,
        }


    # ======================================================
    # OBTENER SESIÓN
    # ======================================================

    def get_session(
        self,
        session_id: str,
    ) -> Optional[dict]:

        if not session_id:
            return None

        try:

            matches = self.dynamo.query_all(
                self.table,
                Key("session_id").eq(
                    str(session_id)
                ),
                index_name="gsi-sessions-sessionid",
                max_items=1,
            )

            if matches:

                return self._materialize_raw_json(
                    matches[0]
                )

        except Exception as error:

            print(
                "Error buscando sesión "
                f"(tipo={type(error).__name__})"
            )

        # Compatibilidad legacy
        if self.legacy_data_service:

            for session in self._list_legacy_sessions():

                if (
                    session.get("session_id")
                    == session_id
                ):

                    return session

        return None


    def get_session_by_session_id(
        self,
        session_id: str,
    ):

        return self.get_session(session_id)


    def get_session_detail(
        self,
        session_id: str,
    ) -> Optional[dict]:

        return self.get_session(session_id)


    # ======================================================
    # OBTENER POR SOURCE KEY
    # ======================================================

    def get_session_by_source_key(
        self,
        source_key: str,
    ) -> Optional[dict]:

        if not source_key:
            return None

        try:

            matches = self.dynamo.query_all(
                self.table,
                Key("source_key").eq(
                    source_key
                ),
                index_name="gsi-sessions-sourcekey",
                max_items=1,
            )

        except Exception as error:

            print(
                "Error buscando sesión por source_key "
                f"(tipo={type(error).__name__})"
            )

            return None

        if not matches:
            return None

        return self._materialize_raw_json(
            matches[0]
        )


    # ======================================================
    # CREAR SESIÓN
    # ======================================================

    def create_session(
        self,
        session_id: str,
        raw_json: Mapping[str, Any],
        *,
        nombre: Optional[str] = None,
        participant_id: Optional[str] = None,
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
        intentos: Optional[int] = None,
        duracion: Optional[Any] = None,
        actor_ctx: Optional[dict] = None,
    ) -> dict:

        # --------------------------------------------------
        # Validaciones
        # --------------------------------------------------

        self._validate_status(status)

        if not isinstance(raw_json, Mapping):

            raise ValueError(
                "raw_json debe ser un objeto JSON"
            )

        if (
            prediction is not None
            and not isinstance(prediction, Mapping)
        ):

            raise ValueError(
                "prediction debe ser un objeto"
            )

        if participant_id is not None:

            participant_id = (
                self._validate_participant_id(
                    participant_id
                )
            )

        # --------------------------------------------------
        # Fecha / hora
        # --------------------------------------------------

        fecha, hora = (
            self._normalise_date_time(
                fecha,
                hora,
                fecha_inicio,
            )
        )

        if not fecha:

            fecha = self._get_date_from_raw_json(
                raw_json,
                source_key,
            )

        if not hora:

            hora = self._get_time_from_raw_json(
                raw_json,
                source_key,
            )

        # --------------------------------------------------
        # Nombre
        # --------------------------------------------------

        clean_nombre = (
            nombre.strip()
            if isinstance(nombre, str)
            and nombre.strip()
            else None
        )

        # --------------------------------------------------
        # Estado
        # --------------------------------------------------

        if (
            participant_id
            and status != STATUS_PROCESSING_ERROR
        ):

            status = STATUS_IDENTIFIED

        # --------------------------------------------------
        # PK REAL
        # --------------------------------------------------

        storage_participant_id = (
            participant_id
            or PENDING_PARTICIPANT_ID
        )

        # --------------------------------------------------
        # Raw JSON
        # --------------------------------------------------

        raw_inline, raw_s3_key = (
            self._maybe_store_raw_json_externally(
                raw_json,
                fecha,
                session_id,
            )
        )

        # --------------------------------------------------
        # SK REAL
        # --------------------------------------------------

        date_part = (
            fecha
            or "0000-00-00"
        )

        time_part = (
            hora
            or "00:00"
        )

        # Incluimos session_id para garantizar
        # unicidad incluso si dos sesiones tienen
        # exactamente la misma fecha/hora.
        fecha_hora_sk = (
            f"{date_part}"
            f"#{time_part}"
            f"#{session_id}"
        )

        # --------------------------------------------------
        # Datos
        # --------------------------------------------------

        now = DynamoDBService.now_iso()

        data = {

            "session_id": session_id,

            "nombre": clean_nombre,

            "participant_id":
                storage_participant_id,

            "observaciones":
                observaciones,

            "prediction":
                (
                    deepcopy(dict(prediction))
                    if prediction
                    else None
                ),

            "status": status,

            "estado": status,

            "fecha": fecha,

            "hora": hora,

            "fecha_inicio":
                fecha_inicio,

            "fecha_fin":
                fecha_fin,

            "edad": edad,

            "genero": genero,

            "difficulty": difficulty,

            "duracion_estimada":
                bool(duracion_estimada),

            "intentos": intentos,

            "duracion": duracion,

            "sk": fecha_hora_sk,

            "raw_json_s3_key":
                raw_s3_key,

            "raw_json":
                raw_inline,

            "prediction_error":
                None,

            "prediction_missing_fields":
                [],

            "pk_type":
                PK_TYPE_SESSION,

            "created_at":
                now,

            "updated_at":
                now,
        }

        if source_key:

            data["source_key"] = source_key

        # --------------------------------------------------
        # Persistencia
        # --------------------------------------------------

        try:

            self.dynamo.put_item(
                self.table,
                data,
                condition_expression=
                    Attr("session_id").not_exists(),
            )

        except Exception as exc:

            if (
                hasattr(exc, "response")
                and exc.response.get(
                    "Error", {}
                ).get("Code")
                == "ConditionalCheckFailedException"
            ):

                raise ValueError(
                    "Ya existe una sesión con "
                    f"session_id={session_id}"
                )

            raise

        invalidate_sessions_cache()

        # --------------------------------------------------
        # Auditoría
        # --------------------------------------------------

        if (
            self.audit
            and actor_ctx
            and participant_id
        ):

            try:

                self.audit.log_session_assigned(
                    actor_uid=
                        actor_ctx.get(
                            "actor_uid",
                            "",
                        ),

                    actor_email=
                        actor_ctx.get(
                            "actor_email",
                            "",
                        ),

                    actor_rol=
                        actor_ctx.get(
                            "actor_rol",
                            "",
                        ),

                    session_id=
                        session_id,

                    participant_id=
                        participant_id,
                )

            except Exception:
                pass

        return (
            self._materialize_raw_json(data)
            or data
        )


    # ======================================================
    # ÍNDICE DE IMPORTACIÓN
    # ======================================================

    def build_import_index(
        self,
    ) -> tuple[
        dict[str, dict],
        dict[str, dict],
    ]:

        cache_key = ("import_index",)

        cached = IMPORT_INDEX_CACHE.get(
            cache_key
        )

        if (
            isinstance(cached, tuple)
            and len(cached) == 2
        ):

            by_source_key, by_session_id = (
                cached
            )

            if (
                isinstance(
                    by_source_key,
                    dict,
                )
                and isinstance(
                    by_session_id,
                    dict,
                )
            ):

                return (
                    dict(by_source_key),
                    dict(by_session_id),
                )

        by_source_key: dict[str, dict] = {}

        by_session_id: dict[str, dict] = {}

        # Se mantiene para compatibilidad con
        # importaciones masivas.
        for session in self.dynamo.scan_all(
            self.table
        ):

            status = (
                session.get("status")
                or session.get("estado")
            )

            summary = {

                "id":
                    session.get(
                        "session_id"
                    ),

                "session_id":
                    session.get(
                        "session_id"
                    )
                    or session.get("id"),

                "source_key":
                    session.get(
                        "source_key"
                    ),

                "participant_id":
                    session.get(
                        "participant_id"
                    ),

                "status":
                    status,

                "estado":
                    status,
            }

            if summary["source_key"]:

                by_source_key[
                    summary["source_key"]
                ] = summary

            if summary["session_id"]:

                by_session_id[
                    summary["session_id"]
                ] = summary

        IMPORT_INDEX_CACHE.set(
            cache_key,
            (
                dict(by_source_key),
                dict(by_session_id),
            ),
        )

        return (
            by_source_key,
            by_session_id,
        )


    # ======================================================
    # LISTAR SESIONES
    # ======================================================

    def list_sessions(
        self,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:

        if status is not None:

            self._validate_status(status)

        cache_key = (
            "sessions",
            status,
            date_from,
            date_to,
        )

        cached = SESSION_CACHE.get(
            cache_key
        )

        if isinstance(cached, list):

            return [
                dict(item)
                for item in cached
            ]

        try:

            if status is not None:

                items = self.dynamo.query_all(
                    self.table,
                    Key("status").eq(
                        status
                    ),
                    index_name=
                        "gsi-sessions-status-date",
                    scan_index_forward=False,
                )

            else:

                items = self.dynamo.scan_all(
                    self.table
                )

        except Exception as error:

            print(
                "Error listando sesiones "
                f"(tipo={type(error).__name__})"
            )

            try:

                items = self.dynamo.scan_all(
                    self.table
                )

            except Exception:

                items = []

        if (
            not items
            and self.legacy_data_service
        ):

            items = (
                self._list_legacy_sessions()
            )

        filtered = [

            self._materialize_raw_json(s)
            or s

            for s in items

            if self._is_in_date_range(
                self._get_date(s),
                date_from,
                date_to,
            )

            and (
                status is None
                or s.get("status")
                    == status
                or s.get("estado")
                    == status
            )
        ]

        result = sorted(
            filtered,
            key=self._date_time_sort_key,
            reverse=True,
        )

        SESSION_CACHE.set(
            cache_key,
            [
                dict(item)
                for item in result
            ],
        )

        return result


    # ======================================================
    # SESIONES LEGACY
    # ======================================================

    def _list_legacy_sessions(
        self,
    ) -> list[dict]:

        if not self.legacy_data_service:
            return []

        try:

            payload = (
                self.legacy_data_service
                .fetch_sessions()
            )

            records = payload.get(
                "sesiones",
                [],
            )

        except Exception:

            return []

        return [

            self._normalize_legacy_session(
                record
            )

            for record in records

            if isinstance(
                record,
                Mapping,
            )
        ]


    @staticmethod
    def _normalize_legacy_session(
        record: Mapping[str, Any],
    ) -> dict:

        session_data = (
            record.get("session_data")
        )

        if isinstance(
            session_data,
            Mapping,
        ):

            session_data = dict(
                session_data
            )

        else:

            session_data = {}

        source_key = (
            record.get("s3_key")
        )

        session_id = (

            session_data.get(
                "session_id"
            )

            or record.get(
                "session_id"
            )

            or source_key

            or f"legacy-session-{id(record)}"
        )

        fecha_inicio = (

            session_data.get(
                "fechaInicio"
            )

            or session_data.get(
                "fecha_inicio"
            )
        )

        fecha, hora = (
            SessionService
            ._split_legacy_datetime(
                fecha_inicio
            )
        )

        edad = None

        genero = None

        difficulty = (
            session_data.get(
                "difficulty_level"
            )
        )

        try:

            from services.parsers.session_parser import (
                SessionParser
            )

            summary = (
                SessionParser(record)
                .get_dashboard_summary()
            )

            fecha = (
                summary.get("fecha")
                or fecha
            )

            hora = (
                summary.get("hora")
                or hora
            )

            edad = summary.get(
                "edad"
            )

            genero = summary.get(
                "genero"
            )

            difficulty = (
                summary.get(
                    "difficulty"
                )
                or difficulty
            )

        except (
            TypeError,
            ValueError,
        ):

            pass

        return {

            "session_id":
                str(session_id),

            "id":
                str(session_id),

            "participant_id":
                session_data.get(
                    "idParticipante"
                ),

            "nombre":
                None,

            "observaciones":
                None,

            "prediction":
                None,

            "status":
                STATUS_PENDING_IDENTIFICATION,

            "estado":
                STATUS_PENDING_IDENTIFICATION,

            "fecha":
                fecha,

            "hora":
                hora,

            "fecha_inicio":
                fecha_inicio,

            "fecha_fin":
                (
                    session_data.get(
                        "fechaFin"
                    )
                    or session_data.get(
                        "fecha_fin"
                    )
                ),

            "edad":
                edad,

            "genero":
                genero,

            "difficulty":
                difficulty,

            "duracion_estimada":
                False,

            "prediction_error":
                None,

            "prediction_missing_fields":
                [],

            "raw_json":
                dict(record),
        }


    @staticmethod
    def _split_legacy_datetime(
        value: Any,
    ) -> tuple[
        Optional[str],
        Optional[str],
    ]:

        if not value:

            return None, None

        parts = (
            str(value)
            .strip()
            .replace("T", " ")
            .split(None, 1)
        )

        if len(parts) == 1:

            return (
                parts[0][:10]
                if _RE_DATE_ISO.match(
                    parts[0]
                )
                else None,
                None,
            )

        return (
            parts[0][:10],
            parts[1][:8],
        )


    # ======================================================
    # SESIONES POR FECHA
    # ======================================================

    def list_sessions_by_date(
        self,
        fecha: str,
    ) -> list[dict]:

        self._validate_date(fecha)

        cache_key = (
            "sessions_by_date",
            fecha,
        )

        cached = SESSION_CACHE.get(
            cache_key
        )

        if isinstance(cached, list):

            return [
                dict(item)
                for item in cached
            ]

        combined: dict[
            str,
            dict,
        ] = {}

        try:

            exact_match = (
                self.dynamo.query_all(
                    self.table,
                    Key("fecha").eq(
                        fecha
                    ),
                    index_name=
                        "gsi-sessions-fecha",
                )
            )

            for session in exact_match:

                combined[
                    self._session_unique_key(
                        session
                    )
                ] = (
                    self._materialize_raw_json(
                        session
                    )
                    or session
                )

        except Exception:

            exact_match = []

        # Compatibilidad
        for session in self.list_sessions():

            if (
                self._get_date(session)
                == fecha
            ):

                combined[
                    self._session_unique_key(
                        session
                    )
                ] = session

        result = sorted(
            combined.values(),
            key=self._date_time_sort_key,
            reverse=True,
        )

        SESSION_CACHE.set(
            cache_key,
            [
                dict(item)
                for item in result
            ],
            ttl_seconds=60,
        )

        return result


    def list_dates(
        self,
    ) -> list[str]:

        return sorted(
            {
                date
                for session in self.list_sessions()

                if (
                    date := self._get_date(
                        session
                    )
                )
            },
            reverse=True,
        )


    # ======================================================
    # SESIONES PENDIENTES
    # ======================================================

    def list_pending_sessions(
        self,
    ) -> list[dict]:

        pending_statuses = [
            STATUS_PENDING_PREDICTION,
            STATUS_PENDING_ASSIGNMENT,
        ]

        pending: list[dict] = []

        try:

            for status in pending_statuses:

                pending.extend(
                    self.dynamo.query_all(
                        self.table,
                        Key("status").eq(
                            status
                        ),
                        index_name=
                            "gsi-sessions-status-date",
                    )
                )

        except Exception:

            for status in pending_statuses:

                pending.extend(
                    self.list_sessions(
                        status=status
                    )
                )

        return sorted(

            [
                self._materialize_raw_json(x)
                or x

                for x in pending
            ],

            key=self._date_time_sort_key,
            reverse=True,
        )


    def list_unassigned_sessions(
        self,
    ) -> list[dict]:

        return [

            session

            for session in self.list_sessions()

            if (
                (
                    not session.get(
                        "participant_id"
                    )
                    or session.get(
                        "participant_id"
                    )
                    == PENDING_PARTICIPANT_ID
                )

                and not session.get(
                    "nombre"
                )

                and (
                    session.get(
                        "estado",
                        session.get("status"),
                    )
                    == STATUS_PENDING_IDENTIFICATION
                )
            )
        ]


    def list_pending_assignment(
        self,
    ) -> list[dict]:

        return self.list_unassigned_sessions()


    # ======================================================
    # ASIGNAR PARTICIPANTE
    # ======================================================

    def assign_participant(
        self,
        session_id: str,
        participant_id: str,
        nombre: Optional[str] = None,
        *,
        actor_ctx: Optional[dict] = None,
    ) -> None:

        participant_id = (
            self._validate_participant_id(
                participant_id
            )
        )

        existing = self.get_session(
            session_id
        )

        if not existing:

            raise SessionNotFound(
                "La sesion no existe"
            )

        old_key = {

            "participant_id":
                existing.get(
                    "participant_id"
                ),

            "sk":
                existing.get("sk"),
        }

        if (
            not old_key["participant_id"]
            or not old_key["sk"]
        ):

            raise SessionNotFound(
                "La sesion no tiene una clave "
                "DynamoDB valida"
            )

        new_item = deepcopy(
            existing
        )

        new_item[
            "participant_id"
        ] = participant_id

        new_item[
            "status"
        ] = STATUS_IDENTIFIED

        new_item[
            "estado"
        ] = STATUS_IDENTIFIED

        new_item[
            "updated_at"
        ] = DynamoDBService.now_iso()

        if nombre is not None:

            if (
                not isinstance(
                    nombre,
                    str,
                )
                or not nombre.strip()
            ):

                raise ValueError(
                    "nombre debe ser texto no vacio"
                )

            new_item[
                "nombre"
            ] = nombre.strip()

        # --------------------------------------------------
        # Crear nueva partición
        # --------------------------------------------------

        self.dynamo.put_item(
            self.table,
            new_item,
            condition_expression=
                Attr("session_id").not_exists(),
        )

        # --------------------------------------------------
        # Eliminar item anterior
        # --------------------------------------------------

        self.dynamo.delete_item(
            self.table,
            old_key,
        )

        invalidate_sessions_cache()

        if self.audit and actor_ctx:

            try:

                self.audit.log_session_assigned(

                    actor_uid=
                        actor_ctx.get(
                            "actor_uid",
                            "",
                        ),

                    actor_email=
                        actor_ctx.get(
                            "actor_email",
                            "",
                        ),

                    actor_rol=
                        actor_ctx.get(
                            "actor_rol",
                            "",
                        ),

                    session_id=
                        session_id,

                    participant_id=
                        participant_id,
                )

            except Exception:

                pass


    # ======================================================
    # ACTUALIZAR NOMBRE
    # ======================================================

    def update_name(
        self,
        session_id: str,
        nombre: str,
    ) -> None:

        if (
            not isinstance(
                nombre,
                str,
            )
            or not nombre.strip()
        ):

            raise ValueError(
                "nombre debe ser texto no vacio"
            )

        key = (
            self._resolve_session_key(
                session_id
            )
        )

        if not key:

            raise SessionNotFound(
                "La sesion no existe"
            )

        payload = {

            "nombre":
                nombre.strip(),

            "status":
                STATUS_IDENTIFIED,

            "estado":
                STATUS_IDENTIFIED,

            "updated_at":
                DynamoDBService.now_iso(),
        }

        self.dynamo.update_item(
            self.table,
            key=key,
            update_data=payload,
        )

        invalidate_sessions_cache()


    # ======================================================
    # ACTUALIZAR SESIÓN
    # ======================================================

    def update_session(
        self,
        session_id: str,
        data: Mapping[str, Any],
    ) -> None:

        if not isinstance(
            data,
            Mapping,
        ):

            raise ValueError(
                "data debe ser un objeto"
            )

        allowed = {
            "nombre",
            "observaciones",
            "participant_id",
        }

        changes = {

            key: data[key]

            for key in allowed

            if key in data
        }

        if not changes:

            raise ValueError(
                "Solo se pueden actualizar "
                "nombre, observaciones o "
                "participant_id"
            )

        # --------------------------------------------------
        # Nombre
        # --------------------------------------------------

        if "nombre" in changes:

            n = changes["nombre"]

            if (
                n is not None
                and (
                    not isinstance(
                        n,
                        str,
                    )
                    or not n.strip()
                )
            ):

                raise ValueError(
                    "nombre debe ser texto "
                    "no vacio o null"
                )

            changes["nombre"] = (
                n.strip()
                if isinstance(n, str)
                else None
            )

        # --------------------------------------------------
        # Participant
        # --------------------------------------------------

        if (
            "participant_id"
            in changes
            and changes["participant_id"]
            is not None
        ):

            changes[
                "participant_id"
            ] = (
                self._validate_participant_id(
                    changes[
                        "participant_id"
                    ]
                )
            )

        # --------------------------------------------------
        # Observaciones
        # --------------------------------------------------

        if (
            "observaciones"
            in changes
            and changes[
                "observaciones"
            ] is not None
        ):

            if not isinstance(
                changes[
                    "observaciones"
                ],
                str,
            ):

                raise ValueError(
                    "observaciones debe ser "
                    "texto o null"
                )

        # --------------------------------------------------
        # Estado
        # --------------------------------------------------

        if (
            changes.get(
                "participant_id"
            )
            or changes.get("nombre")
        ):

            changes[
                "status"
            ] = STATUS_IDENTIFIED

            changes[
                "estado"
            ] = STATUS_IDENTIFIED

        elif (
            "nombre" in changes
            and not changes["nombre"]
        ):

            changes[
                "status"
            ] = STATUS_PENDING_IDENTIFICATION

            changes[
                "estado"
            ] = STATUS_PENDING_IDENTIFICATION

        changes[
            "updated_at"
        ] = DynamoDBService.now_iso()

        # --------------------------------------------------
        # Obtener sesión
        # --------------------------------------------------

        existing = self.get_session(
            session_id
        )

        if not existing:

            raise SessionNotFound(
                "La sesion no existe"
            )

        # --------------------------------------------------
        # Cambio de PK
        # --------------------------------------------------

        new_participant = changes.get(
            "participant_id"
        )

        old_participant = existing.get(
            "participant_id"
        )

        if (
            new_participant is not None
            and new_participant
            != old_participant
        ):

            # Crear copia en nueva partición.
            new_item = deepcopy(
                existing
            )

            for field, value in changes.items():

                new_item[
                    field
                ] = value

            new_item[
                "participant_id"
            ] = new_participant

            new_item[
                "updated_at"
            ] = DynamoDBService.now_iso()

            self.dynamo.put_item(
                self.table,
                new_item,
                condition_expression=
                    Attr("session_id").not_exists(),
            )

            # Eliminar antiguo item.
            old_key = {

                "participant_id":
                    old_participant,

                "sk":
                    existing.get("sk"),
            }

            self.dynamo.delete_item(
                self.table,
                old_key,
            )

        else:

            # Actualización normal.
            key = (
                self._resolve_session_key(
                    session_id
                )
            )

            if not key:

                raise SessionNotFound(
                    "La sesion no existe"
                )

            # participant_id no debe actualizarse
            # aquí porque ya es la PK.
            changes_for_update = dict(
                changes
            )

            changes_for_update.pop(
                "participant_id",
                None,
            )

            self.dynamo.update_item(
                self.table,
                key=key,
                update_data=
                    changes_for_update,
            )

        invalidate_sessions_cache()


    # ======================================================
    # ACTUALIZAR PREDICCIÓN
    # ======================================================

    def update_prediction(
        self,
        session_id: str,
        prediction: Mapping[str, Any],
    ) -> None:

        if not isinstance(
            prediction,
            Mapping,
        ):

            raise ValueError(
                "prediction debe ser un objeto"
            )

        key = (
            self._resolve_session_key(
                session_id
            )
        )

        if not key:

            raise SessionNotFound(
                "La sesion no existe"
            )

        self.dynamo.update_item(

            self.table,

            key=key,

            update_data={

                "prediction":
                    deepcopy(
                        dict(prediction)
                    ),

                "status":
                    STATUS_PENDING_IDENTIFICATION,

                "estado":
                    STATUS_PENDING_IDENTIFICATION,

                "prediction_error":
                    None,

                "prediction_missing_fields":
                    [],

                "updated_at":
                    DynamoDBService.now_iso(),
            },
        )

        invalidate_sessions_cache()


    # ======================================================
    # ACTUALIZAR ESTADO
    # ======================================================

    def update_status(
        self,
        session_id: str,
        status: str,
    ) -> None:

        self._validate_status(
            status
        )

        key = (
            self._resolve_session_key(
                session_id
            )
        )

        if not key:

            raise SessionNotFound(
                "La sesion no existe"
            )

        self.dynamo.update_item(

            self.table,

            key=key,

            update_data={

                "status":
                    status,

                "estado":
                    status,

                "updated_at":
                    DynamoDBService.now_iso(),
            },
        )

        invalidate_sessions_cache()


    # ======================================================
    # INFORMACIÓN PREDICCIÓN PENDIENTE
    # ======================================================

    def update_prediction_pending_info(
        self,
        session_id: str,
        missing_fields: list[str],
    ) -> None:

        key = (
            self._resolve_session_key(
                session_id
            )
        )

        if not key:

            raise SessionNotFound(
                "La sesion no existe"
            )

        self.dynamo.update_item(

            self.table,

            key=key,

            update_data={

                "prediction_error":
                    (
                        "Faltan datos requeridos "
                        "para ejecutar la predicción"
                    ),

                "prediction_missing_fields":
                    list(
                        missing_fields
                        or []
                    ),

                "updated_at":
                    DynamoDBService.now_iso(),
            },
        )

        invalidate_sessions_cache()


    # ======================================================
    # BUSCAR POR NOMBRE
    # ======================================================

    def search_by_name(
        self,
        nombre: str,
    ) -> list[dict]:

        if (
            not isinstance(
                nombre,
                str,
            )
            or not nombre.strip()
        ):

            return []

        nombre_stripped = (
            nombre.strip()
        )

        try:

            return self.dynamo.query_all(

                self.table,

                Key("nombre").eq(
                    nombre_stripped
                ),

                index_name=
                    "gsi-sessions-nombre",

            )

        except Exception:

            # Compatibilidad con tablas
            # donde no exista este GSI.
            try:

                return [

                    s

                    for s in self.list_sessions()

                    if s.get("nombre")
                    == nombre_stripped
                ]

            except Exception:

                return []


    # ======================================================
    # HISTORIAL DEL PARTICIPANTE
    # ======================================================

    def search_participant_sessions(
        self,
        participant_id: str,
    ) -> list[dict]:

        participant_id = (
            self._validate_participant_id(
                participant_id
            )
        )

        # IMPORTANTE:
        # participant_id es la PK real de
        # la tabla, por lo que NO necesitamos
        # hacer scan.
        sessions = (
            self.dynamo.query_all(

                self.table,

                Key("participant_id").eq(
                    participant_id
                ),

                scan_index_forward=False,
            )
        )

        if (
            not sessions
            and self.legacy_data_service
        ):

            sessions = [

                s

                for s in (
                    self._list_legacy_sessions()
                )

                if str(
                    s.get(
                        "participant_id",
                        "",
                    )
                ).upper()
                == participant_id
            ]

        hydrated = [

            self._materialize_raw_json(s)
            or s

            for s in sessions
        ]

        return sorted(

            hydrated,

            key=self._date_time_sort_key,

            reverse=True,
        )


    def get_sessions_by_participant(
        self,
        participant_id: str,
    ) -> list[dict]:

        return (
            self.search_participant_sessions(
                participant_id
            )
        )


    # ======================================================
    # ELIMINAR
    # ======================================================

    def delete_session(
        self,
        session_id: str,
    ) -> None:

        key = (
            self._resolve_session_key(
                session_id
            )
        )

        if not key:

            raise SessionNotFound(
                "La sesion no existe"
            )

        self.dynamo.delete_item(
            self.table,
            key,
        )

        invalidate_sessions_cache()


    # ======================================================
    # VALIDACIONES
    # ======================================================

    @staticmethod
    def _validate_status(
        status: str,
    ) -> None:

        if status not in (
            VALID_SESSION_STATUSES
        ):

            raise ValueError(
                "Estado de sesion no valido: "
                f"{status}"
            )


    @staticmethod
    def _validate_participant_id(
        participant_id: str,
    ) -> str:

        if (
            not isinstance(
                participant_id,
                str,
            )
            or not participant_id.strip()
        ):

            raise ValueError(
                "participant_id debe ser "
                "texto no vacio"
            )

        participant_id = (
            participant_id.strip()
        )

        if len(participant_id) > 64:

            raise ValueError(
                "participant_id no puede "
                "superar los 64 caracteres"
            )

        if not re.fullmatch(
            r"[A-Za-z0-9_-]+",
            participant_id,
        ):

            raise ValueError(
                "participant_id contiene "
                "caracteres no permitidos"
            )

        return participant_id


    @staticmethod
    def _validate_date(
        fecha: str,
    ) -> None:

        try:

            datetime.fromisoformat(
                fecha
            ).date()

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                "fecha debe usar formato "
                "ISO valido"
            ) from error


    # ======================================================
    # FILTRO DE FECHAS
    # ======================================================

    @staticmethod
    def _is_in_date_range(
        fecha: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
    ) -> bool:

        if not date_from and not date_to:

            return True

        if not fecha:

            return False

        try:

            session_date = (
                datetime.fromisoformat(
                    fecha
                ).date()
            )

            start_date = (

                datetime.fromisoformat(
                    date_from
                ).date()

                if date_from

                else None
            )

            end_date = (

                datetime.fromisoformat(
                    date_to
                ).date()

                if date_to

                else None
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                "Las fechas deben usar formato "
                "ISO valido"
            ) from error

        return (

            (
                start_date is None
                or session_date >= start_date
            )

            and

            (
                end_date is None
                or session_date <= end_date
            )
        )


    # ======================================================
    # FECHA / HORA
    # ======================================================

    @staticmethod
    def _normalise_date_time(
        fecha: Optional[str],
        hora: Optional[str],
        fecha_inicio: Optional[str],
    ) -> tuple[
        Optional[str],
        Optional[str],
    ]:

        if (
            fecha is None
            and fecha_inicio
        ):

            try:

                start = (
                    datetime.fromisoformat(
                        fecha_inicio
                    )
                )

            except ValueError as error:

                raise ValueError(
                    "fecha_inicio debe usar "
                    "formato ISO valido"
                ) from error

            return (
                start.date().isoformat(),
                start.strftime("%H:%M"),
            )

        if fecha is not None:

            SessionService._validate_date(
                fecha
            )

        if hora is not None:

            try:

                datetime.strptime(
                    hora,
                    "%H:%M",
                )

            except ValueError as error:

                raise ValueError(
                    "hora debe usar formato HH:MM"
                ) from error

        return fecha, hora


    # ======================================================
    # OBTENER FECHA
    # ======================================================

    @classmethod
    def _get_date(
        cls,
        session: Mapping[str, Any],
    ) -> Optional[str]:

        if fecha := session.get(
            "fecha"
        ):

            return cls._clean_date(
                fecha
            )

        if fecha_inicio := session.get(
            "fecha_inicio"
        ):

            return cls._clean_date(
                fecha_inicio
            )

        raw_json = session.get(
            "raw_json"
        )

        if isinstance(
            raw_json,
            Mapping,
        ):

            session_data = (
                raw_json.get(
                    "session_data"
                )
            )

            if isinstance(
                session_data,
                Mapping,
            ):

                if fecha_inicio := (
                    session_data.get(
                        "fechaInicio"
                    )
                ):

                    return cls._clean_date(
                        fecha_inicio
                    )

            if s3_key := raw_json.get(
                "s3_key"
            ):

                if date_from_key := (
                    cls._date_from_s3_key(
                        s3_key
                    )
                ):

                    return date_from_key

        if source_key := session.get(
            "source_key"
        ):

            if date_from_key := (
                cls._date_from_s3_key(
                    source_key
                )
            ):

                return date_from_key

        return None


    # ======================================================
    # OBTENER HORA
    # ======================================================

    @classmethod
    def _get_time(
        cls,
        session: Mapping[str, Any],
    ) -> Optional[str]:

        if hora := session.get(
            "hora"
        ):

            return cls._clean_time(
                hora
            )

        if fecha_inicio := session.get(
            "fecha_inicio"
        ):

            if tv := (
                cls._time_from_datetime(
                    fecha_inicio
                )
            ):

                return tv

        raw_json = session.get(
            "raw_json"
        )

        if isinstance(
            raw_json,
            Mapping,
        ):

            session_data = (
                raw_json.get(
                    "session_data"
                )
            )

            if isinstance(
                session_data,
                Mapping,
            ):

                if fecha_inicio := (
                    session_data.get(
                        "fechaInicio"
                    )
                ):

                    if tv := (
                        cls._time_from_datetime(
                            fecha_inicio
                        )
                    ):

                        return tv

            if s3_key := raw_json.get(
                "s3_key"
            ):

                if tv := (
                    cls._time_from_s3_key(
                        s3_key
                    )
                ):

                    return tv

        if source_key := session.get(
            "source_key"
        ):

            if tv := (
                cls._time_from_s3_key(
                    source_key
                )
            ):

                return tv

        return None


    # ======================================================
    # FECHA RAW JSON
    # ======================================================

    @classmethod
    def _get_date_from_raw_json(
        cls,
        raw_json: Mapping[str, Any],
        source_key: Optional[str],
    ) -> Optional[str]:

        if isinstance(
            raw_json,
            Mapping,
        ):

            date_value = cls._get_date(
                {
                    "raw_json":
                        raw_json
                }
            )

            if date_value:

                return date_value

        if source_key:

            return cls._date_from_s3_key(
                source_key
            )

        return None


    # ======================================================
    # HORA RAW JSON
    # ======================================================

    @classmethod
    def _get_time_from_raw_json(
        cls,
        raw_json: Mapping[str, Any],
        source_key: Optional[str],
    ) -> Optional[str]:

        if isinstance(
            raw_json,
            Mapping,
        ):

            time_value = cls._get_time(
                {
                    "raw_json":
                        raw_json
                }
            )

            if time_value:

                return time_value

        if source_key:

            return cls._time_from_s3_key(
                source_key
            )

        return None


    # ======================================================
    # LIMPIEZA FECHA
    # ======================================================

    @staticmethod
    def _clean_date(
        value: Any,
    ) -> Optional[str]:

        if value is None:

            return None

        value = str(value).strip()

        if not value:

            return None

        return (
            value[:10]
            if _RE_DATE_ISO.match(value)
            else None
        )


    # ======================================================
    # LIMPIEZA HORA
    # ======================================================

    @staticmethod
    def _clean_time(
        value: Any,
    ) -> Optional[str]:

        if value is None:

            return None

        value = str(value).strip()

        if not value:

            return None

        match = _RE_TIME_HM.match(
            value
        )

        return (

            f"{match.group(1)}:"
            f"{match.group(2)}"

            if match

            else None
        )


    # ======================================================
    # HORA DESDE DATETIME
    # ======================================================

    @staticmethod
    def _time_from_datetime(
        value: Any,
    ) -> Optional[str]:

        if value is None:

            return None

        match = _RE_DATETIME_TIME.search(
            str(value).strip()
        )

        return (

            f"{match.group(1)}:"
            f"{match.group(2)}"

            if match

            else None
        )


    # ======================================================
    # FECHA DESDE S3 KEY
    # ======================================================

    @staticmethod
    def _date_from_s3_key(
        s3_key: Any,
    ) -> Optional[str]:

        if not s3_key:

            return None

        match = _RE_S3_DATE.search(
            str(s3_key)
        )

        return (
            match.group(1)
            if match
            else None
        )


    # ======================================================
    # HORA DESDE S3 KEY
    # ======================================================

    @staticmethod
    def _time_from_s3_key(
        s3_key: Any,
    ) -> Optional[str]:

        if not s3_key:

            return None

        match = _RE_S3_TIME.search(
            str(s3_key)
        )

        if not match:

            return None

        value = match.group(1)

        return (
            f"{value[0:2]}:"
            f"{value[2:4]}"
        )


    # ======================================================
    # ORDENAMIENTO
    # ======================================================

    @classmethod
    def _date_time_sort_key(
        cls,
        session: Mapping[str, Any],
    ) -> str:

        return (

            f"{cls._get_date(session) or ''}"
            f"T"
            f"{cls._get_time(session) or ''}"
        )


    # ======================================================
    # CLAVE ÚNICA
    # ======================================================

    @staticmethod
    def _session_unique_key(
        session: Mapping[str, Any],
    ) -> str:

        if session_id := (
            session.get("session_id")
            or session.get("id")
        ):

            return str(session_id)

        if source_key := (
            session.get("source_key")
        ):

            return (
                f"source:{source_key}"
            )

        return repr(
            sorted(
                (
                    str(k),
                    repr(v),
                )

                for k, v
                in session.items()
            )
        )


    # ======================================================
    # COMPATIBILIDAD
    # ======================================================

    def update_document_if_supported(
        self,
        session_id: str,
        data: dict,
    ) -> None:

        key = (
            self._resolve_session_key(
                session_id
            )
        )

        if not key:

            raise SessionNotFound(
                "La sesion no existe"
            )

        updates = dict(data)

        updates[
            "updated_at"
        ] = DynamoDBService.now_iso()

        self.dynamo.update_item(

            self.table,

            key=key,

            update_data=updates,
        )

        invalidate_sessions_cache()


    def update_session_fields(
        self,
        session_id: str,
        data: dict,
    ) -> None:

        self.update_document_if_supported(
            session_id,
            data,
        )
