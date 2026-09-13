"""Auditoría centralizada en DynamoDB (time-series).

ETAPA 2:
- PK = audit_month (YYYY-MM) para evitar hot partitions en escrituras.
- SK = <iso_timestamp>#<uuid4_short> orden estricto dentro del mes.
- 4 GSIs para consultas eficientes por actor, accion, session, report_request.
- NUNCA almacenar passwords ni PII sensible (solo participant_id anonimizado).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from boto3.dynamodb.conditions import Key

from core.constants import (
    AUDIT_LOGIN_FAIL,
    AUDIT_LOGIN_OK,
    AUDIT_PASSWORD_CHANGED,
    AUDIT_PASSWORD_RESET,
    AUDIT_PARTICIPANT_CREATED,
    AUDIT_REPORT_APPROVED,
    AUDIT_REPORT_DOWNLOADED,
    AUDIT_REPORT_REJECTED,
    AUDIT_REPORT_REQUESTED,
    AUDIT_SESSION_ASSIGNED,
    AUDIT_SESSIONS_IMPORTED,
    AUDIT_USER_CREATED,
    AUDIT_USER_DELETED,
    AUDIT_USER_DISABLED,
    AUDIT_USER_ENABLED,
    AUDIT_USER_UPDATED,
    DYNAMODB_TABLE_AUDIT_LOGS,
    PK_TYPE_AUDIT_LOG,
)
from services.dynamodb_service import DynamoDBService, dynamodb


class AuditService:
    """Registra y consulta eventos de auditoría en DynamoDB."""

    def __init__(
        self,
        dynamodb_service: Optional[DynamoDBService] = None,
    ):
        self.dynamo: DynamoDBService = dynamodb_service or dynamodb
        self.table = DYNAMODB_TABLE_AUDIT_LOGS

    # ==========================================================
    # PRIMITIVA DE LOG
    # ==========================================================

    def log(
        self,
        action: str,
        *,
        actor_uid: Optional[str] = None,
        actor_email: Optional[str] = None,
        actor_rol: Optional[str] = None,
        participant_id: Optional[str] = None,
        session_id: Optional[str] = None,
        report_request_id: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> dict:
        """Registra un evento de auditoría. Retorna el item almacenado."""
        if not action:
            raise ValueError("action es obligatorio")
        ts = timestamp or datetime.now(timezone.utc)
        audit_month = ts.strftime("%Y-%m")
        iso_ts = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        short_id = uuid.uuid4().hex[:10]
        sk = f"{iso_ts}#{short_id}"

        item: dict[str, Any] = {
            "PK": f"AUDIT#{audit_month}",
            "SK": sk,
            "audit_month": audit_month,
            "timestamp": iso_ts,
            "iso_timestamp": iso_ts,
            "action": action,
            "pk_type": PK_TYPE_AUDIT_LOG,
        }
        if actor_uid:
            item["actor_uid"] = actor_uid
        if actor_email:
            item["actor_email"] = actor_email.lower()
        if actor_rol:
            item["actor_rol"] = actor_rol
        if participant_id:
            item["participant_id"] = participant_id.upper()
        if session_id:
            item["session_id"] = session_id
        if report_request_id:
            item["report_request_id"] = report_request_id
        if extra:
            safe_extra = self._sanitize_extra(extra)
            if safe_extra:
                item["extra"] = safe_extra
        self.dynamo.put_item(self.table, item)
        return item

    @staticmethod
    def _sanitize_extra(extra: Mapping[str, Any]) -> dict[str, Any]:
        """Conserva contexto simple y descarta secretos antes de persistir."""
        sensitive_markers = (
            "password", "passwd", "token", "secret", "credential",
            "authorization", "api_key", "apikey", "private_key",
            "aws_access", "fernet", "jwt", "session",
        )
        sanitized: dict[str, Any] = {}
        for raw_key, value in dict(extra).items():
            key = str(raw_key)
            normalized_key = key.lower().replace("-", "_")
            if any(marker in normalized_key for marker in sensitive_markers):
                continue
            if value is None or isinstance(value, (bool, int, float)):
                sanitized[key] = value
            elif isinstance(value, str) and len(value) <= 1024:
                sanitized[key] = value
            elif isinstance(value, (list, tuple)) and len(value) <= 50 and all(
                item is None or isinstance(item, (bool, int, float))
                or (isinstance(item, str) and len(item) <= 256)
                for item in value
            ):
                sanitized[key] = list(value)
        return sanitized

    # ==========================================================
    # HELPERS TIPIFICADOS (para mapear legacy log_activity strings)
    # ==========================================================

    def log_login_ok(
        self, actor_uid: str, actor_email: str, actor_rol: str, **extra
    ):
        return self.log(
            AUDIT_LOGIN_OK,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            extra=extra,
        )

    def log_login_fail(self, actor_email: str, reason: Optional[str] = None, **extra):
        return self.log(
            AUDIT_LOGIN_FAIL,
            actor_email=actor_email,
            extra={"reason": reason} if reason else None,
        )

    def log_user_created(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        target_uid: str, target_email: str, target_rol: str, **extra,
    ):
        return self.log(
            AUDIT_USER_CREATED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            extra={
                "target_uid": target_uid,
                "target_email": target_email,
                "target_rol": target_rol,
                **extra,
            },
        )

    def log_user_updated(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        target_uid: str, changes: Optional[list[str]] = None, **extra,
    ):
        return self.log(
            AUDIT_USER_UPDATED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            extra={"target_uid": target_uid, "changes": changes or [], **extra},
        )

    def log_user_deleted(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        target_uid: str, **extra,
    ):
        return self.log(
            AUDIT_USER_DELETED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            extra={"target_uid": target_uid, **extra},
        )

    def log_user_enabled(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        target_uid: str, **extra,
    ):
        return self.log(
            AUDIT_USER_ENABLED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            extra={"target_uid": target_uid, **extra},
        )

    def log_user_disabled(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        target_uid: str, **extra,
    ):
        return self.log(
            AUDIT_USER_DISABLED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            extra={"target_uid": target_uid, **extra},
        )

    def log_password_changed(self, actor_uid: str, actor_email: str, actor_rol: str, **extra):
        return self.log(
            AUDIT_PASSWORD_CHANGED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            extra=extra,
        )

    def log_password_reset(self, actor_email: str, **extra):
        return self.log(AUDIT_PASSWORD_RESET, actor_email=actor_email, extra=extra)

    def log_sessions_imported(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        count: int, **extra,
    ):
        return self.log(
            AUDIT_SESSIONS_IMPORTED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            extra={"count": count, **extra},
        )

    def log_participant_created(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        participant_id: str, **extra,
    ):
        return self.log(
            AUDIT_PARTICIPANT_CREATED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            participant_id=participant_id,
            extra=extra,
        )

    def log_session_assigned(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        session_id: str, participant_id: str, **extra,
    ):
        return self.log(
            AUDIT_SESSION_ASSIGNED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            session_id=session_id, participant_id=participant_id,
            extra=extra,
        )

    def log_report_requested(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        report_request_id: str, session_id: Optional[str] = None,
        participant_id: Optional[str] = None, **extra,
    ):
        return self.log(
            AUDIT_REPORT_REQUESTED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            report_request_id=report_request_id,
            session_id=session_id, participant_id=participant_id,
            extra=extra,
        )

    def log_report_approved(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        report_request_id: str, session_id: Optional[str] = None,
        participant_id: Optional[str] = None, **extra,
    ):
        return self.log(
            AUDIT_REPORT_APPROVED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            report_request_id=report_request_id,
            session_id=session_id,
            participant_id=participant_id,
            extra=extra,
        )

    def log_report_rejected(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        report_request_id: str, reason: Optional[str] = None, **extra,
    ):
        return self.log(
            AUDIT_REPORT_REJECTED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            report_request_id=report_request_id,
            extra={"reason": reason, **extra} if reason else extra,
        )

    def log_report_downloaded(
        self, actor_uid: str, actor_email: str, actor_rol: str,
        report_request_id: str, session_id: Optional[str] = None,
        participant_id: Optional[str] = None, **extra,
    ):
        return self.log(
            AUDIT_REPORT_DOWNLOADED,
            actor_uid=actor_uid, actor_email=actor_email, actor_rol=actor_rol,
            report_request_id=report_request_id,
            session_id=session_id,
            participant_id=participant_id,
            extra=extra,
        )

    # ==========================================================
    # CONSULTAS PAGINADAS EFICIENTES
    # ==========================================================

    def list_month(
        self,
        year: int,
        month: int,
        *,
        limit: int = 100,
        last_evaluated_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict], Optional[dict]]:
        """Eventos de un mes (YYYY-MM), orden cronológico descendente."""
        audit_month = f"{int(year):04d}-{int(month):02d}"
        return self.dynamo.query(
            self.table,
            Key("PK").eq(f"AUDIT#{audit_month}"),
            ScanIndexForward=False,
            limit=limit,
            exclusive_start_key=last_evaluated_key,
        )

    def list_by_actor(
        self,
        actor_uid: str,
        *,
        limit: int = 100,
        last_evaluated_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict], Optional[dict]]:
        """Eventos por usuario actor, vía gsi-audit-actor."""
        return self.dynamo.query(
            self.table,
            Key("actor_uid").eq(actor_uid),
            index_name="gsi-audit-actor-date",
            ScanIndexForward=False,
            limit=limit,
            exclusive_start_key=last_evaluated_key,
        )

    def list_by_action(
        self,
        action: str,
        *,
        limit: int = 100,
        last_evaluated_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict], Optional[dict]]:
        """Eventos por acción, vía gsi-audit-action."""
        return self.dynamo.query(
            self.table,
            Key("action").eq(action),
            index_name="gsi-audit-action-month",
            ScanIndexForward=False,
            limit=limit,
            exclusive_start_key=last_evaluated_key,
        )

    def list_by_session(
        self,
        session_id: str,
        *,
        limit: int = 100,
        last_evaluated_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict], Optional[dict]]:
        """Eventos relacionados con una sesión, vía gsi-audit-session."""
        return self.dynamo.query(
            self.table,
            Key("session_id").eq(session_id),
            index_name="gsi-audit-session",
            ScanIndexForward=False,
            limit=limit,
            exclusive_start_key=last_evaluated_key,
        )

    def list_by_participant(
        self,
        participant_id: str,
        *,
        limit: int = 100,
        last_evaluated_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict], Optional[dict]]:
        """Eventos de un participante mediante el GSI definido en Terraform."""
        return self.dynamo.query(
            self.table,
            Key("participant_id").eq(participant_id.upper()),
            index_name="gsi-audit-participant",
            ScanIndexForward=False,
            limit=limit,
            exclusive_start_key=last_evaluated_key,
        )

    def list_by_report_request(
        self,
        report_request_id: str,
        *,
        limit: int = 100,
        last_evaluated_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict], Optional[dict]]:
        """Eventos relacionados con una solicitud de informe, vía gsi-audit-report."""
        return self.dynamo.query(
            self.table,
            Key("report_request_id").eq(report_request_id),
            index_name="gsi-audit-report",
            ScanIndexForward=False,
            limit=limit,
            exclusive_start_key=last_evaluated_key,
        )

    def list_by_target_user(
        self,
        target_uid: str,
        *,
        limit: int = 100,
        last_evaluated_key: Optional[Mapping[str, Any]] = None,
    ) -> tuple[list[dict], Optional[dict]]:
        """Eventos cuyo usuario afectado es ``target_uid``."""
        return self.dynamo.query(
            self.table,
            Key("target_uid").eq(target_uid),
            index_name="gsi-audit-targetuser",
            ScanIndexForward=False,
            limit=limit,
            exclusive_start_key=last_evaluated_key,
        )

    def list_by_actor_or_target(self, uid: str, *, limit: int = 100) -> list[dict]:
        """Une actividad propia y actividad administrativa sobre un usuario."""
        limit = max(1, min(int(limit), 500))
        actor_items, _ = self.list_by_actor(uid, limit=limit)
        target_items, _ = self.list_by_target_user(uid, limit=limit)
        unique = {
            (item.get("PK"), item.get("SK")): item
            for item in [*actor_items, *target_items]
        }
        return sorted(
            unique.values(),
            key=lambda item: item.get("timestamp", item.get("iso_timestamp", "")),
            reverse=True,
        )[:limit]


# ==========================================================
# FÁBRICA / INSTANCIA GLOBAL (se inyecta vía DI pero existe fallback)
# ==========================================================

_audit_singleton: Optional[AuditService] = None


def audit_service() -> AuditService:
    global _audit_singleton
    if _audit_singleton is None:
        _audit_singleton = AuditService()
    return _audit_singleton


def audit(*args, **kwargs) -> dict:
    """Atajo conveniente para log() cuando no hay DI (legacy)."""
    return audit_service().log(*args, **kwargs)
