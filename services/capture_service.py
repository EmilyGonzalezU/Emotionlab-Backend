"""Ciclo de vida de una experiencia de captura de EmotionLab.

Modelo funcional vigente (identificador principal = `session_id`):

    INICIAR -> CAPTURAR -> FINALIZAR -> EXTRAER JSON DESDE AWS ->
    CENTRALIZAR -> PROCESAR -> CERRAR SESIÓN

Identificadores:

    session_id        UUID que genera EmotionLab al presionar «Iniciar
                      experiencia». Es la identidad de la experiencia y el
                      contenedor de TODOS los datos extraídos.
    display_code      PIN de 6 dígitos generado EXCLUSIVAMENTE por la Lambda
                      del lente. Puede repetirse y NO es identidad
                      permanente ni criterio de búsqueda temporal.
    lens_session_id   identidad ORIGINAL de la captura del lente.
    unity_session_id  identificador propio de cada registro de Unity; se
                      conserva tal cual. Una experiencia centraliza varios.

Mecanismo de recolección:

- Durante la captura NO se descargan datos: los JSON se acumulan en AWS.
- Al presionar «Finalizar experiencia» se registra `capture_ended_at` (hora
  exacta del botón) y la experiencia pasa a `finalizing`.
- La extracción se ejecuta al confirmar la transferencia (`data_ready_at`)
  y busca los JSON cuyo timestamp cae en el intervalo de la experiencia.
- La fuente principal es el mecanismo que ya usa `AWSService`
  (`AWS_UNITY_DATA_URL`); opcionalmente puede listarse un prefijo S3
  configurado por entorno (paginación completa con `ContinuationToken`).
- Cada archivo se identifica de forma estable por su `s3_key`, de modo que
  reejecutar la extracción no duplica registros.

Reglas que este servicio garantiza:

- El `session_id` NUNCA se reemplaza por identificadores de Unity o del lente.
- No se generan `capture_id`, `fragment_id` ni `processed_session_id`.
- No se crea `participant_id` de alumno ni `tracking_id` durante la captura.
- Una experiencia `captured` rechaza registros nuevos.
- La orden física de detención del lente NO existe en este repositorio; se
  documenta como integración externa pendiente (`device_stop`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from core.constants import (
    AUDIT_CAPTURE_PIN_REJECTED,
    AUDIT_CAPTURE_COMPLETED,
    AUDIT_CAPTURE_FINALIZING,
    AUDIT_CAPTURE_IMPORT_FAILED,
    AUDIT_CAPTURE_IMPORT_STARTED,
    AUDIT_CAPTURE_IMPORTED,
    AUDIT_CAPTURE_RECORD_LINKED,
    AUDIT_CAPTURE_RECORD_REJECTED,
    AUDIT_CAPTURE_STARTED,
)
from services.emotion_classification import enriquecer_analisis
from services.emotion_model_v2_service import (
    ESTADO_ANALIZADO,
    MODEL_VERSION_ESPERADA,
    EmotionModelV2Service,
)
from services.import_service import ImportService
from services.parsers.emotion_v2_parser import EmotionV2Parser
from services.lens_session_service import (
    LensApiConfirmationAmbiguous,
    LensSessionExpired,
    LensSessionNotFound,
    SessionCodeRateLimitUnavailable,
)
from services.unity_data_service import (
    STATUS_AMBIGUOUS,
    STATUS_MATCHED,
)
from services.session_service import (
    CAPTURE_CONFIRMATION_IN_PROGRESS,
    CAPTURE_CONFIRMATION_SUCCEEDED,
    CAPTURE_CONFIRMATION_UNKNOWN,
    STATUS_CAPTURED,
    STATUS_CAPTURE_IMPORT_ERROR,
    STATUS_CAPTURING,
    STATUS_FINALIZING,
    STATUS_IMPORTING,
    SessionService,
)


# ------------------------------------------------------------------
# Errores del ciclo de captura
# ------------------------------------------------------------------


class CaptureNotFound(Exception):
    """La experiencia indicada no existe."""


class CaptureNotAuthorized(Exception):
    """La experiencia pertenece a otro especialista."""


class SessionClosed(Exception):
    """La experiencia ya fue finalizada y no admite capturas nuevas."""


class SessionClosing(Exception):
    """La experiencia está cerrando: solo admite datos pendientes conocidos."""


class InvalidCapturePayload(Exception):
    """El lote de registros no tiene el formato esperado."""


class DeviceBusy(Exception):
    """Ya existe una experiencia activa en el sistema (cerrojo global)."""


class InvalidDisplayCode(Exception):
    """El PIN indicado no corresponde a una captura vigente del lente."""


class CaptureConfirmationPending(Exception):
    """La confirmación del PIN con el lente quedó en estado indeterminado.

    Ocurre ante un timeout ambiguo tras enviar la confirmación: la sesión
    del lente pudo haberse confirmado aunque no se recibió respuesta. La
    operación se marca como `confirmation_unknown` y el cerrojo global
    permanece retenido hasta resolverla. NO se reintenta la llamada
    automáticamente, NO se vuelve a confirmar el PIN con el mismo
    `request_token` y NO se inicia otra experiencia.
    """


# Formato local del PIN generado por la Lambda del lente: 6 dígitos.
_DISPLAY_CODE_FORMAT = re.compile(r"\d{6}")


_UNITY_SESSION_ID_KEYS = (
    "unity_session_id",
    "unitySessionId",
    "session_id",
    "sessionId",
)

_DISPLAY_CODE_KEYS = (
    "display_code",
    "displayCode",
)

_LENS_SESSION_ID_KEYS = (
    "lens_session_id",
    "lensSessionId",
)

_SOURCE_KEY_KEYS = (
    "source_key",
    "s3_key",
)

# Identificador del participante del lente (`idParticipante`) que emite la
# Lambda al confirmar la sesión. NO es la identidad de la experiencia; se usa
# solo para correlacionar el registro de Unity de ESTA captura.
_LENS_PARTICIPANT_KEYS = (
    "lens_participant_id",
    "idParticipante",
    "participant_id",
    "participantId",
)

# Marca temporal de CAPTURA dentro del registro (prevalece sobre la de
# subida para decidir si el dato pertenece al intervalo de la experiencia).
_CAPTURE_TIME_KEYS = (
    "captured_at",
    "capture_timestamp",
    "timestamp_captura",
    "fecha_captura",
    "session_started_at",
    "fechaInicio",
)

# Marca temporal de SUBIDA / modificación del objeto. Se usa únicamente
# cuando no hay una marca de captura más fiable.
_UPLOAD_TIME_KEYS = (
    "timestamp_subida",
    "uploaded_at",
    "uploadedAt",
    "last_modified",
    "LastModified",
)

# Estados terminales desde los que una experiencia ya no "ocupa" el
# dispositivo.
_SLOT_RELEASING_STATUSES = (STATUS_CAPTURED,)


class CaptureService:
    """Orquesta el ciclo de captura sobre `SessionService`."""

    # Estados desde los que se admite una nueva captura por lote (legacy).
    _OPEN_STATUSES = (STATUS_CAPTURING, STATUS_FINALIZING)

    def __init__(
        self,
        session_service: Optional[SessionService] = None,
        lens_session_service=None,
        audit_svc=None,
        aws_service=None,
        s3_service=None,
        unity_bucket: Optional[str] = None,
        unity_prefix: Optional[str] = None,
        unity_data_service=None,
        emotion_v2_service=None,
    ):
        self.session = session_service or SessionService()
        self.lens = lens_session_service
        self.audit = audit_svc
        self.aws = aws_service
        self.s3 = s3_service
        # Fuente PRINCIPAL de los registros de Unity: `GET /data` del API
        # Gateway de los lentes. Se separa de `aws` (componente legacy que
        # lee `AWS_UNITY_DATA_URL`) para no romper el mecanismo anterior.
        self.unity_data = unity_data_service
        # Modelo congelado V2 (K-Means): ÚNICO motor de IA vigente para las
        # nuevas experiencias.
        self.emotion_v2 = emotion_v2_service
        self.unity_bucket = (
            unity_bucket
            if unity_bucket is not None
            else os.getenv("S3_UNITY_DATA_BUCKET", "")
        ).strip()
        self.unity_prefix = (
            unity_prefix
            if unity_prefix is not None
            else os.getenv("S3_UNITY_DATA_PREFIX", "")
        ).strip()

    # ==========================================================
    # HELPERS
    # ==========================================================

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _first(record: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @classmethod
    def _extract_nested(
        cls,
        record: Mapping[str, Any],
        keys: Sequence[str],
    ) -> Optional[str]:
        """Busca la clave en el registro y en sus contenedores conocidos."""

        found = cls._first(record, keys)
        if found:
            return found

        for nested_key in ("session_data", "record"):
            nested = record.get(nested_key)
            if isinstance(nested, Mapping):
                found = cls._first(nested, keys)
                if found:
                    return found

        return None

    @classmethod
    def _extract_display_code(cls, record: Mapping[str, Any]) -> Optional[str]:
        return cls._extract_nested(record, _DISPLAY_CODE_KEYS)

    @classmethod
    def _extract_unity_session_id(
        cls, record: Mapping[str, Any]
    ) -> Optional[str]:
        return cls._extract_nested(record, _UNITY_SESSION_ID_KEYS)

    @classmethod
    def _extract_lens_session_hint(
        cls, record: Mapping[str, Any]
    ) -> Optional[str]:
        return cls._extract_nested(record, _LENS_SESSION_ID_KEYS)

    @classmethod
    def _extract_source_key(cls, record: Mapping[str, Any]) -> Optional[str]:
        return cls._first(record, _SOURCE_KEY_KEYS)

    @classmethod
    def _extract_lens_participant_id(
        cls, record: Mapping[str, Any]
    ) -> Optional[str]:
        return cls._extract_nested(record, _LENS_PARTICIPANT_KEYS)

    @staticmethod
    def _record_identity(record: Mapping[str, Any]) -> str:
        """Clave estable para evitar duplicar un registro reimportado."""

        source_key = CaptureService._extract_source_key(record)
        if source_key:
            return f"source:{source_key}"

        unity = CaptureService._extract_unity_session_id(record)
        if unity:
            return f"unity:{unity}"

        canonical = json.dumps(
            record,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"hash:{digest}"

    # ------------------------------------------------------------------
    # FECHAS
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_utc(value: Any) -> Optional[datetime]:
        """Convierte un timestamp heterogéneo a `datetime` UTC con zona.

        Acepta `datetime`, epoch (segundos/milisegundos) y texto ISO 8601
        (con `Z` u offset). Un texto sin zona se interpreta como UTC, que es
        el formato en que EmotionLab registra sus marcas (`...Z`).
        """

        if value is None:
            return None

        if isinstance(value, datetime):
            moment = value
            if moment.tzinfo is None:
                return moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc)

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = float(value)
            if seconds > 1e12:  # milisegundos
                seconds /= 1000.0
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None

        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None

            candidate = text[:-1] + "+00:00" if text.endswith("Z") else text

            moment: Optional[datetime] = None

            try:
                moment = datetime.fromisoformat(candidate)
            except ValueError:
                for fmt in (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d %H:%M",
                    "%Y-%m-%d",
                ):
                    try:
                        moment = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue

            if moment is None:
                return None

            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)

            return moment.astimezone(timezone.utc)

        return None

    @classmethod
    def _record_time(
        cls,
        record: Mapping[str, Any],
    ) -> tuple[Optional[datetime], bool]:
        """Devuelve (momento UTC, es_marca_de_captura).

        Prioriza una marca de captura fiable del propio registro; si no
        existe, cae a la marca de subida/modificación del objeto.
        """

        sources: list[Mapping[str, Any]] = [record]
        session_data = record.get("session_data")
        if isinstance(session_data, Mapping):
            sources.append(session_data)

        for source in sources:
            for key in _CAPTURE_TIME_KEYS:
                if key in source:
                    moment = cls._coerce_utc(source.get(key))
                    if moment is not None:
                        return moment, True

        for source in sources:
            for key in _UPLOAD_TIME_KEYS:
                if key in source:
                    moment = cls._coerce_utc(source.get(key))
                    if moment is not None:
                        return moment, False

        return None, False

    def _audit(self, action: str, actor_ctx: Optional[Mapping[str, Any]], extra=None):
        if self.audit is None:
            return

        actor_ctx = actor_ctx or {}

        try:
            self.audit.log(
                action,
                actor_uid=actor_ctx.get("actor_uid"),
                actor_email=actor_ctx.get("actor_email"),
                actor_rol=actor_ctx.get("actor_rol"),
                extra=extra,
            )
        except Exception:
            print(
                "Error registrando auditoría de captura.",
                flush=True,
            )

    # ==========================================================
    # AUTORIZACIÓN
    # ==========================================================

    def _load_owned(
        self,
        session_id: str,
        actor_ctx: Optional[Mapping[str, Any]],
    ) -> dict:
        """Carga la experiencia y comprueba que pertenece al especialista.

        Evita que experiencias de especialistas distintos se mezclen: solo
        el propietario (o un administrador) opera sobre la experiencia.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise CaptureNotFound("La experiencia no existe.")

        session = self.session.get_session(session_id.strip())

        if not session:
            raise CaptureNotFound("La experiencia no existe.")

        actor_ctx = actor_ctx or {}
        actor_uid = actor_ctx.get("actor_uid")
        actor_rol = (actor_ctx.get("actor_rol") or "").strip().lower()

        owner_id = session.get("owner_id")

        if actor_rol != "admin" and (
            not actor_uid
            or not owner_id
            or str(actor_uid) != str(owner_id)
        ):
            raise CaptureNotAuthorized(
                "La experiencia no tiene un propietario verificable para este especialista."
            )

        return session

    @staticmethod
    def _status_of(session: Mapping[str, Any]) -> str:
        return (
            session.get("status")
            or session.get("estado")
            or ""
        )

    # ==========================================================
    # ETAPA 1 — INICIAR EXPERIENCIA
    # ==========================================================

    def start_experience(
        self,
        *,
        actor_ctx: Optional[Mapping[str, Any]] = None,
        organization_id: Optional[str] = None,
        device_id: Optional[str] = None,
        request_token: Optional[str] = None,
        display_code: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> dict:
        """Genera el `session_id` y crea la experiencia en estado capturing.

        Integración con el API Gateway de los lentes (cuenta AWS externa):

            POST {LENS_API_BASE_URL}/session/confirm

        La Lambda DESPLEGADA no implementa `validateOnly`: esa misma ruta
        realiza directamente la transición `pending -> confirmed`. Por eso el
        inicio envía UNA ÚNICA petición de confirmación y NUNCA una validación
        previa (una validación consumiría el PIN por efecto secundario).

        Orden del flujo:

            1. Comprobar idempotencia y fallos previos del request_token.
            2. Consultar el rate limit local por IP.
            3. Adquirir el cerrojo atómico global.
            4. Enviar UNA SOLA petición de confirmación con `displayCode`,
               `specialistId` y `patientId`.
            5. Persistir la respuesta confirmada y crear la experiencia de
               EmotionLab vinculando el `lens_session_id` original.

        Si se recibe `request_token`, el `session_id` y el
        `confirmation_request_id` se derivan de forma determinista (UUID v5)
        para que un `request_token` repetido recupere la MISMA experiencia sin
        volver a confirmar el PIN contra el lente.

        Ante un timeout AMBIGUO al confirmar (la petición se envió pero no
        hubo respuesta) se marca la operación como `confirmation_unknown` en
        el cerrojo y se lanza `CaptureConfirmationPending`: NO se libera el
        cerrojo (la confirmación pudo haberse aplicado), NO se reintenta la
        llamada automáticamente y NO se inicia otra experiencia. El cerrojo
        permanece retenido hasta resolver la operación manualmente.

        La creación reserva un cerrojo atómico GLOBAL: por diseño del MVP no
        pueden coexistir dos experiencias activas en todo el sistema. El
        cerrojo solo se libera al alcanzar el estado terminal `captured`.
        """

        actor_ctx = actor_ctx or {}
        owner_id = actor_ctx.get("actor_uid") or ""

        if not owner_id:
            raise CaptureNotAuthorized(
                "Se requiere un especialista autenticado."
            )

        # Identificadores idempotentes derivados del token de la solicitud.
        if isinstance(request_token, str) and request_token.strip():
            token = request_token.strip()
            session_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"emotionlab-capture:{owner_id}:{token}",
                )
            )
            confirmation_request_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"emotionlab-confirm:{owner_id}:{token}",
                )
            )
        else:
            token = None
            session_id = str(uuid.uuid4())
            confirmation_request_id = str(uuid.uuid4())

        # Idempotencia: un `request_token` repetido devuelve la experiencia
        # existente SIN volver a confirmar el PIN contra el lente.
        existing = self.session.get_session(session_id)

        if existing:
            confirmed = self.session.get_capture_confirmation(session_id)
            if confirmed and confirmed.get("session_id"):
                lens_ids = existing.get("lens_session_ids") or []
                if confirmed["session_id"] not in lens_ids:
                    self.session.bind_capture_lens_session(
                        session_id, confirmed["session_id"]
                    )
                    existing = self.session.get_session(session_id) or existing
            return self._experience_view(existing, created=False)

        # Validación LOCAL del formato del PIN (6 dígitos). No contacta al
        # lente: el código se revalida una única vez al confirmarlo.
        code = self._normalize_display_code(display_code)

        # Protección: si un intento previo con este token quedó en estado
        # indeterminado, NO se vuelve a confirmar el PIN ni se inicia otra
        # experiencia; el cerrojo permanece retenido hasta resolverlo.
        if self._confirmation_is_unknown(session_id):
            raise CaptureConfirmationPending(
                "El intento de inicio con este token quedó en estado "
                "indeterminado tras la confirmación. No se reintenta "
                "automáticamente; resuelve la operación antes de volver a "
                "iniciar una experiencia."
            )

        if code and client_ip and self.lens is not None:
            prior_failure = self.lens.check_capture_attempt(
                client_ip=client_ip,
                actor_uid=owner_id,
                request_token=token,
            )
            if prior_failure == "expired":
                raise LensSessionExpired(
                    "El código del lente expiró."
                )
            if prior_failure == "not_found":
                raise InvalidDisplayCode(
                    "El código no corresponde a una sesión vigente del lente."
                )
        elif code and client_ip:
            raise SessionCodeRateLimitUnavailable(
                "El limitador de intentos no está disponible."
            )

        # 1. Adquirir el cerrojo global.
        if not self._reserve_slot(session_id, owner_id, token):
            raise DeviceBusy(
                "Ya existe una experiencia activa en el sistema. "
                "Finalícela antes de iniciar otra."
            )

        lens_session_id: Optional[str] = None
        # `idParticipante` que emite la Lambda al confirmar. Se guarda como
        # `lens_participant_id` para correlacionar el registro de Unity de
        # ESTA captura. No es la identidad del alumno ni reemplaza al
        # `session_id`.
        lens_participant_id: Optional[str] = None

        # 2. Registrar el intento (el cerrojo queda asociado al token) y
        # 3. enviar UNA ÚNICA petición de confirmación con displayCode,
        #    specialistId y patientId.
        if code:
            resolved = self.session.get_capture_confirmation(session_id)
            if not resolved:
                try:
                    resolved = self._confirm_lens_session(
                        display_code=code,
                        specialist_id=owner_id,
                        confirmation_request_id=confirmation_request_id,
                    )
                except LensApiConfirmationAmbiguous:
                    self.session.mark_capture_confirmation_unknown(session_id)
                    raise CaptureConfirmationPending(
                        "No se pudo confirmar el PIN con el lente en el tiempo "
                        "esperado. La operación quedó en estado indeterminado; no "
                        "se reintenta automáticamente."
                    )
                except (LensSessionNotFound, LensSessionExpired) as error:
                    failure_code = "expired" if isinstance(error, LensSessionExpired) else "not_found"
                    try:
                        if client_ip and self.lens is not None:
                            self.lens.register_capture_failure(
                                client_ip=client_ip,
                                actor_uid=owner_id,
                                request_token=token,
                                failure_code=failure_code,
                            )
                    finally:
                        self.session.release_capture_slot(session_id)
                    self._audit(
                        AUDIT_CAPTURE_PIN_REJECTED,
                        actor_ctx,
                        {"reason": failure_code},
                    )
                    if isinstance(error, LensSessionExpired):
                        raise
                    raise InvalidDisplayCode(
                        "El código no corresponde a una sesión vigente del lente."
                    ) from error
                except Exception:
                    self.session.release_capture_slot(session_id)
                    raise

                lens_session_id = (
                    str(resolved["session_id"])
                    if resolved and resolved.get("session_id")
                    else None
                )
                lens_participant_id = (
                    str(resolved["participant_id"])
                    if resolved and resolved.get("participant_id")
                    else None
                )
                if not lens_session_id:
                    self.session.release_capture_slot(session_id)
                    raise InvalidDisplayCode(
                        "El código no corresponde a una sesión vigente del lente."
                    )
                try:
                    persisted = self.session.mark_capture_confirmation_succeeded(
                        session_id,
                        lens_session_id,
                        lens_participant_id,
                    )
                    if not persisted:
                        raise RuntimeError(
                            "No se pudo persistir la confirmación del lente."
                        )
                except Exception as error:
                    try:
                        self.session.mark_capture_confirmation_unknown(session_id)
                    except Exception:
                        # Si DynamoDB tampoco permite marcar `unknown`, se
                        # conserva el slot `in_progress` y nunca se libera.
                        pass
                    raise CaptureConfirmationPending(
                        "El lente confirmó el PIN, pero no se pudo guardar el "
                        "resultado. El bloqueo se conserva para evitar otra confirmación."
                    ) from error
                if client_ip and self.lens is not None:
                    self.lens.reset_capture_attempts(client_ip)
            else:
                lens_session_id = resolved.get("session_id")
                lens_participant_id = resolved.get("participant_id")

            if lens_session_id:
                lens_session_id = str(lens_session_id)
            if lens_participant_id:
                lens_participant_id = str(lens_participant_id)

            if not lens_session_id:
                self.session.release_capture_slot(session_id)
                raise InvalidDisplayCode(
                    "El código no corresponde a una sesión vigente del lente."
                )

        # 5. Generar el UUID de EmotionLab y crear la experiencia.
        try:
            result = self.session.create_capture_session(
                session_id,
                owner_id=owner_id,
                organization_id=organization_id,
                device_id=device_id,
                capture_token=token,
                lens_participant_id=lens_participant_id,
            )
        except Exception:
            # La confirmación externa ya pudo consumirse. Se conserva el
            # resultado confirmado en el slot para reanudar con el mismo token.
            if not (code and self.session.get_capture_confirmation(session_id)):
                self.session.release_capture_slot(session_id)
            raise

        session = result["session"]

        if result["created"] and lens_session_id:
            # El PIN no se persiste: la vinculación permanente usa la
            # identidad ORIGINAL del lente (`lens_session_id`).
            self.session.bind_capture_lens_session(
                session_id,
                lens_session_id,
            )
            session = self.session.get_session(session_id) or session

        if result["created"]:
            self._audit(
                AUDIT_CAPTURE_STARTED,
                actor_ctx,
                {
                    "session_id": session_id,
                    "organization_id": organization_id,
                    "lens_session_id": lens_session_id,
                },
            )

        return self._experience_view(session, created=result["created"])

    @staticmethod
    def _normalize_display_code(
        display_code: Optional[str],
    ) -> Optional[str]:
        """Valida LOCALMENTE el formato del PIN sin contactar al lente.

        Devuelve el código normalizado o `None` si no se recibió PIN. Lanza
        `InvalidDisplayCode` si el valor no es un PIN de 6 dígitos.
        """

        if display_code is None:
            return None

        if not isinstance(display_code, str) or not display_code.strip():
            raise InvalidDisplayCode(
                "El código del lente es obligatorio."
            )

        code = display_code.strip()

        if not _DISPLAY_CODE_FORMAT.fullmatch(code):
            raise InvalidDisplayCode(
                "El código debe contener 6 dígitos."
            )

        return code

    def _confirmation_is_unknown(self, session_id: str) -> bool:
        """Indica si el intento con este `session_id` quedó indeterminado."""

        slot = self.session.get_capture_slot() or {}

        return (
            slot.get("active_session_id") == session_id
            and slot.get("confirmation_state")
            == CAPTURE_CONFIRMATION_UNKNOWN
        )

    def _confirm_lens_session(
        self,
        *,
        display_code: str,
        specialist_id: str,
        confirmation_request_id: Optional[str],
    ) -> dict:
        """Envía la ÚNICA petición de confirmación al API Gateway del lente."""

        if self.lens is None:
            raise RuntimeError(
                "La confirmación del PIN requiere la fuente autorizada "
                "del lente."
            )

        return self.lens.confirm_display_code(
            display_code,
            specialist_id=specialist_id,
            confirmation_request_id=confirmation_request_id,
        )

    def _reserve_slot(
        self,
        session_id: str,
        owner_id: str,
        capture_token: Optional[str] = None,
    ) -> bool:
        """Reserva el cerrojo global, limpiando uno obsoleto si aplica.

        MVP: el cerrojo es único para todo el sistema; solo se libera cuando
        la experiencia alcanza el estado terminal `captured`. El `capture_token`
        se guarda como metadato que asocia el cerrojo al intento de inicio.
        """

        if self.session.acquire_capture_slot(
            session_id, owner_id, capture_token
        ):
            return True

        slot = self.session.get_capture_slot() or {}

        # Reintento del mismo inicio: el cerrojo ya nos pertenece.
        if slot.get("active_session_id") == session_id:
            return slot.get("confirmation_state") != CAPTURE_CONFIRMATION_IN_PROGRESS

        if self._slot_is_stale(slot):
            # Se libera comprobando el valor leído: si otro proceso ya
            # reemplazó el cerrojo, este borrado no lo elimina.
            self.session.release_capture_slot(slot.get("active_session_id"))
            return bool(
                self.session.acquire_capture_slot(
                    session_id, owner_id, capture_token
                )
            )

        return False

    def _slot_is_stale(self, slot: Mapping[str, Any]) -> bool:
        """No libera intentos remotos en curso o de resultado incierto."""

        if slot.get("confirmation_state") in {
            CAPTURE_CONFIRMATION_IN_PROGRESS,
            CAPTURE_CONFIRMATION_UNKNOWN,
            CAPTURE_CONFIRMATION_SUCCEEDED,
        }:
            return False

        active_id = slot.get("active_session_id")

        if not active_id:
            return True

        session = self.session.get_session(active_id)

        if not session:
            return True

        return self._status_of(session) in _SLOT_RELEASING_STATUSES

    # ==========================================================
    # LEGACY — RECEPCIÓN DIRECTA DE REGISTROS
    # ==========================================================
    #
    # Este camino se conserva solo por compatibilidad. El mecanismo
    # principal de recolección es la extracción desde AWS al finalizar.

    @staticmethod
    def _normalize_records(payload: Any) -> list[dict]:
        """Acepta `{"records": [...]}` o un único registro."""

        if isinstance(payload, Mapping):
            records = payload.get("records")
            if isinstance(records, list):
                candidates = records
            else:
                candidates = [payload]
        elif isinstance(payload, list):
            candidates = payload
        else:
            raise InvalidCapturePayload(
                "El lote debe ser un objeto JSON con una lista "
                "'records' o un registro único."
            )

        normalized: list[dict] = []
        for record in candidates:
            if not isinstance(record, Mapping):
                raise InvalidCapturePayload(
                    "Cada registro del lote debe ser un objeto JSON."
                )
            normalized.append(dict(record))

        if not normalized:
            raise InvalidCapturePayload(
                "El lote no contiene registros."
            )

        return normalized

    def ingest_records(
        self,
        session_id: str,
        payload: Any,
        *,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Centraliza registros de Unity bajo el `session_id` de EmotionLab.

        Compatibilidad: el mecanismo principal es la extracción desde AWS.
        Cada registro debe traer el `display_code` que generó el lente; el
        PIN se valida contra la fuente autorizada y se usa para conservar la
        identidad original del lente, nunca como identidad permanente.
        """

        session = self._load_owned(session_id, actor_ctx)

        status = self._status_of(session)

        if status == STATUS_CAPTURED:
            raise SessionClosed(
                "La experiencia ya fue finalizada y no admite "
                "nuevas capturas."
            )

        if status not in self._OPEN_STATUSES:
            raise SessionClosed(
                "La experiencia no está en captura."
            )

        records = self._normalize_records(payload)

        raw = session.get("raw_json") or {}
        stored_records = list(raw.get("records") or [])
        known_identities = {
            item.get("identity")
            for item in stored_records
            if isinstance(item, Mapping)
        }

        unity_ids = list(session.get("unity_session_ids") or [])

        bound_lens_ids = set(session.get("lens_session_ids") or [])
        if session.get("lens_session_id"):
            bound_lens_ids.add(session.get("lens_session_id"))

        accepted: list[dict] = []
        rejected: list[dict] = []
        primary_lens_id = session.get("lens_session_id")
        primary_display_code = None

        for record in records:

            identity = self._record_identity(record)

            if identity in known_identities:
                # Reimportación del mismo registro: no duplica.
                continue

            unity_id = self._extract_unity_session_id(record)
            display_code = self._extract_display_code(record)
            hint = self._extract_lens_session_hint(record)

            if not display_code:
                rejected.append(
                    {
                        "unity_session_id": unity_id,
                        "reason": "SIN_CODIGO",
                        "message": (
                            "El registro no incluye el display_code del "
                            "lente; no se puede validar su procedencia."
                        ),
                    }
                )
                continue

            resolved_lens_id = self._resolve_lens_id(display_code)

            if resolved_lens_id is None:
                rejected.append(
                    {
                        "unity_session_id": unity_id,
                        "display_code": display_code,
                        "reason": "CODIGO_INVALIDO",
                        "message": (
                            "El código no corresponde a una captura "
                            "vigente del lente."
                        ),
                    }
                )
                continue

            # El PIN no es identidad permanente: dos experiencias
            # independientes que compartan el mismo valor no se fusionan
            # porque la vinculación real usa el `lens_session_id`.
            other = self.session.get_capture_by_lens_session(resolved_lens_id)

            if other and other.get("session_id") != session_id:
                rejected.append(
                    {
                        "unity_session_id": unity_id,
                        "display_code": display_code,
                        "lens_session_id": resolved_lens_id,
                        "reason": "EXPERIENCIA_DISTINTA",
                        "message": (
                            "La captura del lente ya está vinculada a otra "
                            "experiencia de EmotionLab."
                        ),
                    }
                )
                continue

            if status == STATUS_FINALIZING and resolved_lens_id not in bound_lens_ids:
                rejected.append(
                    {
                        "unity_session_id": unity_id,
                        "display_code": display_code,
                        "lens_session_id": resolved_lens_id,
                        "reason": "EXPERIENCIA_CERRANDO",
                        "message": (
                            "La experiencia está cerrando; solo admite "
                            "datos pendientes de una captura ya conocida."
                        ),
                    }
                )
                continue

            stored = {
                "identity": identity,
                "origin": "ingest",
                "unity_session_id": unity_id,
                "display_code": display_code,
                "lens_session_id": resolved_lens_id,
                "lens_session_id_declared": hint,
                "origin_validated": True,
                "source_key": self._extract_source_key(record),
                "received_at": self._now(),
                # Identificadores originales y datos íntegros de Unity.
                "record": record,
            }

            stored_records.append(stored)
            known_identities.add(identity)

            bound_lens_ids.add(resolved_lens_id)

            if unity_id and unity_id not in unity_ids:
                unity_ids.append(unity_id)

            primary_lens_id = primary_lens_id or resolved_lens_id
            primary_display_code = primary_display_code or display_code

            accepted.append(
                {
                    "unity_session_id": unity_id,
                    "display_code": display_code,
                    "lens_session_id": resolved_lens_id,
                }
            )

        if accepted:

            self.session.merge_session_data_set(
                session_id,
                {"records": stored_records},
                fields={
                    "unity_session_ids": unity_ids,
                    "record_count": len(stored_records),
                },
            )

            self.session.bind_capture_lens_session(
                session_id,
                primary_lens_id,
                primary_display_code,
            )

            self._audit(
                AUDIT_CAPTURE_RECORD_LINKED,
                actor_ctx,
                {
                    "session_id": session_id,
                    "accepted": len(accepted),
                },
            )

        for item in rejected:
            self._audit(
                AUDIT_CAPTURE_RECORD_REJECTED,
                actor_ctx,
                {
                    "session_id": session_id,
                    "reason": item.get("reason"),
                },
            )

        refreshed = self.session.get_session(session_id) or session

        return {
            "session_id": session_id,
            "status": self._status_of(refreshed),
            "accepted": accepted,
            "rejected": rejected,
            "record_count": len(stored_records),
            "unity_session_ids": unity_ids,
        }

    def _resolve_lens_id(self, display_code: str) -> Optional[str]:
        """Valida el PIN contra la fuente autorizada del lente."""

        if self.lens is None:
            raise RuntimeError(
                "La validación del PIN requiere la fuente autorizada "
                "del lente."
            )

        try:
            resolved = self.lens.lookup_by_display_code(display_code)
        except LensSessionNotFound:
            return None

        return str(resolved.get("session_id")) if resolved else None

    # ==========================================================
    # ETAPA 3 — FINALIZAR EXPERIENCIA
    # ==========================================================

    def finalize_experience(
        self,
        session_id: str,
        *,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Registra la hora de término y pasa la experiencia a `finalizing`.

        `capture_ended_at` conserva el instante en que el especialista
        presionó «Finalizar experiencia» (no el momento en que termina la
        descarga) y NUNCA se modifica durante los reintentos.

        No ejecuta la orden física de detención del dispositivo: ese canal
        no existe en este repositorio (ver `device_stop`).
        """

        session = self._load_owned(session_id, actor_ctx)

        status = self._status_of(session)

        if status in (STATUS_CAPTURED, STATUS_IMPORTING, STATUS_CAPTURE_IMPORT_ERROR):
            return self._finalize_view(session)

        if status == STATUS_CAPTURING:
            changed = self.session.transition_capture_status(
                session_id,
                STATUS_FINALIZING,
                allowed_from=STATUS_CAPTURING,
            )
            if changed:
                self._audit(
                    AUDIT_CAPTURE_FINALIZING,
                    actor_ctx,
                    {"session_id": session_id},
                )
            session = self.session.get_session(session_id) or session

        return self._finalize_view(session)

    # ==========================================================
    # ETAPAS 4-7 — EXTRAER, CENTRALIZAR, PROCESAR Y CERRAR
    # ==========================================================

    def complete_experience(
        self,
        session_id: str,
        *,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Extrae los JSON de AWS, los centraliza y cierra la experiencia.

        Es la operación que el ejecutable ejecuta tras confirmar que los
        archivos pendientes terminaron de subirse. Idempotente: repetirla
        no duplica archivos ni altera `capture_started_at`/`capture_ended_at`.
        """

        session = self._load_owned(session_id, actor_ctx)

        status = self._status_of(session)

        if status == STATUS_CAPTURED:
            return self._finalize_view(session)

        if status == STATUS_CAPTURING:
            # Cierre directo: el ejecutable confirmó de inmediato.
            self.session.transition_capture_status(
                session_id,
                STATUS_FINALIZING,
                allowed_from=STATUS_CAPTURING,
            )
            status = STATUS_FINALIZING

        if status in (STATUS_FINALIZING, STATUS_CAPTURE_IMPORT_ERROR):
            changed = self.session.transition_capture_status(
                session_id,
                STATUS_IMPORTING,
                allowed_from=status,
            )
            if not changed:
                # Otra ejecución concurrente ya está importando.
                session = self.session.get_session(session_id) or session
                return self._finalize_view(session)

            self._audit(
                AUDIT_CAPTURE_IMPORT_STARTED,
                actor_ctx,
                {"session_id": session_id},
            )

            session = self.session.get_session(session_id) or session

            return self._run_extraction(session_id, actor_ctx)

        # Ya está `importing`: se devuelve el estado sin relanzar.
        return self._finalize_view(session)

    def _run_extraction(
        self,
        session_id: str,
        actor_ctx: Optional[Mapping[str, Any]],
    ) -> dict:
        """Ejecuta la extracción y deja la experiencia en un estado estable."""

        session = self.session.get_session(session_id) or {}

        try:
            outcome = self._extract_and_centralize(session_id, session, actor_ctx)
        except Exception as error:
            print(
                "Error inesperado durante la extracción de la experiencia "
                f"(tipo={type(error).__name__}).",
                flush=True,
            )
            outcome = {
                "files_found": 0,
                "files_imported": 0,
                "files_pending": 0,
                "errors": [
                    {
                        "stage": "extraction",
                        "reason": type(error).__name__,
                        "message": str(error)[:300],
                    }
                ],
            }

        success = (
            not outcome["errors"]
            and outcome["files_found"] > 0
            and outcome["files_imported"] > 0
        )

        no_data = (
            outcome["files_found"] == 0
            and outcome["files_imported"] == 0
            and all(
                item.get("reason") == "SIN_CANDIDATO"
                for item in outcome["errors"]
            )
        )

        fields = {
            "import_files_found": outcome["files_found"],
            "import_files_imported": outcome["files_imported"],
            "import_files_pending": outcome["files_pending"],
            "import_errors": outcome["errors"],
        }

        if success:
            # La extracción persiste raw_json y sus índices. El objeto
            # `session` cargado al iniciar _run_extraction es anterior a esa
            # escritura; recargarlo evita analizar y presentar un snapshot
            # vacío aunque DynamoDB ya tenga los registros.
            session = self.session.get_session(session_id) or session
            fields.update(self._process_experience(session_id, session))
            fields["import_status"] = "imported"
            changed = self.session.transition_capture_status(
                session_id,
                STATUS_CAPTURED,
                allowed_from=STATUS_IMPORTING,
            )
            self.session.update_session_fields(session_id, fields)
            if changed:
                self.session.release_capture_slot(session_id)
                self._audit(
                    AUDIT_CAPTURE_IMPORTED,
                    actor_ctx,
                    {
                        "session_id": session_id,
                        "files_imported": outcome["files_imported"],
                    },
                )
                self._audit(
                    AUDIT_CAPTURE_COMPLETED,
                    actor_ctx,
                    {"session_id": session_id},
                )
        else:
            fields["import_status"] = "no_data" if no_data else "error"
            self.session.transition_capture_status(
                session_id,
                STATUS_CAPTURE_IMPORT_ERROR,
                allowed_from=STATUS_IMPORTING,
            )
            self.session.update_session_fields(session_id, fields)
            self._audit(
                AUDIT_CAPTURE_IMPORT_FAILED,
                actor_ctx,
                {
                    "session_id": session_id,
                    "files_found": outcome["files_found"],
                    "errors": len(outcome["errors"]),
                },
            )

        refreshed = self.session.get_session(session_id) or session

        return self._finalize_view(refreshed)

    # ------------------------------------------------------------------
    # DESCUBRIMIENTO DE ARCHIVOS
    # ------------------------------------------------------------------

    def _discover_candidates(
        self,
    ) -> tuple[list[dict], list[dict]]:
        """Reúne los registros candidatos y los errores de descubrimiento.

        Fuente PRINCIPAL: `GET /data` del API Gateway de los lentes
        (`UnityDataService`). Fuente legacy: el mecanismo que ya utiliza
        `AWSService` (`AWS_UNITY_DATA_URL`). Fuente opcional: listado
        paginado de un prefijo S3 configurado por entorno.

        Los registros de `/data` se marcan con `_source = "lens-data"` para
        aplicarles la correlación de candidato único y excluirlos de los
        filtros de prefijo/sufijo pensados para el mecanismo legacy.
        """

        candidates: list[dict] = []
        errors: list[dict] = []

        if self.unity_data is not None:

            try:
                records = self.unity_data.fetch_records()
            except Exception as error:
                records = []
                errors.append(
                    {
                        "stage": "fetch_unity_data",
                        "reason": type(error).__name__,
                        "message": str(error)[:300],
                    }
                )

            for item in records:
                if isinstance(item, Mapping):
                    candidate = dict(item)
                    candidate["_source"] = "lens-data"
                    candidates.append(candidate)

        if self.aws is not None:

            try:
                payload = self.aws.fetch_sessions()
            except Exception as error:
                payload = None
                errors.append(
                    {
                        "stage": "fetch_sessions",
                        "reason": type(error).__name__,
                        "message": str(error)[:300],
                    }
                )

            if isinstance(payload, Mapping):
                sesiones = payload.get("sesiones")
                if isinstance(sesiones, list):
                    for item in sesiones:
                        if isinstance(item, Mapping):
                            candidates.append(dict(item))

        if self.s3 is not None and self.unity_bucket:

            try:
                objects = self.s3.list_objects_v2(
                    self.unity_bucket,
                    self.unity_prefix,
                    suffix=".json",
                )
            except Exception as error:
                objects = []
                errors.append(
                    {
                        "stage": "list_objects_v2",
                        "reason": type(error).__name__,
                        "message": str(error)[:300],
                    }
                )

            known = {
                str(item.get("s3_key"))
                for item in candidates
                if item.get("s3_key")
            }

            for obj in objects:
                key = obj.get("key")
                if not key or key in known:
                    continue
                candidates.append(
                    {
                        "s3_key": key,
                        "last_modified": obj.get("last_modified"),
                        "size": obj.get("size"),
                        "etag": obj.get("etag"),
                        "_from_listing": True,
                    }
                )

        if self.unity_prefix:
            candidates = [
                item
                for item in candidates
                if item.get("_source") == "lens-data"
                or not item.get("s3_key")
                or str(item["s3_key"]).startswith(self.unity_prefix)
            ]

        # Solo archivos JSON: se ignora cualquier otro objeto. Los registros
        # de `/data` ya vienen validados por el servicio de ingestión.
        candidates = [
            item
            for item in candidates
            if item.get("_source") == "lens-data"
            or not item.get("s3_key")
            or str(item["s3_key"]).lower().endswith(".json")
        ]

        return candidates, errors

    def _window(
        self,
        session: Mapping[str, Any],
    ) -> tuple[Optional[datetime], Optional[datetime], Optional[datetime]]:
        """Devuelve (inicio, fin de captura, disponibilidad de datos)."""

        start = self._coerce_utc(session.get("capture_started_at"))
        end = self._coerce_utc(session.get("capture_ended_at"))
        if end is None:
            end = self._coerce_utc(session.get("finalize_requested_at"))
        ready = self._coerce_utc(session.get("data_ready_at")) or end

        return start, end, ready

    def _in_window(
        self,
        record: Mapping[str, Any],
        start: Optional[datetime],
        end: Optional[datetime],
        ready: Optional[datetime],
    ) -> bool:
        """Decide si un registro cae dentro del intervalo de la experiencia.

        Un timestamp de captura fiable debe cumplir
        `started_at <= ts <= ended_at`. Cuando solo hay marca de
        subida/modificación se usa `data_ready_at` como límite superior, de
        modo que los JSON subidos tras finalizar pero capturados antes no se
        pierdan. Sin marca temporal no se excluye (el origen ya delimita).
        """

        moment, is_capture = self._record_time(record)

        if moment is None:
            return True

        if start is not None and moment < start:
            return False

        upper = end if is_capture else (ready or end)

        if upper is not None and moment > upper:
            return False

        return True

    def _match_unity_record(
        self,
        session: Mapping[str, Any],
        records: list[dict],
        errors: list[dict],
    ) -> Optional[dict]:
        """Selecciona el ÚNICO registro de `/data` que corresponde a la captura.

        Devuelve el candidato correlacionado o `None`. Ante ambigüedad añade
        el error `SESSION_MATCH_AMBIGUOUS` y NO importa nada; sin candidato
        añade `SIN_CANDIDATO` y la captura queda pendiente.
        """

        if not records or self.unity_data is None:
            return None

        result = self.unity_data.select_candidate(
            records,
            lens_participant_id=session.get("lens_participant_id"),
            capture_started_at=session.get("capture_started_at"),
            capture_ended_at=session.get("capture_ended_at"),
            data_ready_at=session.get("data_ready_at"),
        )

        status = result.get("status")

        if status == STATUS_MATCHED:
            return result.get("candidate")

        if status == STATUS_AMBIGUOUS:
            errors.append(
                {
                    "stage": "match",
                    "reason": "SESSION_MATCH_AMBIGUOUS",
                    "message": (
                        "Más de un registro de Unity coincide con la "
                        "captura: no se importa ninguno."
                    ),
                    "matched_by": result.get("matched_by"),
                    "matches": len(result.get("matches") or []),
                }
            )
            return None

        errors.append(
            {
                "stage": "match",
                "reason": "SIN_CANDIDATO",
                "message": (
                    "Ningún registro de Unity coincide todavía con la "
                    "captura."
                ),
                "matched_by": result.get("matched_by"),
            }
        )
        return None

    # ------------------------------------------------------------------
    # CENTRALIZACIÓN
    # ------------------------------------------------------------------

    def _extract_and_centralize(
        self,
        session_id: str,
        session: Mapping[str, Any],
        actor_ctx: Optional[Mapping[str, Any]],
    ) -> dict:
        """Extrae los JSON del intervalo y los centraliza bajo `session_id`."""

        # `data_ready_at`: la transferencia de los archivos pendientes se
        # confirma al iniciar la extracción; queda fijada una sola vez.
        data_ready_at = session.get("data_ready_at") or self._now()
        self.session.update_session_fields(
            session_id,
            {"data_ready_at": data_ready_at},
        )
        session = dict(session)
        session["data_ready_at"] = data_ready_at

        candidates, errors = self._discover_candidates()

        start, end, ready = self._window(session)

        # Los registros del API Gateway de los lentes (`/data`) exigen un
        # candidato ÚNICO correlacionado con ESTA captura; se separan del
        # mecanismo legacy (filtro temporal sobre todos los candidatos).
        data_records = [
            record
            for record in candidates
            if record.get("_source") == "lens-data"
        ]
        legacy_records = [
            record
            for record in candidates
            if record.get("_source") != "lens-data"
        ]

        selected = [
            record
            for record in legacy_records
            if self._in_window(record, start, end, ready)
        ]

        matched = self._match_unity_record(session, data_records, errors)
        if matched is not None:
            selected.append(matched)

        raw = session.get("raw_json") or {}
        stored_records = list(raw.get("records") or [])
        known_identities = {
            item.get("identity")
            for item in stored_records
            if isinstance(item, Mapping)
        }

        unity_ids = list(session.get("unity_session_ids") or [])
        source_keys = list(session.get("source_keys") or [])

        imported: list[dict] = []
        pending: list[dict] = []

        for record in selected:

            identity = self._record_identity(record)

            if identity in known_identities:
                # Ya importado (mismo `s3_key`): no se duplica.
                continue

            content = self._materialize_record(record)

            if content is None:
                pending.append(
                    {
                        "s3_key": self._extract_source_key(record),
                        "reason": "DESCARGA_FALLIDA",
                    }
                )
                continue

            unity_id = self._extract_unity_session_id(content)
            source_key = self._extract_source_key(content)

            stored_records.append(
                {
                    "identity": identity,
                    "origin": record.get("_source") or "aws-extraction",
                    "unity_session_id": unity_id,
                    "display_code": self._extract_display_code(content),
                    "lens_session_id": self._extract_lens_session_hint(content),
                    "origin_validated": False,
                    "source_key": source_key,
                    "s3_key": source_key,
                    "last_modified": content.get("last_modified"),
                    "size": content.get("size"),
                    "etag": content.get("etag"),
                    "captured_at": self._captured_at_for(record, content),
                    "received_at": self._now(),
                    "record": content.get("record", content),
                }
            )
            known_identities.add(identity)
            imported.append({"identity": identity, "s3_key": source_key})

            if unity_id and unity_id not in unity_ids:
                unity_ids.append(unity_id)

            if source_key and source_key not in source_keys:
                source_keys.append(source_key)

        if imported:

            fields: dict[str, Any] = {
                "unity_session_ids": unity_ids,
                "source_keys": source_keys,
                "record_count": len(stored_records),
            }

            if matched is not None:
                fields.update(
                    self._unity_metadata(session_id, matched)
                )

            self.session.merge_session_data_set(
                session_id,
                {"records": stored_records},
                fields=fields,
            )

        for item in pending:
            errors.append(
                {
                    "stage": "download",
                    "s3_key": item.get("s3_key"),
                    "reason": item.get("reason"),
                }
            )

        return {
            "files_found": len(selected),
            "files_imported": len(imported),
            "files_pending": len(pending),
            "errors": errors,
        }

    def _captured_at_for(
        self,
        record: Mapping[str, Any],
        content: Mapping[str, Any],
    ) -> Optional[str]:
        """Marca de captura almacenada para el registro centralizado.

        Para los registros de `/data` se usa el inicio de la sesión Unity ya
        normalizado a UTC (`_unity_started_at`); para el resto se aplica la
        heurística heredada.
        """

        if record.get("_unity_started_at"):
            return record.get("_unity_started_at")

        moment, _is_capture = self._record_time(content)
        return moment.isoformat() if moment else None

    @staticmethod
    def _source_basename(source_key: Any) -> Optional[str]:
        if not isinstance(source_key, str) or not source_key.strip():
            return None

        name = source_key.strip().rsplit("/", 1)[-1]

        if name.lower().endswith(".json"):
            name = name[:-5]

        return name or None

    def _store_unity_raw_json(
        self,
        session_id: str,
        record: Mapping[str, Any],
        source_key: Optional[str],
    ) -> Optional[str]:
        """Guarda el JSON ORIGINAL del registro bajo una clave determinística.

        Clave: `raw/unity/<session_id>/<participante|archivo>.json`. Es
        idempotente: reimportar sobrescribe el mismo objeto y no genera
        archivos innecesarios. Devuelve la clave o `None` si no hay S3.
        """

        if self.s3 is None:
            return None

        payload = dict(record)
        for internal in ("_source", "_unity_started_at", "_unity_ended_at"):
            payload.pop(internal, None)

        name = (
            self._extract_lens_participant_id(record)
            or self._source_basename(source_key)
            or self._extract_unity_session_id(record)
            or "registro"
        )

        key = f"raw/unity/{session_id}/{name}.json"

        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
            self.s3.put_session_raw_json(key, body)
            return key
        except Exception:
            # Si S3 falla, el registro queda igualmente centralizado inline.
            return None

    def _unity_metadata(
        self,
        session_id: str,
        record: Mapping[str, Any],
    ) -> dict:
        """Metadatos de importación del registro de Unity correlacionado."""

        source_key = self._extract_source_key(record)

        return {
            "source": "lens-data",
            "source_s3_key": source_key,
            "raw_s3_key": self._store_unity_raw_json(
                session_id, record, source_key
            ),
            "unity_started_at": record.get("_unity_started_at"),
            "unity_ended_at": record.get("_unity_ended_at"),
            "lens_participant_id": self._extract_lens_participant_id(record),
            "imported_at": self._now(),
            "import_status": "imported",
        }

    def load_unity_json(
        self,
        session_id: str,
        *,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> Optional[dict]:
        """Recupera el JSON ORIGINAL de Unity a partir del `session_id`.

        Usa la referencia S3 (`raw_s3_key`) y, si no está disponible, cae al
        registro centralizado en la propia sesión. Devuelve `None` si la
        experiencia no tiene datos importados.
        """

        session = self._load_owned(session_id, actor_ctx)

        raw_key = session.get("raw_s3_key")

        if raw_key and self.s3 is not None:
            try:
                data = self.s3.get_session_raw_json(raw_key)
                if isinstance(data, (bytes, bytearray)):
                    return json.loads(data.decode("utf-8"))
                if isinstance(data, Mapping):
                    return dict(data)
            except Exception:
                pass

        records = list((session.get("raw_json") or {}).get("records") or [])
        if records:
            return records[0].get("record")

        return None

    def _materialize_record(
        self,
        record: Mapping[str, Any],
    ) -> Optional[dict]:
        """Devuelve el registro con su contenido, descargándolo si falta."""

        record = dict(record)

        session_data = record.get("session_data")

        if isinstance(session_data, Mapping):
            # El candidato ya trae su contenido inline (por ejemplo, un
            # registro devuelto por `fetch_sessions`); no hay nada que
            # descargar ni que envolver.
            return record

        s3_key = self._extract_source_key(record)

        if not s3_key:
            return None

        if self.s3 is None or not self.unity_bucket:
            # No hay bucket configurado para descargar el objeto.
            return None

        try:
            raw = self.s3.get_object(self.unity_bucket, s3_key)
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            return None

        if not isinstance(parsed, Mapping):
            # El objeto no es un objeto JSON: se conserva tal cual.
            record["record"] = parsed
            record["session_data"] = parsed
            return record

        # El contenido real es el JSON descargado. Se conserva su estructura
        # (identificadores originales incluidos) y se añaden los metadatos
        # del objeto sin sobrescribir lo que ya viniera dentro.
        content = dict(parsed)
        content.setdefault("s3_key", s3_key)

        for metadata_key in ("last_modified", "size", "etag"):
            if record.get(metadata_key) is not None:
                content.setdefault(metadata_key, record.get(metadata_key))

        return content

    # ------------------------------------------------------------------
    # PROCESAMIENTO
    # ------------------------------------------------------------------

    def _process_experience(
        self,
        session_id: str,
        session: Mapping[str, Any],
    ) -> dict:
        """Calcula el análisis IA V2 de la experiencia completa.

        No altera el `status` de la experiencia: solo enriquece la sesión
        con el análisis del único modelo vigente (K-Means V2 congelado) y
        los campos derivados del dashboard. Un fallo del modelo NUNCA
        invalida la captura: el bloque queda en `processing_error`.
        """

        refreshed = self.session.get_session(session_id) or session
        raw = refreshed.get("raw_json") or {}
        stored_records = list(raw.get("records") or [])

        merged: dict[str, Any] = {}

        for item in stored_records:
            if not isinstance(item, Mapping):
                continue
            inner = item.get("record")
            if isinstance(inner, Mapping):
                merged = ImportService._merge_session_records(merged, inner)

        fields: dict[str, Any] = {}

        if self.emotion_v2 is None:
            fields["processing_status"] = "modelo_v2_no_configurado"
            return fields

        fields.update(self._analyze_v2(session_id, merged, refreshed))
        fields["processing_status"] = fields.get("ai_status")

        return fields

    def _analyze_v2(
        self,
        session_id: str,
        merged: Mapping[str, Any],
        session: Mapping[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Ejecuta el análisis V2 sobre el registro fusionado de la sesión.

        Es idempotente: si la sesión ya fue analizada con la MISMA versión de
        modelo y las MISMAS respuestas medidas, se reutiliza el resultado.
        Con `force=True` se ignora la reutilización y se recalcula.
        """

        v2_parser = EmotionV2Parser(merged)
        student_data = v2_parser.get_student_data()
        valores = v2_parser.get_model_values()
        extraccion = v2_parser.get_extraction_report()

        existente = session.get("model_processing") or {}

        if (
            not force
            and isinstance(existente, Mapping)
            and existente.get("model_version") == MODEL_VERSION_ESPERADA
            and existente.get("status") == ESTADO_ANALIZADO
            and existente.get("measured_values") == valores
        ):
            return {
                "model_processing": existente,
                "ai_status": ESTADO_ANALIZADO,
                "ai_reused": True,
                "student_data": student_data,
            }

        try:
            analisis = self.emotion_v2.analyze(session_id, valores)
        except Exception as error:
            analisis = EmotionModelV2Service.error_block(error)
            analisis["session_id"] = session_id
            analisis["measured_values"] = valores

        analisis["extraction"] = extraccion

        # Enmascaramiento binario + bienestar promedio (post-predicción).
        # Conserva `prediction.group` (original) y agrega la clasificación
        # final binaria y el promedio de los tres indicadores.
        enriquecer_analisis(analisis)

        return {
            "model_processing": analisis,
            "ai_status": analisis.get("status"),
            "ai_reused": False,
            "student_data": student_data,
        }

    def analyze_experience(
        self,
        session_id: str,
        *,
        actor_ctx: Optional[Mapping[str, Any]] = None,
        force: bool = False,
    ) -> dict:
        """Recalcula el análisis V2 de una experiencia existente.

        Reutiliza el mismo `session_id`: NUNCA crea una sesión nueva ni
        duplica el JSON original. Con `force=False` respeta el resultado
        existente cuando la versión y las entradas coinciden.
        """

        if self.emotion_v2 is None:
            raise RuntimeError("El modelo V2 no está configurado.")

        session = self._load_owned(session_id, actor_ctx)
        raw = session.get("raw_json") or {}
        stored_records = list(raw.get("records") or [])

        if not stored_records:
            raise InvalidCapturePayload(
                "La experiencia no tiene registros de Unity importados."
            )

        merged: dict[str, Any] = {}

        for item in stored_records:
            if not isinstance(item, Mapping):
                continue
            inner = item.get("record")
            if isinstance(inner, Mapping):
                merged = ImportService._merge_session_records(merged, inner)

        previous = session.get("model_processing") or {}

        fields = self._analyze_v2(session_id, merged, session, force=force)

        block = fields.get("model_processing")

        if force and isinstance(block, dict):
            if isinstance(previous, Mapping) and previous:
                # El reprocesamiento NUNCA descarta el resultado anterior: se
                # conserva su trazabilidad dentro del mismo bloque de evaluación.
                historial = list(previous.get("previous_processing") or [])

                snapshot = {
                    clave: valor
                    for clave, valor in previous.items()
                    if clave != "previous_processing"
                }
                snapshot["replaced_at"] = self._now()
                historial.append(snapshot)

                block["previous_processing"] = historial

            block["reprocessed_at"] = self._now()

        self.session.update_session_fields(session_id, fields)

        refreshed = self.session.get_session(session_id) or session

        return self._experience_view(refreshed, created=False)

    # ------------------------------------------------------------------
    # VISTAS
    # ------------------------------------------------------------------

    @staticmethod
    def _extraction_view(session: Mapping[str, Any]) -> dict:
        return {
            "files_found": session.get("import_files_found", 0),
            "files_imported": session.get("import_files_imported", 0),
            "files_pending": session.get("import_files_pending", 0),
            "errors": list(session.get("import_errors") or []),
            "processing_status": session.get("processing_status"),
            "import_status": session.get("import_status"),
        }

    def _finalize_view(self, session: Mapping[str, Any]) -> dict:
        return {
            "session_id": session.get("session_id"),
            "status": self._status_of(session),
            "capture_started_at": session.get("capture_started_at"),
            "capture_ended_at": session.get("capture_ended_at"),
            "data_ready_at": session.get("data_ready_at"),
            "finalize_requested_at": session.get("finalize_requested_at"),
            "capture_finished_at": session.get("capture_finished_at"),
            "record_count": session.get("record_count", 0),
            "unity_session_ids": list(
                session.get("unity_session_ids") or []
            ),
            "lens_session_ids": list(
                session.get("lens_session_ids") or []
            ),
            "extraction": self._extraction_view(session),
            "device_stop": {
                "requested": True,
                "confirmed": self._status_of(session)
                in (STATUS_CAPTURED, STATUS_CAPTURE_IMPORT_ERROR),
                "integration_pending": True,
                "note": (
                    "La orden física de detención del lente se gestiona "
                    "fuera de este repositorio: no existe canal hacia el "
                    "dispositivo. El ejecutable debe detener la captura y "
                    "confirmar la transferencia antes de la extracción."
                ),
            },
        }

    # ==========================================================
    # CONSULTAS
    # ==========================================================

    def get_experience(
        self,
        session_id: str,
        *,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        session = self._load_owned(session_id, actor_ctx)
        return self._experience_view(session, created=False)

    def list_original_records(
        self,
        session_id: str,
        *,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> list[dict]:
        """Devuelve los registros originales centralizados.

        Se conservan los identificadores originales (`unity_session_id`),
        la referencia de origen (`s3_key`), los metadatos del objeto y los
        datos íntegros de cada archivo.
        """

        session = self._load_owned(session_id, actor_ctx)

        raw = session.get("raw_json") or {}

        return list(raw.get("records") or [])

    def _experience_view(
        self,
        session: Mapping[str, Any],
        *,
        created: bool,
    ) -> dict:
        return {
            "session_id": session.get("session_id"),
            "status": self._status_of(session),
            "created": created,
            "owner_id": session.get("owner_id"),
            "organization_id": session.get("organization_id"),
            "capture_started_at": session.get("capture_started_at"),
            "capture_ended_at": session.get("capture_ended_at"),
            "data_ready_at": session.get("data_ready_at"),
            "finalize_requested_at": session.get("finalize_requested_at"),
            "capture_finished_at": session.get("capture_finished_at"),
            "lens_session_ids": list(
                session.get("lens_session_ids") or []
            ),
            "display_codes": list(session.get("display_codes") or []),
            "unity_session_ids": list(
                session.get("unity_session_ids") or []
            ),
            "record_count": session.get("record_count", 0),
            "extraction": self._extraction_view(session),
            "created_at": session.get("created_at"),
            # Bloque aditivo del modelo V2 (K-Means): datos del alumno,
            # trazabilidad de imputación, indicadores exploratorios y
            # resultado del agrupamiento. Se recupera junto al resto de la
            # experiencia mediante el mismo `session_id`.
            "student_data": session.get("student_data"),
            "model_processing": session.get("model_processing"),
            "ai_status": session.get("ai_status"),
        }
