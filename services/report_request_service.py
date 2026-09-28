"""
Solicitudes de informes PDF.

Flujo:

    PENDING
       ↓
    APPROVED / REJECTED
       ↓
    COMPLETED / ERROR

Una solicitud COMPLETED permite una única descarga.

Después de utilizar la descarga, el especialista debe crear
una nueva solicitud y volver a pasar por aprobación.

Los PDFs se almacenan en S3 privado.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional

from boto3.dynamodb.conditions import Attr, Key

from core.constants import (
    DYNAMODB_TABLE_REPORT_REQUESTS,
    PK_TYPE_REPORT_REQUEST,
)
from core.exceptions import SessionNotFound
from core.roles import ROLE_ADMIN

from services.dynamodb_service import (
    DynamoDBService,
    dynamodb,
)


# ==========================================================
# ESTADOS
# ==========================================================

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_COMPLETED = "COMPLETED"
STATUS_ERROR = "ERROR"

VALID_REPORT_REQUEST_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_APPROVED,
        STATUS_REJECTED,
        STATUS_COMPLETED,
        STATUS_ERROR,
    }
)


# ==========================================================
# IMPORTS OPCIONALES
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
    from services.report_service import ReportService
except Exception:  # pragma: no cover

    ReportService = None  # type: ignore


try:
    from services.session_service import SessionService
except Exception:  # pragma: no cover

    SessionService = None  # type: ignore


try:
    from services.s3_service import (
        S3Service,
        s3_service as _s3_default,
    )
except Exception:  # pragma: no cover

    S3Service = None  # type: ignore

    def _s3_default():
        return None


try:
    from services.ses_service import (
        SESService,
        ses_service as _ses_default,
    )
except Exception:  # pragma: no cover

    SESService = None  # type: ignore

    def _ses_default():
        return None


class ReportRequestService:
    """
    Administra el ciclo de vida de las solicitudes de informes.

    DynamoDB:

        PK real:
            requester_uid

        SK:
            REQUEST#<request_id>

    Índices:

        gsi-report-id
        gsi-report-status-date
        gsi-report-session
    """

    def __init__(
        self,
        dynamodb_service: Optional[
            DynamoDBService
        ] = None,
        audit_svc: Optional[
            "AuditService"
        ] = None,
        report_service: Optional[
            "ReportService"
        ] = None,
        session_service: Optional[
            "SessionService"
        ] = None,
        s3_service: Optional[
            "S3Service"
        ] = None,
        ses_service: Optional[
            "SESService"
        ] = None,
    ):

        self.dynamo = (
            dynamodb_service
            or dynamodb
        )

        self.table = (
            DYNAMODB_TABLE_REPORT_REQUESTS
        )

        # ------------------------------------------------------
        # AUDIT
        # ------------------------------------------------------

        if audit_svc is not None:
            self.audit = audit_svc
        else:
            try:
                self.audit = audit_service()
            except Exception:
                self.audit = None

        # ------------------------------------------------------
        # REPORT
        # ------------------------------------------------------

        if report_service is not None:
            self.report_service = (
                report_service
            )
        else:
            try:
                self.report_service = (
                    ReportService()
                    if ReportService
                    else None
                )
            except Exception:
                self.report_service = None

        # ------------------------------------------------------
        # SESSION
        # ------------------------------------------------------

        if session_service is not None:
            self.session_service = (
                session_service
            )
        else:
            try:
                self.session_service = (
                    SessionService()
                    if SessionService
                    else None
                )
            except Exception:
                self.session_service = None

        # ------------------------------------------------------
        # S3
        # ------------------------------------------------------

        if s3_service is not None:
            self.s3 = s3_service
        else:
            try:
                self.s3 = _s3_default()
            except Exception:
                self.s3 = None

        # ------------------------------------------------------
        # SES
        # ------------------------------------------------------

        if ses_service is not None:
            self.ses = ses_service
        else:
            try:
                self.ses = _ses_default()
            except Exception:
                self.ses = None

    # ==========================================================
    # HELPERS
    # ==========================================================

    def _require_admin(
        self,
        reviewer_rol: str,
    ) -> None:

        if reviewer_rol != ROLE_ADMIN:
            raise PermissionError(
                "Solo administradores pueden revisar solicitudes"
            )

    def _validate(
        self,
        req: Optional[
            Mapping[str, Any]
        ],
    ) -> dict:

        if not req:
            raise ValueError(
                "Solicitud no existe"
            )

        return dict(req)

    def _has_active_request_for_session(
        self,
        session_id: str,
        requester_uid: str,
    ) -> bool:
        """
        Determina si el especialista ya tiene una solicitud
        activa para la misma sesión.

        Estados activos:

            PENDING
            APPROVED
            COMPLETED sin descarga

        Estados que permiten solicitar nuevamente:

            REJECTED
            ERROR
            COMPLETED ya descargado
        """

        if not session_id or not requester_uid:
            return False

        try:
            items, _ = self.list_my_requests(
                requester_uid,
                limit=500,
            )
        except Exception:
            return False

        for item in items:

            if item.get(
                "session_id"
            ) != session_id:
                continue

            status = item.get(
                "status"
            )

            if status in {
                STATUS_PENDING,
                STATUS_APPROVED,
            }:
                return True

            if (
                status == STATUS_COMPLETED
                and not item.get(
                    "downloaded_at"
                )
            ):
                return True

        return False

    # ==========================================================
    # CREAR SOLICITUD
    # ==========================================================

    def create_request(
        self,
        session_id: str,
        *,
        requester_uid: str,
        requester_email: str,
        requester_rol: str,
        motivo: Optional[str] = None,
    ) -> dict:
        """
        Crea una nueva solicitud PENDING.

        No permite duplicar una solicitud activa para
        la misma sesión y especialista.
        """

        if not session_id:
            raise ValueError(
                "session_id es obligatorio"
            )

        if (
            not requester_uid
            or not requester_email
        ):
            raise ValueError(
                "requester_uid y requester_email obligatorios"
            )

        if self.session_service is None:
            raise RuntimeError(
                "SessionService no disponible"
            )

        session = (
            self.session_service.get_session(
                session_id
            )
        )

        if not session:
            raise SessionNotFound(
                "La sesion no existe"
            )

        # ------------------------------------------------------
        # PARTICIPANTE
        # ------------------------------------------------------

        participant_id = session.get(
            "participant_id"
        )

        if (
            not participant_id
            or participant_id == "__PENDING__"
        ):
            raise ValueError(
                "La sesión no tiene un participante identificado. "
                "Asigne primero un participante antes de solicitar "
                "el informe."
            )

        # ------------------------------------------------------
        # EVITAR SOLICITUDES DUPLICADAS
        # ------------------------------------------------------

        if self._has_active_request_for_session(
            session_id,
            requester_uid,
        ):
            raise ValueError(
                "Ya existe una solicitud activa para esta sesión. "
                "Debe esperar su resolución o utilizar la descarga "
                "aprobada antes de solicitar nuevamente."
            )

        now = DynamoDBService.now_iso()

        request_id = uuid.uuid4().hex

        item = {
            "request_id": request_id,
            "report_request_id": request_id,
            "sk": f"REQUEST#{request_id}",

            "session_id": session_id,
            "participant_id": participant_id,

            "requester_uid": requester_uid,
            "requester_email": requester_email.lower(),
            "requester_rol": requester_rol,

            "motivo": motivo,

            "status": STATUS_PENDING,

            "reviewer_uid": None,
            "reviewer_email": None,
            "reviewer_rol": None,

            "review_comments": None,
            "rejection_reason": None,

            "report_s3_key": None,
            "report_filename": None,
            "report_bytes": None,

            "generation_error": None,

            # downloaded_at se crea solamente cuando la descarga
            # se consume. No se debe guardar como NULL porque
            # attribute_not_exists(downloaded_at) considera que
            # un atributo NULL igualmente existe en DynamoDB.

            "pk_type": PK_TYPE_REPORT_REQUEST,

            "created_at": now,
            "requested_at": now,
            "reviewed_at": None,
            "updated_at": now,
        }

        self.dynamo.put_item(
            self.table,
            item,
            condition_expression=Attr(
                "request_id"
            ).not_exists(),
        )

        # ------------------------------------------------------
        # AUDITORÍA
        # ------------------------------------------------------

        if self.audit:

            try:
                self.audit.log_report_requested(
                    actor_uid=requester_uid,
                    actor_email=requester_email,
                    actor_rol=requester_rol,
                    report_request_id=request_id,
                    session_id=session_id,
                    participant_id=participant_id,
                    extra={
                        "motivo": motivo or "",
                    },
                )

            except Exception:
                pass

        return item

    # ==========================================================
    # CONSULTAR UNA SOLICITUD
    # ==========================================================

    def get_request(
        self,
        request_id: str,
    ) -> Optional[dict]:

        if not request_id:
            return None

        try:

            items = self.dynamo.query_all(
                self.table,
                Key(
                    "report_request_id"
                ).eq(request_id),
                index_name="gsi-report-id",
            )

            if items:
                return items[0]

        except Exception as error:

            print(
                "Error buscando solicitud por "
                f"report_request_id {request_id}: "
                f"{error}"
            )

        return None

    # ==========================================================
    # HELPERS INTERNOS
    # ==========================================================

    @staticmethod
    def _sort_by_date_desc(items: list[dict]) -> list[dict]:
        """Ordena solicitudes por la fecha MÁS RECIENTE primero.

        La tabla usa `request_id` (UUID) como Sort Key, por lo que
        `scan_index_forward=False` NO ordena por fecha real. Se
        ordena en memoria usando, en este orden de prioridad:
            1. updated_at   (último cambio: aprobación, rechazo, ...)
            2. reviewed_at  (fecha de revisión del admin)
            3. downloaded_at(fecha en que el especialista lo descargó)
            4. requested_at (fecha de creación de la solicitud)
            5. created_at   (fallback)
        """

        def _rank(item: dict) -> str:
            return (
                item.get("updated_at")
                or item.get("reviewed_at")
                or item.get("downloaded_at")
                or item.get("requested_at")
                or item.get("created_at")
                or ""
            )

        return sorted(items, key=_rank, reverse=True)

    # ==========================================================
    # LISTAR
    # ==========================================================

    def list_requests(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 100,
        last_evaluated_key: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> tuple[
        list[dict],
        Optional[dict],
    ]:

        limit = max(
            1,
            min(int(limit), 500),
        )

        items: list[dict] = []
        last = None

        if status is None:
            items, last = self.dynamo.scan(
                self.table,
                limit=limit,
                exclusive_start_key=(
                    last_evaluated_key
                ),
            )

        else:
            status = status.upper()

            if (
                status
                not in VALID_REPORT_REQUEST_STATUSES
            ):
                raise ValueError(
                    f"status invalido: {status}"
                )

            try:

                items, last = self.dynamo.query(
                    self.table,
                    Key("status").eq(status),
                    index_name=(
                        "gsi-report-status-date"
                    ),
                    scan_index_forward=False,
                    limit=limit,
                    exclusive_start_key=(
                        last_evaluated_key
                    ),
                )

            except Exception:

                items, last = (
                    self.dynamo.scan(
                        self.table,
                        limit=limit * 2,
                        exclusive_start_key=(
                            last_evaluated_key
                        ),
                    )
                )

                items = [
                    item
                    for item in items
                    if item.get("status")
                    == status
                ]

        items = self._sort_by_date_desc(items)
        return items[:limit], last

    # ==========================================================
    # LISTAR SOLICITUDES DEL ESPECIALISTA
    # ==========================================================

    def list_my_requests(
        self,
        requester_uid: str,
        *,
        status: Optional[str] = None,
        limit: int = 100,
        last_evaluated_key: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> tuple[
        list[dict],
        Optional[dict],
    ]:

        if not requester_uid:
            return [], None

        items, last = self.dynamo.query(
            self.table,
            Key(
                "requester_uid"
            ).eq(requester_uid),
            scan_index_forward=False,
            limit=max(
                1,
                min(int(limit), 500),
            ),
            exclusive_start_key=(
                last_evaluated_key
            ),
        )

        if status:

            status = status.upper()

            if (
                status
                not in VALID_REPORT_REQUEST_STATUSES
            ):
                raise ValueError(
                    f"status invalido: {status}"
                )

            items = [
                item
                for item in items
                if item.get("status")
                == status
            ]

        items = self._sort_by_date_desc(items)
        return (
            items[:limit],
            last,
        )

    # ==========================================================
    # LISTAR POR SESIÓN
    # ==========================================================

    def list_by_session(
        self,
        session_id: str,
        *,
        limit: int = 100,
        last_evaluated_key: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> tuple[
        list[dict],
        Optional[dict],
    ]:
        """Obtiene solicitudes de una sesión."""

        if not session_id:
            return [], None

        items, last = self.dynamo.query(
            self.table,
            Key(
                "session_id"
            ).eq(session_id),
            index_name="gsi-report-session",
            scan_index_forward=False,
            limit=max(
                1,
                min(int(limit), 500),
            ),
            exclusive_start_key=(
                last_evaluated_key
            ),
        )
        items = self._sort_by_date_desc(items)
        return items[:limit], last

    # ==========================================================
    # APROBAR
    # ==========================================================

    def approve_request(
        self,
        request_id: str,
        *,
        reviewer_uid: str,
        reviewer_email: str,
        reviewer_rol: str,
        comments: Optional[str] = None,
    ) -> dict:

        self._require_admin(
            reviewer_rol
        )

        req = self._validate(
            self.get_request(
                request_id
            )
        )

        current_status = req.get(
            "status"
        )

        if current_status != STATUS_PENDING:
            raise ValueError(
                "Solo se puede aprobar una solicitud "
                f"que esté PENDING. Estado actual: "
                f"{current_status}"
            )

        now = DynamoDBService.now_iso()

        approval_updates = {
            "status": STATUS_APPROVED,
            "reviewer_uid": reviewer_uid,
            "reviewer_email": reviewer_email.lower(),
            "reviewer_rol": reviewer_rol,
            "review_comments": comments,
            "reviewed_at": now,
            "updated_at": now,
            "generation_error": None,
        }

        self.dynamo.update_item(
            self.table,
            key={
                "requester_uid": (
                    req["requester_uid"]
                ),
                "sk": req["sk"],
            },
            update_data=approval_updates,
        )

        req.update(
            approval_updates
        )

        # ------------------------------------------------------
        # AUDITORÍA
        # ------------------------------------------------------

        if self.audit:

            try:

                self.audit.log_report_approved(
                    actor_uid=reviewer_uid,
                    actor_email=reviewer_email,
                    actor_rol=reviewer_rol,
                    report_request_id=request_id,
                    session_id=req.get(
                        "session_id"
                    ),
                    participant_id=req.get(
                        "participant_id"
                    ),
                )

            except Exception:
                pass

        # ------------------------------------------------------
        # GENERAR PDF
        # ------------------------------------------------------

        try:

            self._generate_and_store_pdf(
                request_id,
                reviewer_ctx={
                    "actor_uid": reviewer_uid,
                    "actor_email": reviewer_email,
                    "actor_rol": reviewer_rol,
                },
            )

        except Exception as exc:

            error_message = str(exc)

            error_updates = {
                "status": STATUS_ERROR,
                "generation_error": (
                    error_message
                ),
                "updated_at": (
                    DynamoDBService.now_iso()
                ),
            }

            try:

                self.dynamo.update_item(
                    self.table,
                    key={
                        "requester_uid": (
                            req["requester_uid"]
                        ),
                        "sk": req["sk"],
                    },
                    update_data=error_updates,
                )

            except Exception:
                pass

            req.update(
                error_updates
            )

            if (
                self.ses
                and req.get(
                    "requester_email"
                )
            ):

                try:

                    self.ses.send_report_notification(
                        to_email=(
                            req[
                                "requester_email"
                            ]
                        ),
                        request_id=request_id,
                        session_id=req.get(
                            "session_id",
                            "",
                        ),
                        approved=False,
                        comments=(
                            "La solicitud fue aprobada, "
                            "pero ocurrió un error al "
                            "generar el informe. "
                            "Estado: ERROR."
                        ),
                    )

                except Exception:
                    pass

            return (
                self.get_request(
                    request_id
                )
                or req
            )

        # ------------------------------------------------------
        # NOTIFICACIÓN
        # ------------------------------------------------------

        final_req = (
            self.get_request(
                request_id
            )
            or req
        )

        if (
            self.ses
            and final_req.get(
                "requester_email"
            )
        ):

            try:

                self.ses.send_report_notification(
                    to_email=(
                        final_req[
                            "requester_email"
                        ]
                    ),
                    request_id=request_id,
                    session_id=final_req.get(
                        "session_id",
                        "",
                    ),
                    approved=True,
                    comments=comments,
                )

            except Exception:
                pass

        return final_req

    # ==========================================================
    # RECHAZAR
    # ==========================================================

    def reject_request(
        self,
        request_id: str,
        *,
        reviewer_uid: str,
        reviewer_email: str,
        reviewer_rol: str,
        reason: str,
    ) -> dict:

        self._require_admin(
            reviewer_rol
        )

        if (
            not reason
            or not isinstance(
                reason,
                str,
            )
            or not reason.strip()
        ):
            raise ValueError(
                "El motivo de rechazo es obligatorio"
            )

        req = self._validate(
            self.get_request(
                request_id
            )
        )

        current_status = req.get(
            "status"
        )

        if current_status != STATUS_PENDING:
            raise ValueError(
                "Solo se puede rechazar una solicitud "
                f"PENDING. Estado actual: "
                f"{current_status}"
            )

        now = DynamoDBService.now_iso()

        updates = {
            "status": STATUS_REJECTED,
            "reviewer_uid": reviewer_uid,
            "reviewer_email": reviewer_email.lower(),
            "reviewer_rol": reviewer_rol,
            "rejection_reason": reason.strip(),
            "reviewed_at": now,
            "updated_at": now,
        }

        self.dynamo.update_item(
            self.table,
            key={
                "requester_uid": (
                    req["requester_uid"]
                ),
                "sk": req["sk"],
            },
            update_data=updates,
        )

        req.update(
            updates
        )

        # ------------------------------------------------------
        # AUDITORÍA
        # ------------------------------------------------------

        if self.audit:

            try:

                self.audit.log_report_rejected(
                    actor_uid=reviewer_uid,
                    actor_email=reviewer_email,
                    actor_rol=reviewer_rol,
                    report_request_id=request_id,
                    reason=reason,
                )

            except Exception:
                pass

        # ------------------------------------------------------
        # NOTIFICACIÓN
        # ------------------------------------------------------

        if (
            self.ses
            and req.get(
                "requester_email"
            )
        ):

            try:

                self.ses.send_report_notification(
                    to_email=(
                        req[
                            "requester_email"
                        ]
                    ),
                    request_id=request_id,
                    session_id=req.get(
                        "session_id",
                        "",
                    ),
                    approved=False,
                    comments=reason,
                )

            except Exception:
                pass

        return (
            self.get_request(
                request_id
            )
            or req
        )

    # ==========================================================
    # GENERAR + SUBIR PDF
    # ==========================================================

    def _generate_and_store_pdf(
        self,
        request_id: str,
        reviewer_ctx: Optional[
            Mapping[str, str]
        ] = None,
    ) -> dict:
        """
        Genera el PDF y lo almacena en S3 privado.

        Estados permitidos:

            APPROVED -> COMPLETED
            COMPLETED -> COMPLETED

        En una regeneración administrativa se reinicia
        downloaded_at para permitir una nueva descarga
        de la nueva versión del PDF.
        """

        req = self._validate(
            self.get_request(
                request_id
            )
        )

        current_status = req.get(
            "status"
        )

        if current_status not in {
            STATUS_APPROVED,
            STATUS_COMPLETED,
        }:
            raise ValueError(
                "No se puede generar el informe desde el estado "
                f"{current_status}"
            )

        if not self.report_service:
            raise RuntimeError(
                "ReportService no disponible"
            )

        if not self.s3:
            raise RuntimeError(
                "S3Service no disponible"
            )

        session_id = req.get(
            "session_id"
        )

        if not session_id:
            raise ValueError(
                "La solicitud no tiene session_id"
            )

        # ------------------------------------------------------
        # GENERAR PDF
        # ------------------------------------------------------

        pdf_bytes, s3_key = (
            self.report_service.generate_and_upload_pdf(
                session_id,
                request_id=request_id,
            )
        )

        if not pdf_bytes:
            raise RuntimeError(
                "El PDF generado está vacío"
            )

        if not s3_key:
            raise RuntimeError(
                "No se obtuvo la clave S3 del informe"
            )

        filename = (
            s3_key.rsplit(
                "/",
                1,
            )[-1]
        )

        now = DynamoDBService.now_iso()

        updates = {
            "status": STATUS_COMPLETED,
            "report_s3_key": s3_key,
            "report_filename": filename,
            "report_bytes": len(pdf_bytes),
            "generation_error": None,
            "updated_at": now,
        }

        # Una nueva generación representa una nueva versión
        # disponible para descarga. El atributo downloaded_at
        # debe eliminarse físicamente, no establecerse en NULL.
        self.dynamo.update_item(
            self.table,
            key={
                "requester_uid": (
                    req["requester_uid"]
                ),
                "sk": req["sk"],
            },
            update_data=updates,
            remove_fields=["downloaded_at"],
        )

        req.update(
            updates
        )

        return req

    # ==========================================================
    # REGENERAR
    # ==========================================================

    def regenerate_report(
        self,
        request_id: str,
        *,
        reviewer_uid: str,
        reviewer_email: str,
        reviewer_rol: str,
    ) -> dict:
        """
        Regenera un informe existente.

        Permitido:

            APPROVED -> COMPLETED
            COMPLETED -> COMPLETED

        Una regeneración crea una nueva versión descargable
        y reinicia downloaded_at.
        """

        self._require_admin(
            reviewer_rol
        )

        req = self._validate(
            self.get_request(
                request_id
            )
        )

        current_status = req.get(
            "status"
        )

        if current_status not in {
            STATUS_APPROVED,
            STATUS_COMPLETED,
        }:
            raise ValueError(
                "Solo se puede regenerar un informe "
                "APPROVED o COMPLETED. "
                f"Estado actual: {current_status}"
            )

        try:

            result = (
                self._generate_and_store_pdf(
                    request_id,
                    reviewer_ctx={
                        "actor_uid": reviewer_uid,
                        "actor_email": reviewer_email,
                        "actor_rol": reviewer_rol,
                    },
                )
            )

        except Exception as exc:

            updates = {
                "status": STATUS_ERROR,
                "generation_error": str(
                    exc
                ),
                "updated_at": (
                    DynamoDBService.now_iso()
                ),
            }

            self.dynamo.update_item(
                self.table,
                key={
                    "requester_uid": (
                        req["requester_uid"]
                    ),
                    "sk": req["sk"],
                },
                update_data=updates,
            )

            raise ValueError(
                f"Error regenerando informe: {exc}"
            )

        return result

    # ==========================================================
    # DESCARGA
    # ==========================================================

    def get_download_url(
        self,
        request_id: str,
        *,
        actor_uid: str,
        actor_email: str,
        actor_rol: str,
        expires_in: int = 300,
    ) -> str:
        """
        Genera una URL presignada temporal.

        Una solicitud COMPLETED puede utilizarse una sola vez.

        Después de descargar:

            downloaded_at != None

        Una segunda descarga queda bloqueada.

        Para obtener nuevamente el reporte, el especialista
        debe crear una nueva solicitud.
        """

        req = self._validate(
            self.get_request(
                request_id
            )
        )

        # ======================================================
        # 1. ESTADO
        # ======================================================

        if (
            req.get("status")
            != STATUS_COMPLETED
        ):
            raise ValueError(
                "La solicitud no tiene informe listo "
                "para descargar "
                f"(estado actual: {req.get('status')})"
            )

        # ======================================================
        # 2. YA DESCARGADO
        # ======================================================

        if req.get(
            "downloaded_at"
        ):
            raise ValueError(
                "Esta solicitud ya fue utilizada para "
                "descargar el informe. Para obtener "
                "nuevamente el reporte debe realizar "
                "una nueva solicitud y esperar una nueva "
                "aprobación."
            )

        # ======================================================
        # 3. PDF
        # ======================================================

        if not req.get(
            "report_s3_key"
        ):
            raise ValueError(
                "La solicitud está COMPLETED pero no tiene "
                "report_s3_key"
            )

        # ======================================================
        # 4. PERMISOS
        # ======================================================

        is_admin = (
            actor_rol == ROLE_ADMIN
        )

        is_owner = (
            req.get(
                "requester_uid"
            )
            == actor_uid
        )

        if not is_admin and not is_owner:
            raise PermissionError(
                "No tiene permiso para descargar este informe"
            )

        if self.s3 is None:
            raise RuntimeError(
                "S3Service no disponible"
            )

        # ======================================================
        # 5. URL TEMPORAL
        # ======================================================

        expires_in = max(
            60,
            min(
                int(expires_in),
                900,
            ),
        )

        url = (
            self.s3.generate_presigned_download_url(
                bucket=self.s3.report_bucket,
                key=req[
                    "report_s3_key"
                ],
                expires_in=expires_in,
                download_filename=req.get(
                    "report_filename"
                ),
            )
        )

        # ======================================================
        # 6. CONSUMIR LA DESCARGA
        # ======================================================

        now = DynamoDBService.now_iso()

        marked = self.dynamo.update_item(
            self.table,
            key={
                "requester_uid": (
                    req["requester_uid"]
                ),
                "sk": req["sk"],
            },
            update_data={
                "downloaded_at": now,
                "updated_at": now,
            },
            # Compatibilidad con solicitudes antiguas que fueron
            # creadas cuando downloaded_at se guardaba como NULL.
            # Las solicitudes nuevas no tendrán el atributo hasta
            # que realmente se consuma la descarga.
            condition_expression=(
                "attribute_not_exists(downloaded_at) "
                "OR attribute_type(downloaded_at, :null_type)"
            ),
            expression_attribute_values={
                ":null_type": "NULL",
            },
        )

        # ------------------------------------------------------
        # Si otra petición consumió la solicitud antes,
        # DynamoDB devuelve False por la condición.
        # ------------------------------------------------------

        if not marked:

            raise ValueError(
                "Esta solicitud ya fue utilizada para "
                "descargar el informe. Para obtener "
                "nuevamente el reporte debe realizar "
                "una nueva solicitud y esperar una nueva "
                "aprobación."
            )

        # ======================================================
        # 7. AUDITORÍA
        # ======================================================

        if self.audit:

            try:

                self.audit.log_report_downloaded(
                    actor_uid=actor_uid,
                    actor_email=actor_email,
                    actor_rol=actor_rol,
                    report_request_id=request_id,
                )

            except Exception:
                pass

        return url