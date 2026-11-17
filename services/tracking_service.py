from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.constants import (
    AUDIT_TRACKING_CREATED,
    AUDIT_TRACKING_DELETED,
    AUDIT_TRACKING_SESSION_ADDED,
    AUDIT_TRACKING_SESSION_REMOVED,
    DYNAMODB_TABLE_SESSIONS,
    DYNAMODB_TABLE_TRACKINGS,
)
from services.cache_service import invalidate_sessions_cache
from services.dynamodb_service import DynamoDBService
from services.emotion_classification import (
    bienestar_promedio,
    clasificacion_binaria,
    clasificacion_original,
    etapas_bienestar,
)
from services.emotion_model_v2_service import ESTADO_ANALIZADO

logger = logging.getLogger(__name__)


class TrackingNotFound(ValueError):
    """El seguimiento no existe o no pertenece al profesor solicitante.

    Se responde igual en ambos casos para no revelar la existencia de
    seguimientos ajenos.
    """


class TrackingService:
    """Gestiona seguimientos como colecciones de experiencias.

    Un seguimiento agrupa experiencias independientes por su ``session_id``.
    EmotionLab NO dispone de identificación por alumno: no se generan ni se
    usan identificadores de estudiante. La propiedad del seguimiento es del
    profesor que lo creó (``created_by``), que es quien puede operarlo.
    """

    TRACKINGS_ID_INDEX = "gsi-trackings-id"
    SESSIONS_TRACKING_INDEX = "gsi-sessions-tracking"

    def __init__(
        self,
        dynamodb_service: DynamoDBService,
        session_service,
        audit_svc=None,
    ):
        if dynamodb_service is None:
            raise RuntimeError(
                "TrackingService requiere DynamoDB."
            )

        if not hasattr(dynamodb_service, "transact_write_items"):
            raise RuntimeError(
                "El repositorio configurado no admite transacciones DynamoDB."
            )

        self.dynamo = dynamodb_service
        self.sessions = session_service
        self.audit = audit_svc

        self.trackings_table = DYNAMODB_TABLE_TRACKINGS
        self.sessions_table = DYNAMODB_TABLE_SESSIONS

    # ==========================================================
    # HELPERS
    # ==========================================================

    @staticmethod
    def _now_iso() -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _normalize_tracking_code(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError(
                "El código de seguimiento debe ser texto."
            )

        code = value.strip().upper()

        if not 2 <= len(code) <= 32:
            raise ValueError(
                "El código debe tener entre 2 y 32 caracteres."
            )

        if not re.fullmatch(
            r"[A-Z0-9]+(?:-[A-Z0-9]+)*",
            code,
        ):
            raise ValueError(
                "El código solo puede contener letras, números y guiones."
            )

        return code

    def _get_tracking_by_code(
        self,
        tracking_code: str,
    ) -> Optional[dict]:
        return self.dynamo.get_item(
            self.trackings_table,
            {"tracking_code": tracking_code},
        )

    def generate_tracking_code(self) -> str:
        """Genera un código legible y verifica que no exista en DynamoDB.

        La vista previa no reserva el código: create_tracking vuelve a
        comprobarlo y la transacción protege la clave contra carreras.
        """
        for _ in range(20):
            code = f"SEG-{uuid.uuid4().hex[:12].upper()}"
            if not self._get_tracking_by_code(code):
                return code
        raise RuntimeError("No se pudo generar un código único para el seguimiento.")

    def _get_tracking_by_id(
        self,
        tracking_id: str,
    ) -> Optional[dict]:
        matches = self.dynamo.query_all(
            self.trackings_table,
            Key("tracking_id").eq(str(tracking_id)),
            index_name=self.TRACKINGS_ID_INDEX,
            max_items=1,
        )

        return matches[0] if matches else None

    @staticmethod
    def _actor_uid(
        actor_ctx: Optional[Mapping[str, Any]],
    ) -> str:
        """Identificador del profesor autenticado (desde el contexto de auth)."""

        actor_ctx = actor_ctx or {}

        return str(actor_ctx.get("actor_uid") or "")

    def _require_tracking(
        self,
        tracking_id: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        tracking = self._get_tracking_by_id(tracking_id)

        if not tracking:
            raise TrackingNotFound("El seguimiento no existe.")

        solicitante = self._actor_uid(actor_ctx)

        # Las rutas suministran actor_ctx. Sin identidad y propietario
        # verificables, el registro legacy queda denegado hasta su migración.
        if actor_ctx is not None:
            propietario = str(tracking.get("created_by") or "")
            if not solicitante or not propietario or propietario != solicitante:
                raise TrackingNotFound("El seguimiento no existe.")

        return tracking

    def _require_session_key(
        self,
        session_id: str,
    ) -> dict:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError(
                "Debes indicar un session_id válido."
            )

        key = self.sessions._resolve_session_key(
            session_id.strip()
        )

        if not key:
            raise ValueError(
                "La sesión no existe en DynamoDB o no tiene una clave válida."
            )

        return {
            "participant_id": key["participant_id"],
            "sk": key["sk"],
        }

    def _require_session_owner(
        self,
        session: Mapping[str, Any],
        actor_ctx: Optional[Mapping[str, Any]],
    ) -> None:
        """Deniega asociaciones a sesiones ajenas o sin propietario verificable."""
        if actor_ctx is None:
            return
        actor_uid = self._actor_uid(actor_ctx)
        owner_id = str(session.get("owner_id") or "")
        if not actor_uid or not owner_id or owner_id != actor_uid:
            raise ValueError("La sesión no está disponible para este especialista.")

    def _sessions_for_actor(
        self,
        sessions: list[dict],
        actor_ctx: Optional[Mapping[str, Any]],
    ) -> list[dict]:
        if actor_ctx is None:
            return sessions
        actor_uid = self._actor_uid(actor_ctx)
        if not actor_uid:
            return []
        return [item for item in sessions if str(item.get("owner_id") or "") == actor_uid]

    @staticmethod
    def _ensure_v2_processed(session: Mapping[str, Any]) -> None:
        """Exige que la sesión tenga un procesamiento V2 finalizado.

        Un seguimiento solo agrupa sesiones ya analizadas por EmotionLab V2;
        de lo contrario la evolución de bienestar tendría huecos.
        """

        procesamiento = session.get("model_processing")

        if (
            not isinstance(procesamiento, Mapping)
            or procesamiento.get("status") != ESTADO_ANALIZADO
        ):
            raise ValueError(
                "La sesión no tiene un procesamiento V2 finalizado correctamente."
            )

    @staticmethod
    def _session_date(session: Mapping[str, Any]) -> Optional[str]:
        """Fecha de la sesión (``fecha`` + ``hora`` cuando ambas existen)."""

        fecha = session.get("fecha")
        hora = session.get("hora")

        if fecha and hora:
            return f"{fecha} {hora}"

        return fecha

    def _audit_event(
        self,
        action: str,
        actor_ctx: Optional[Mapping[str, Any]],
        tracking: Mapping[str, Any],
        session_id: Optional[str] = None,
    ) -> None:
        if not self.audit:
            return

        actor_ctx = actor_ctx or {}

        try:
            self.audit.log(
                action,
                actor_uid=actor_ctx.get("actor_uid", ""),
                actor_email=actor_ctx.get("actor_email", ""),
                actor_rol=actor_ctx.get("actor_rol", ""),
                session_id=session_id,
                extra={
                    "tracking_id": tracking.get("tracking_id"),
                    "tracking_code": tracking.get("tracking_code"),
                },
            )
        except Exception:
            # La operación de negocio ya fue confirmada en DynamoDB.
            # Un fallo de auditoría no debe presentarse como fallo de la operación.
            logger.exception(
                "No se pudo registrar el evento de auditoría %s",
                action,
            )

    def _session_summary(self, session: Mapping[str, Any]) -> dict:
        """Devuelve solo campos necesarios para la pantalla de seguimiento.

        Incluye la clasificación FINAL binaria (``0`` / ``1``) y el promedio
        de bienestar derivados de ``model_processing``. La clasificación
        original del modelo se conserva para trazabilidad. Es compatible con
        registros históricos (se derivan al vuelo si faltan).
        """

        fields = (
            "session_id",
            "status",
            "fecha",
            "hora",
            "capture_started_at",
            "model_processing",
            "difficulty",
        )

        summary = {
            field: session.get(field)
            for field in fields
            if session.get(field) is not None
        }

        processing = session.get("model_processing")
        etapas = etapas_bienestar(processing)

        # No se expone ningún identificador de alumno: EmotionLab identifica
        # cada experiencia únicamente por su ``session_id``.
        summary["classification_final"] = clasificacion_binaria(processing)
        summary["classification_original"] = clasificacion_original(processing)
        summary["indicador_inicial"] = etapas.get("inicial")
        summary["indicador_facil"] = etapas.get("facil")
        summary["indicador_dificil"] = etapas.get("dificil")
        summary["bienestar_promedio"] = bienestar_promedio(processing)

        return summary

    def _raise_transaction_error(
        self,
        error: ClientError,
        session_key: Optional[Mapping[str, Any]] = None,
        tracking_code: Optional[str] = None,
        tracking_id: Optional[str] = None,
    ) -> None:
        error_code = (
            error.response
            .get("Error", {})
            .get("Code")
        )

        if error_code == "TransactionCanceledException":
            if tracking_code:
                existing = self._get_tracking_by_code(
                    tracking_code
                )

                if existing:
                    raise ValueError(
                        "Ya existe un seguimiento con ese código."
                    ) from error

            if session_key:
                current_session = self.dynamo.get_item(
                    self.sessions_table,
                    dict(session_key),
                )

                current_tracking_id = (
                    current_session or {}
                ).get("tracking_id")

                if current_tracking_id:
                    if (
                        tracking_id
                        and current_tracking_id == tracking_id
                    ):
                        raise ValueError(
                            "La sesión ya está asociada a este seguimiento."
                        ) from error

                    raise ValueError(
                        "La sesión ya está asociada a otro seguimiento."
                    ) from error

            raise ValueError(
                "No se pudo completar la operación. "
                "Verifica que el código y la sesión sigan disponibles."
            ) from error

        raise error

    # ==========================================================
    # CREAR SEGUIMIENTO CON SU PRIMERA SESIÓN
    # ==========================================================

    def create_tracking(
        self,
        tracking_code: Optional[str] = None,
        first_session_id: str = "",
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        tracking_code = self._normalize_tracking_code(
            tracking_code or self.generate_tracking_code()
        )

        if self._get_tracking_by_code(tracking_code):
            raise ValueError(
                "Ya existe un seguimiento con ese código."
            )

        session_key = self._require_session_key(
            first_session_id
        )

        session = self.dynamo.get_item(
            self.sessions_table,
            session_key,
        )

        if not session:
            raise ValueError(
                "No se encontró la sesión seleccionada."
            )

        self._require_session_owner(session, actor_ctx)
        self._ensure_v2_processed(session)

        if session.get("tracking_id"):
            raise ValueError(
                "La sesión ya está asociada a un seguimiento."
            )

        now = self._now_iso()
        tracking_id = str(uuid.uuid4())

        # El seguimiento agrupa experiencias independientes por su
        # `session_id`. No se almacena ningún identificador de alumno ni se
        # usa el `participant_id` para agrupar o validar: el profesor
        # propietario (obtenido del contexto de autenticación) es quien
        # puede operar el seguimiento.
        tracking = {
            "tracking_code": tracking_code,
            "tracking_id": tracking_id,
            "created_by": self._actor_uid(actor_ctx),
            "created_at": now,
            "updated_at": now,
        }

        operations = [
            {
                "Put": {
                    "TableName": self.trackings_table,
                    "Item": tracking,
                    "ConditionExpression": (
                        "attribute_not_exists(#tracking_code)"
                    ),
                    "ExpressionAttributeNames": {
                        "#tracking_code": "tracking_code",
                    },
                }
            },
            {
                "Update": {
                    "TableName": self.sessions_table,
                    "Key": session_key,
                    "UpdateExpression": (
                        "SET #tracking_id = :tracking_id, "
                        "#updated_at = :updated_at"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(#session_id) "
                        "AND attribute_not_exists(#tracking_id)"
                    ),
                    "ExpressionAttributeNames": {
                        "#session_id": "session_id",
                        "#tracking_id": "tracking_id",
                        "#updated_at": "updated_at",
                    },
                    "ExpressionAttributeValues": {
                        ":tracking_id": tracking_id,
                        ":updated_at": now,
                    },
                }
            },
        ]

        try:
            self.dynamo.transact_write_items(operations)
        except ClientError as error:
            self._raise_transaction_error(
                error,
                session_key=session_key,
                tracking_code=tracking_code,
            )

        invalidate_sessions_cache()

        self._audit_event(
            AUDIT_TRACKING_CREATED,
            actor_ctx,
            tracking,
            session_id=first_session_id,
        )

        return tracking

    # ==========================================================
    # LISTAR Y OBTENER SEGUIMIENTOS
    # ==========================================================

    def list_trackings(
        self,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> list[dict]:
        """Lista los seguimientos del profesor autenticado.

        Si la petición está autenticada, solo se devuelven los seguimientos
        creados por ese profesor (aislamiento por profesor).
        """

        trackings = self.dynamo.scan_all(
            self.trackings_table
        )

        solicitante = self._actor_uid(actor_ctx)

        if actor_ctx is not None:
            if not solicitante:
                return []
            trackings = [
                item
                for item in trackings
                if str(item.get("created_by") or "") == solicitante
            ]

        return sorted(
            trackings,
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )

    def get_tracking(
        self,
        tracking_id: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        return self._require_tracking(tracking_id, actor_ctx)

    # ==========================================================
    # SESIONES DE UN SEGUIMIENTO
    # ==========================================================

    def get_tracking_sessions(
        self,
        tracking_id: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> list[dict]:
        self._require_tracking(tracking_id, actor_ctx)

        sessions = self.dynamo.query_all(
            self.sessions_table,
            Key("tracking_id").eq(str(tracking_id)),
            index_name=self.SESSIONS_TRACKING_INDEX,
        )
        sessions = self._sessions_for_actor(sessions, actor_ctx)

        result = [
            self._session_summary(session)
            for session in sessions
        ]

        return sorted(
            result,
            key=lambda item: (
                item.get("fecha") or "",
                item.get("hora") or "",
            ),
            reverse=True,
        )

    # ==========================================================
    # SEGUIMIENTO DE LAS EXPERIENCIAS DE UN SEGUIMIENTO
    # ==========================================================

    def get_follow_up(
        self,
        tracking_id: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> list[dict]:
        """Devuelve el historial de las experiencias del seguimiento.

        Cada experiencia incorporada genera UN registro (clave ``session_id``),
        por lo que reprocesar una sesión NO produce duplicados: se actualiza el
        registro existente. Los resultados se ordenan cronológicamente
        (ascendente: del más antiguo al más reciente).

        EmotionLab no identifica alumnos: el seguimiento es una colección de
        experiencias independientes. Cada registro conserva su propio
        ``session_id`` y NUNCA se mezclan datos de otros seguimientos.
        """

        tracking = self._require_tracking(tracking_id, actor_ctx)

        sessions = self.dynamo.query_all(
            self.sessions_table,
            Key("tracking_id").eq(str(tracking_id)),
            index_name=self.SESSIONS_TRACKING_INDEX,
        )
        sessions = self._sessions_for_actor(sessions, actor_ctx)

        registros: dict[str, dict] = {}

        for session in sessions:
            session_id = session.get("session_id")

            if not session_id:
                continue

            registro = self._session_summary(session)
            registro["experience_id"] = tracking.get("tracking_id")
            registro["experience_code"] = tracking.get("tracking_code")

            # La clave es el `session_id` original: reprocesar actualiza el
            # mismo registro en lugar de crear uno nuevo.
            registros[str(session_id)] = registro

        return sorted(
            registros.values(),
            key=lambda item: (
                item.get("fecha") or "",
                item.get("hora") or "",
            ),
        )

    # ==========================================================
    # EVOLUCIÓN DEL BIENESTAR (GRÁFICO LONGITUDINAL)
    # ==========================================================

    def get_evolution(
        self,
        tracking_id: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Evolución cronológica del bienestar promedio de las experiencias.

        Cada punto representa UNA sesión procesada (nunca una sesión repetida
        ni un reprocesamiento). El valor de bienestar se LEE de los resultados
        V2 ya persistidos; NUNCA se vuelve a ejecutar el modelo.

        La evolución es el historial de las experiencias incorporadas al
        seguimiento (identificadas por su ``session_id``), no una evolución
        longitudinal de un alumno: EmotionLab no dispone de esa identificación.
        """

        tracking = self._require_tracking(tracking_id, actor_ctx)

        sessions = self.dynamo.query_all(
            self.sessions_table,
            Key("tracking_id").eq(str(tracking_id)),
            index_name=self.SESSIONS_TRACKING_INDEX,
        )
        sessions = self._sessions_for_actor(sessions, actor_ctx)

        # Se deduplica por `session_id`: reprocesar no crea puntos nuevos.
        por_sesion: dict[str, dict] = {}

        for session in sessions:
            session_id = session.get("session_id")

            if not session_id:
                continue

            por_sesion[str(session_id)] = session

        # Orden CRONOLÓGICO (ascendente): del más antiguo al más reciente.
        ordenadas = sorted(
            por_sesion.values(),
            key=lambda session: (
                session.get("fecha") or "",
                session.get("hora") or "",
                str(session.get("session_id") or ""),
            ),
        )

        puntos = []

        for indice, session in enumerate(ordenadas, start=1):
            puntos.append(
                {
                    "session_number": indice,
                    "session_id": session.get("session_id"),
                    "session_date": self._session_date(session),
                    "wellbeing": bienestar_promedio(
                        session.get("model_processing")
                    ),
                }
            )

        return {
            "tracking_id": tracking.get("tracking_id"),
            "total_sessions": len(puntos),
            "sessions": puntos,
        }

    # ==========================================================
    # SESIONES DISPONIBLES
    # ==========================================================

    def list_available_sessions(
        self,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> list[dict]:
        sessions = self.sessions.list_sessions()
        available = []
        sessions = self._sessions_for_actor(sessions, actor_ctx)

        for session in sessions:
            session_id = session.get("session_id")

            if not session_id or session.get("tracking_id"):
                continue

            # Excluye sesiones legacy que no existen en la tabla DynamoDB.
            if not self.sessions._resolve_session_key(
                str(session_id)
            ):
                continue

            available.append(
                self._session_summary(session)
            )

        return sorted(
            available,
            key=lambda item: (
                item.get("fecha") or "",
                item.get("hora") or "",
            ),
            reverse=True,
        )

    # ==========================================================
    # ASOCIAR SESIÓN A UN SEGUIMIENTO
    # ==========================================================

    def add_session(
        self,
        tracking_id: str,
        session_id: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        tracking = self._require_tracking(tracking_id, actor_ctx)

        session_key = self._require_session_key(
            session_id
        )

        session = self.dynamo.get_item(
            self.sessions_table,
            session_key,
        )

        if not session:
            raise ValueError(
                "No se encontró la sesión seleccionada."
            )

        self._ensure_v2_processed(session)
        self._require_session_owner(session, actor_ctx)

        if session.get("tracking_id"):
            raise ValueError(
                "La sesión ya está asociada a un seguimiento."
            )

        now = self._now_iso()

        operations = [
            {
                "Update": {
                    "TableName": self.sessions_table,
                    "Key": session_key,
                    "UpdateExpression": (
                        "SET #tracking_id = :tracking_id, "
                        "#updated_at = :updated_at"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(#session_id) "
                        "AND attribute_not_exists(#tracking_id)"
                    ),
                    "ExpressionAttributeNames": {
                        "#session_id": "session_id",
                        "#tracking_id": "tracking_id",
                        "#updated_at": "updated_at",
                    },
                    "ExpressionAttributeValues": {
                        ":tracking_id": str(tracking_id),
                        ":updated_at": now,
                    },
                }
            },
        ]

        try:
            self.dynamo.transact_write_items(operations)
        except ClientError as error:
            self._raise_transaction_error(
                error,
                session_key=session_key,
                tracking_id=str(tracking_id),
            )

        invalidate_sessions_cache()

        self._audit_event(
            AUDIT_TRACKING_SESSION_ADDED,
            actor_ctx,
            tracking,
            session_id=session_id,
        )

        return {
            "tracking_id": str(tracking_id),
            "session_id": session_id,
            "message": "Sesión asociada correctamente.",
        }

    # ==========================================================
    # ASOCIAR VARIAS SESIONES EN UNA SOLA OPERACIÓN ATÓMICA
    # ==========================================================

    # DynamoDB admite como máximo 100 acciones por transacción.
    MAX_TRANSACTION_ACTIONS = 100

    def add_sessions(
        self,
        tracking_id: str,
        session_ids: list[str],
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Incorpora varias sesiones a un seguimiento de forma atómica.

        Todas las sesiones se validan ANTES de escribir: si una falla, no se
        incorpora ninguna. Los duplicados, incluidas las sesiones ya
        vinculadas al seguimiento, se rechazan. La escritura se realiza en
        una única transacción, de modo que respeta la atomicidad de DynamoDB.
        """

        tracking = self._require_tracking(tracking_id, actor_ctx)

        if not isinstance(session_ids, (list, tuple)) or not session_ids:
            raise ValueError(
                "Debes indicar una lista de session_id."
            )

        # 1. Normalizar y rechazar duplicados antes de consultar o escribir.
        unicos: list[str] = []
        vistos: set[str] = set()

        for raw in session_ids:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(
                    "Todos los session_id deben ser texto válido."
                )

            session_id = raw.strip()

            if session_id in vistos:
                raise ValueError(
                    "La lista contiene session_id duplicados."
                )

            vistos.add(session_id)
            unicos.append(session_id)

        # 2. Validar TODAS las sesiones antes de tocar DynamoDB.
        preparadas: list[tuple[str, dict]] = []

        for session_id in unicos:
            session_key = self._require_session_key(session_id)

            session = self.dynamo.get_item(
                self.sessions_table,
                session_key,
            )

            if not session:
                raise ValueError(
                    "No se encontró la sesión seleccionada."
                )

            self._require_session_owner(session, actor_ctx)
            self._ensure_v2_processed(session)

            actual = session.get("tracking_id")

            if actual == str(tracking_id):
                raise ValueError(
                    "La sesión ya está asociada a este seguimiento."
                )

            if actual:
                raise ValueError(
                    "La sesión ya está asociada a otro seguimiento."
                )

            preparadas.append((session_id, session_key))

        if not preparadas:
            return {
                "tracking_id": str(tracking_id),
                "added": [],
                "already_linked": [],
                "message": (
                    "Ninguna sesión nueva para incorporar."
                ),
            }

        if len(preparadas) > self.MAX_TRANSACTION_ACTIONS:
            raise ValueError(
                "No se pueden incorporar más de "
                f"{self.MAX_TRANSACTION_ACTIONS} sesiones "
                "en una sola operación."
            )

        now = self._now_iso()
        operations = []

        for session_id, session_key in preparadas:
            operations.append(
                {
                    "Update": {
                        "TableName": self.sessions_table,
                        "Key": session_key,
                        "UpdateExpression": (
                            "SET #tracking_id = :tracking_id, "
                            "#updated_at = :updated_at"
                        ),
                        "ConditionExpression": (
                            "attribute_exists(#session_id) "
                            "AND attribute_not_exists(#tracking_id)"
                        ),
                        "ExpressionAttributeNames": {
                            "#session_id": "session_id",
                            "#tracking_id": "tracking_id",
                            "#updated_at": "updated_at",
                        },
                        "ExpressionAttributeValues": {
                            ":tracking_id": str(tracking_id),
                            ":updated_at": now,
                        },
                    }
                }
            )

        try:
            self.dynamo.transact_write_items(operations)
        except ClientError as error:
            self._raise_transaction_error(
                error,
                tracking_id=str(tracking_id),
            )

        invalidate_sessions_cache()

        for session_id, _ in preparadas:
            self._audit_event(
                AUDIT_TRACKING_SESSION_ADDED,
                actor_ctx,
                tracking,
                session_id=session_id,
            )

        return {
            "tracking_id": str(tracking_id),
            "added": [session_id for session_id, _ in preparadas],
            "already_linked": [],
            "message": (
                f"{len(preparadas)} sesiones incorporadas correctamente."
            ),
        }

    # ==========================================================
    # DESVINCULAR SESIÓN SIN ELIMINARLA
    # ==========================================================

    def remove_session(
        self,
        tracking_id: str,
        session_id: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        tracking = self._require_tracking(tracking_id, actor_ctx)

        session_key = self._require_session_key(
            session_id
        )

        session = self.dynamo.get_item(
            self.sessions_table,
            session_key,
        )

        if not session:
            raise ValueError(
                "No se encontró la sesión seleccionada."
            )

        self._require_session_owner(session, actor_ctx)

        if session.get("tracking_id") != str(tracking_id):
            raise ValueError(
                "La sesión no pertenece a este seguimiento."
            )

        now = self._now_iso()

        operations = [
            {
                "Update": {
                    "TableName": self.sessions_table,
                    "Key": session_key,
                    "UpdateExpression": (
                        "SET #updated_at = :updated_at "
                        "REMOVE #tracking_id"
                    ),
                    "ConditionExpression": (
                        "attribute_exists(#session_id) "
                        "AND #tracking_id = :tracking_id"
                    ),
                    "ExpressionAttributeNames": {
                        "#session_id": "session_id",
                        "#tracking_id": "tracking_id",
                        "#updated_at": "updated_at",
                    },
                    "ExpressionAttributeValues": {
                        ":tracking_id": str(tracking_id),
                        ":updated_at": now,
                    },
                }
            },
        ]

        try:
            self.dynamo.transact_write_items(operations)
        except ClientError as error:
            self._raise_transaction_error(
                error,
                session_key=session_key,
                tracking_id=str(tracking_id),
            )

        invalidate_sessions_cache()

        self._audit_event(
            AUDIT_TRACKING_SESSION_REMOVED,
            actor_ctx,
            tracking,
            session_id=session_id,
        )

        return {
            "tracking_id": str(tracking_id),
            "session_id": session_id,
            "message": "Sesión desvinculada correctamente.",
        }

    # ==========================================================
    # ELIMINAR UN SEGUIMIENTO (NUNCA LAS SESIONES)
    # ==========================================================

    def delete_tracking(
        self,
        tracking_id: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Elimina el seguimiento y desvincula sus sesiones.

        Se elimina únicamente la asociación y el registro del seguimiento:
        las sesiones originales y sus resultados V2 NUNCA se borran.
        """

        tracking = self._require_tracking(tracking_id, actor_ctx)

        sessions = self.dynamo.query_all(
            self.sessions_table,
            Key("tracking_id").eq(str(tracking_id)),
            index_name=self.SESSIONS_TRACKING_INDEX,
        )

        if actor_ctx is not None:
            for session in sessions:
                self._require_session_owner(session, actor_ctx)

        now = self._now_iso()
        operations = []

        for session in sessions:
            participant_id = session.get("participant_id")
            sk = session.get("sk")

            if not participant_id or not sk:
                continue

            operations.append(
                {
                    "Update": {
                        "TableName": self.sessions_table,
                        "Key": {
                            "participant_id": participant_id,
                            "sk": sk,
                        },
                        "UpdateExpression": (
                            "SET #updated_at = :updated_at "
                            "REMOVE #tracking_id"
                        ),
                        "ConditionExpression": (
                            "#tracking_id = :tracking_id"
                        ),
                        "ExpressionAttributeNames": {
                            "#tracking_id": "tracking_id",
                            "#updated_at": "updated_at",
                        },
                        "ExpressionAttributeValues": {
                            ":tracking_id": str(tracking_id),
                            ":updated_at": now,
                        },
                    }
                }
            )

        operations.append(
            {
                "Delete": {
                    "TableName": self.trackings_table,
                    "Key": {
                        "tracking_code": tracking.get("tracking_code"),
                    },
                }
            }
        )

        try:
            self.dynamo.transact_write_items(operations)
        except ClientError as error:
            self._raise_transaction_error(
                error,
                tracking_id=str(tracking_id),
            )

        invalidate_sessions_cache()

        self._audit_event(
            AUDIT_TRACKING_DELETED,
            actor_ctx,
            tracking,
        )

        return {
            "tracking_id": str(tracking_id),
            "message": "Seguimiento eliminado correctamente.",
        }
