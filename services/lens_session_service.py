"""Identificación del ciclo de captura del lente a partir del displayCode.

Integración con el **API Gateway de los lentes** (cuenta AWS externa) por
HTTPS. EmotionLab ya **no** consulta las tablas DynamoDB del lente: toda la
comunicación se realiza contra:

    POST {LENS_API_BASE_URL}/session/confirm

que invoca la Lambda `emotionlabtest-confirmSession`. Esa Lambda admite dos
modalidades:

    validateOnly = true   -> valida el PIN sin modificar la sesión.
    validateOnly = false  -> confirma la sesión (pending -> confirmed).

Este servicio expone:

    validate_display_code(display_code)         validación sin consumir.
    confirm_display_code(display_code, ...)     confirmación real (una sola vez).

Identificadores (no deben confundirse):

    displayCode   conexión inicial temporal; no es identidad de datos.
    sessionId     identidad permanente de la experiencia y de todos sus
                  datos; es lo que resuelve este servicio (`lens_session_id`).
    tracking_id   agrupación de sesiones elegida por el especialista.

El `sessionToken` del lente NUNCA se guarda ni se devuelve.

El limitador de intentos fallidos es LOCAL a EmotionLab y usa su propia tabla
DynamoDB (`session-code-rate-limits`); lo comparten la ruta de resolución
compatible y el inicio de captura.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import requests
from botocore.exceptions import ClientError

from core.constants import (
    AUDIT_SESSION_CODE_RESOLVE_FAILED,
    AUDIT_SESSION_CODE_RESOLVED,
    DYNAMODB_SESSION_CODE_RATE_LIMITS,
)


DEFAULT_LENS_API_TIMEOUT = 15
DEFAULT_LENS_API_PATH = "/session/confirm"


# ==========================================================
# ERRORES
# ==========================================================


class LensSessionNotFound(Exception):
    """No existe una sesión válida asociada al código indicado (404)."""


class LensSessionExpired(Exception):
    """La sesión del lente asociada al código expiró (410)."""


class LensSessionConflict(Exception):
    """La sesión del lente ya fue confirmada o entra en conflicto (409)."""


class LensApiRateLimited(Exception):
    """El servicio del lente rechazó la petición por exceso de intentos."""


class LensApiUnavailable(Exception):
    """El servicio del lente no respondió o devolvió un error 5xx."""


class LensApiConfirmationAmbiguous(LensApiUnavailable):
    """Timeout ambiguo tras enviar la confirmación.

    La petición pudo haberse aplicado en el lente aunque no se recibió
    respuesta. NO se debe reintentar ni reutilizar el PIN como si no se
    hubiera usado: el resultado es indeterminado y debe recuperarse el
    estado manualmente.

    Hereda de `LensApiUnavailable` para que el manejo genérico de
    indisponibilidad siga funcionando.
    """


class LensApiError(Exception):
    """El servicio del lente devolvió una respuesta inválida o inesperada."""


class SessionCodeRateLimitExceeded(Exception):
    """Se superó el límite local de validaciones fallidas del código."""


class SessionCodeRateLimitUnavailable(Exception):
    """El almacenamiento o secreto del limitador no está disponible."""


class LensSessionService:
    """Resuelve `displayCode -> sessionId` mediante el API Gateway del lente.

    No accede directamente a DynamoDB de la cuenta de los lentes: usa
    exclusivamente HTTPS contra `LENS_API_BASE_URL`. Su única dependencia de
    DynamoDB es la tabla de límites propia de EmotionLab.
    """

    DISPLAY_CODE_PATTERN = re.compile(r"\d{6}")

    MAX_FAILED_ATTEMPTS = 5
    RATE_LIMIT_WINDOW_SECONDS = 300
    RATE_LIMIT_TTL_SECONDS = 86400

    def __init__(
        self,
        dynamodb_service,
        audit_svc=None,
        ssm_service=None,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
        http_client=None,
    ):
        if dynamodb_service is None:
            raise RuntimeError(
                "LensSessionService requiere DynamoDB para el limitador."
            )

        self.dynamo = dynamodb_service
        self.audit = audit_svc
        self.ssm = ssm_service

        self.rate_limits_table = (
            DYNAMODB_SESSION_CODE_RATE_LIMITS
        )

        self._explicit_base_url = base_url
        self._explicit_timeout = timeout
        self.http = http_client or requests.Session()

        self._hmac_secret: Optional[str] = None

    # ==========================================================
    # CONFIGURACIÓN
    # ==========================================================

    def _base_url(self) -> str:
        url = (
            self._explicit_base_url
            or os.getenv("LENS_API_BASE_URL")
            or ""
        ).strip().rstrip("/")

        if not url:
            raise RuntimeError(
                "LENS_API_BASE_URL no está configurado."
            )

        return url

    def _timeout(self) -> int:
        if self._explicit_timeout is not None:
            return int(self._explicit_timeout)

        raw = os.getenv("LENS_API_TIMEOUT")

        try:
            return int(raw) if raw else DEFAULT_LENS_API_TIMEOUT
        except ValueError:
            return DEFAULT_LENS_API_TIMEOUT

    # ==========================================================
    # HELPERS
    # ==========================================================

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _get_hmac_secret(self) -> str:
        if self._hmac_secret:
            return self._hmac_secret

        if self.ssm is None:
            raise RuntimeError(
                "El servicio de secretos no está disponible."
            )

        secret = self.ssm.get_session_code_hmac_secret()

        if not isinstance(secret, str) or len(secret) < 32:
            raise RuntimeError(
                "La clave HMAC del código de sesión no es válida."
            )

        self._hmac_secret = secret
        return secret

    @staticmethod
    def _normalize_ip(client_ip: Any) -> str:
        """Valida y normaliza el origen reportado por API Gateway."""

        if not isinstance(client_ip, str) or not client_ip.strip():
            raise ValueError("Origen de conexión no válido.")

        try:
            return ipaddress.ip_address(
                client_ip.strip()
            ).compressed
        except ValueError as error:
            raise ValueError(
                "Origen de conexión no válido."
            ) from error

    def _hash_ip(self, client_ip: str) -> str:
        """La IP nunca se almacena en texto plano."""

        normalized = self._normalize_ip(client_ip)
        secret = self._get_hmac_secret().encode("utf-8")

        return hmac.new(
            secret,
            f"ip:{normalized}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _window_start(self) -> int:
        now_epoch = int(self._now().timestamp())
        return (
            now_epoch // self.RATE_LIMIT_WINDOW_SECONDS
        ) * self.RATE_LIMIT_WINDOW_SECONDS

    def _rate_key(self, client_ip: str) -> str:
        ip_hash = self._hash_ip(client_ip)
        return f"{ip_hash}#{self._window_start()}"

    # ==========================================================
    # LIMITADOR DE INTENTOS FALLIDOS (LOCAL A EMOTIONLAB)
    # ==========================================================

    def _is_blocked(self, client_ip: str) -> bool:
        """Comprueba si el origen agotó los intentos de la ventana actual."""

        rate_key = self._rate_key(client_ip)
        try:
            record = self.dynamo.get_item(
                self.rate_limits_table,
                {"rate_key": rate_key},
                consistent_read=True,
            )
        except Exception as error:
            raise SessionCodeRateLimitUnavailable(
                "No se pudo consultar el limitador de intentos."
            ) from error

        if not record:
            return False

        return (
            int(record.get("attempts", 0))
            >= self.MAX_FAILED_ATTEMPTS
        )

    def _register_failed_attempt(self, client_ip: str) -> None:
        """Incrementa el contador de fallos de forma atómica."""

        now_epoch = int(self._now().timestamp())

        operations = [
            {
                "Update": {
                    "TableName": self.rate_limits_table,
                    "Key": {
                        "rate_key": self._rate_key(client_ip),
                    },
                    "UpdateExpression": (
                        "SET attempts = "
                        "if_not_exists(attempts, :zero) + :one, "
                        "expires_at = :expires_at"
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(attempts) "
                        "OR attempts < :max_attempts"
                    ),
                    "ExpressionAttributeValues": {
                        ":zero": 0,
                        ":one": 1,
                        ":max_attempts": self.MAX_FAILED_ATTEMPTS,
                        ":expires_at": (
                            now_epoch + self.RATE_LIMIT_TTL_SECONDS
                        ),
                    },
                }
            }
        ]

        try:
            self.dynamo.transact_write_items(operations)

        except ClientError as error:
            error_code = (
                error.response.get("Error", {}).get("Code")
            )

            if error_code in {
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            }:
                reasons = error.response.get("CancellationReasons") or []
                conditional_failure = error_code == "ConditionalCheckFailedException" or any(
                    reason.get("Code") == "ConditionalCheckFailed"
                    for reason in reasons
                )
                if not conditional_failure:
                    raise SessionCodeRateLimitUnavailable(
                        "No se pudo actualizar el limitador de intentos."
                    ) from error
                raise SessionCodeRateLimitExceeded(
                    "Se superó el límite de intentos de validación "
                    "del código."
                ) from error

            raise SessionCodeRateLimitUnavailable(
                "No se pudo actualizar el limitador de intentos."
            ) from error
        except SessionCodeRateLimitExceeded:
            raise
        except Exception as error:
            raise SessionCodeRateLimitUnavailable(
                "No se pudo actualizar el limitador de intentos."
            ) from error

    def _capture_request_key(
        self,
        actor_uid: str,
        request_token: Optional[str],
    ) -> Optional[str]:
        """Clave seudónima que permite deduplicar fallos del mismo inicio."""

        if not isinstance(request_token, str) or not request_token.strip():
            return None

        secret = self._get_hmac_secret().encode("utf-8")
        value = f"capture:{actor_uid}:{request_token.strip()}".encode("utf-8")
        digest = hmac.new(secret, value, hashlib.sha256).hexdigest()
        return f"capture-request#{digest}"

    def get_capture_failure(
        self,
        *,
        actor_uid: str,
        request_token: Optional[str],
    ) -> Optional[str]:
        """Recupera el resultado fallido idempotente sin guardar el token."""

        if not request_token:
            return None
        try:
            item = self.dynamo.get_item(
                self.rate_limits_table,
                {"rate_key": self._capture_request_key(actor_uid, request_token)},
                consistent_read=True,
            )
        except Exception as error:
            raise SessionCodeRateLimitUnavailable(
                "No se pudo consultar el estado idempotente del inicio."
            ) from error
        return item.get("failure_code") if item else None

    def register_capture_failure(
        self,
        *,
        client_ip: str,
        actor_uid: str,
        request_token: Optional[str],
        failure_code: str,
    ) -> bool:
        """Cuenta un PIN rechazado una sola vez por request_token.

        El contador y la marca idempotente se escriben en una transacción.
        Devuelve False cuando este mismo request_token ya se había contado.
        """

        counter_key = self._rate_key(client_ip)
        request_key = self._capture_request_key(actor_uid, request_token)
        now_epoch = int(self._now().timestamp())
        operations = [
            {
                "Update": {
                    "TableName": self.rate_limits_table,
                    "Key": {"rate_key": counter_key},
                    "UpdateExpression": (
                        "SET attempts = if_not_exists(attempts, :zero) + :one, "
                        "expires_at = :expires_at"
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(attempts) OR attempts < :max_attempts"
                    ),
                    "ExpressionAttributeValues": {
                        ":zero": 0,
                        ":one": 1,
                        ":max_attempts": self.MAX_FAILED_ATTEMPTS,
                        ":expires_at": now_epoch + self.RATE_LIMIT_TTL_SECONDS,
                    },
                }
            }
        ]
        if request_key:
            operations.append(
                {
                    "Put": {
                        "TableName": self.rate_limits_table,
                        "Item": {
                            "rate_key": request_key,
                            "failure_code": failure_code,
                            "expires_at": now_epoch + self.RATE_LIMIT_TTL_SECONDS,
                        },
                        "ConditionExpression": "attribute_not_exists(rate_key)",
                    }
                }
            )

        try:
            self.dynamo.transact_write_items(operations)
            return True
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code")
            if error_code in {
                "TransactionCanceledException",
                "ConditionalCheckFailedException",
            }:
                reasons = error.response.get("CancellationReasons") or []
                conditional_failure = error_code == "ConditionalCheckFailedException" or any(
                    reason.get("Code") == "ConditionalCheckFailed"
                    for reason in reasons
                )
                if not conditional_failure:
                    raise SessionCodeRateLimitUnavailable(
                        "No se pudo registrar el intento fallido."
                    ) from error
                if request_key:
                    try:
                        existing = self.dynamo.get_item(
                            self.rate_limits_table,
                            {"rate_key": request_key},
                            consistent_read=True,
                        )
                    except Exception as read_error:
                        raise SessionCodeRateLimitUnavailable(
                            "No se pudo comprobar el reintento del inicio."
                        ) from read_error
                    if existing:
                        return False
                raise SessionCodeRateLimitExceeded(
                    "Se superó el límite de intentos de validación del código."
                ) from error
            raise SessionCodeRateLimitUnavailable(
                "No se pudo registrar el intento fallido."
            ) from error
        except (SessionCodeRateLimitExceeded, SessionCodeRateLimitUnavailable):
            raise
        except Exception as error:
            raise SessionCodeRateLimitUnavailable(
                "No se pudo registrar el intento fallido."
            ) from error

    def check_capture_attempt(
        self,
        *,
        client_ip: str,
        actor_uid: str,
        request_token: Optional[str],
    ) -> Optional[str]:
        """Devuelve un fallo previo idempotente o bloquea por IP."""

        failure_code = self.get_capture_failure(
            actor_uid=actor_uid,
            request_token=request_token,
        )
        if failure_code:
            return failure_code
        if self._is_blocked(client_ip):
            raise SessionCodeRateLimitExceeded(
                "Se superó el límite de intentos de validación del código."
            )
        return None

    def reset_capture_attempts(self, client_ip: str) -> None:
        """Limpia los fallos previos después de una confirmación exitosa."""

        self._reset_failed_attempts(client_ip)

    def _reset_failed_attempts(self, client_ip: str) -> None:
        """Limpia el contador tras una validación exitosa."""

        try:
            self.dynamo.delete_item(
                self.rate_limits_table,
                {"rate_key": self._rate_key(client_ip)},
            )
        except Exception:
            # El reinicio es best-effort: no debe impedir la consulta.
            pass

    # ==========================================================
    # VALIDACIÓN DEL CÓDIGO
    # ==========================================================

    def _normalize_display_code(self, display_code: Any) -> str:
        if not isinstance(display_code, str):
            raise ValueError("El código debe contener 6 dígitos.")

        code = display_code.strip()

        if not self.DISPLAY_CODE_PATTERN.fullmatch(code):
            raise ValueError("El código debe contener 6 dígitos.")

        return code

    def _post(self, payload: Mapping[str, Any], *, confirm: bool = False) -> dict:
        """Ejecuta la llamada HTTPS al API Gateway del lente.

        Traduce las respuestas HTTP a errores tipados. Nunca expone el
        cuerpo crudo ni el `sessionToken`.

        Si `confirm` es verdadero y la petición termina en un timeout
        ambiguo (se envió pero no hubo respuesta), se propaga
        `LensApiConfirmationAmbiguous` en lugar de `LensApiUnavailable`:
        la confirmación pudo haberse aplicado y NO debe reintentarse
        automáticamente.
        """

        url = self._base_url() + DEFAULT_LENS_API_PATH

        try:
            response = self.http.post(
                url,
                json=dict(payload),
                timeout=self._timeout(),
            )
        except requests.exceptions.Timeout as error:
            if confirm:
                raise LensApiConfirmationAmbiguous(
                    "El lente no respondió a la confirmación en el tiempo "
                    "esperado; el resultado es indeterminado."
                ) from error

            raise LensApiUnavailable(
                "No se pudo contactar el servicio del lente."
            ) from error
        except requests.exceptions.RequestException as error:
            raise LensApiUnavailable(
                "No se pudo contactar el servicio del lente."
            ) from error

        status = response.status_code

        if status >= 500:
            raise LensApiUnavailable(
                f"El servicio del lente respondió {status}."
            )

        if status == 429:
            raise LensApiRateLimited(
                "El servicio del lente limitó los intentos."
            )

        try:
            data = response.json()
        except ValueError as error:
            raise LensApiError(
                "Respuesta inválida del servicio del lente."
            ) from error

        if status == 400:
            raise ValueError(
                "El servicio del lente rechazó el código."
            )

        if status == 404:
            raise LensSessionNotFound(
                "El código no corresponde a una sesión vigente."
            )

        if status == 409:
            raise LensSessionConflict(
                "La sesión del lente ya fue confirmada."
            )

        if status == 410:
            raise LensSessionExpired(
                "El código del lente expiró."
            )

        if status >= 400:
            raise LensApiError(
                f"El servicio del lente respondió {status}."
            )

        if not isinstance(data, dict) or not data.get("sessionId"):
            raise LensApiError(
                "Respuesta inválida del servicio del lente."
            )

        return data

    @staticmethod
    def _session_view(data: Mapping[str, Any], display_code: str) -> dict:
        return {
            "session_id": str(data.get("sessionId")),
            "display_code": display_code,
            "capture_status": data.get("status"),
            "created_at": data.get("createdAt"),
            "participant_id": data.get("idParticipante"),
        }

    # ==========================================================
    # AUDITORÍA
    # ==========================================================

    def _audit_event(
        self,
        action: str,
        actor_ctx: Optional[Mapping[str, Any]],
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
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
            # La consulta ya fue resuelta; la auditoría no debe
            # reportarse como un fallo de la operación.
            print(
                "Error registrando auditoría de código de sesión.",
                flush=True,
            )

    # ==========================================================
    # API PÚBLICA
    # ==========================================================

    def validate_display_code(self, display_code: Any) -> dict:
        """Valida el PIN sin consumirlo (`validateOnly = true`).

        No modifica la sesión original del lente. Devuelve el
        `lens_session_id` original.
        """

        code = self._normalize_display_code(display_code)

        data = self._post(
            {"displayCode": code, "validateOnly": True}
        )

        return self._session_view(data, code)

    def confirm_display_code(
        self,
        display_code: Any,
        specialist_id: str,
        confirmation_request_id: Optional[str] = None,
        patient_id: str = "",
    ) -> dict:
        """Confirma la sesión del lente (`validateOnly = false`).

        Envía `displayCode`, `specialistId` y `patientId`; si se dispone de
        `confirmation_request_id`, se reenvía como `confirmationRequestId`
        (la Lambda desplegada puede ignorarlo: NO se asume idempotencia
        remota; la prevención de doble confirmación es responsabilidad de
        EmotionLab).

        Un timeout ambiguo se propaga como `LensApiConfirmationAmbiguous`.
        """

        code = self._normalize_display_code(display_code)

        payload = {
            "displayCode": code,
            "validateOnly": False,
            "specialistId": specialist_id or "",
            "patientId": patient_id or "",
        }

        if confirmation_request_id:
            payload["confirmationRequestId"] = confirmation_request_id

        data = self._post(payload, confirm=True)

        return self._session_view(data, code)

    def lookup_by_display_code(self, display_code: Any) -> dict:
        """Valida la procedencia de un PIN (sin limitador ni auditoría).

        Se usa para validaciones automáticas, donde no hay entrada humana
        de códigos. Devuelve la captura ORIGINAL del lente.
        """

        return self.validate_display_code(display_code)

    def resolve_by_display_code(
        self,
        display_code: str,
        *,
        client_ip: str,
        actor_ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        """Valida el PIN con limitador de intentos y auditoría.

        Se usa en el flujo manual (el especialista introduce el código).
        Solo cuentan para el limitador las validaciones fallidas; una
        consulta exitosa reinicia el contador del origen.
        """

        code = self._normalize_display_code(display_code)

        if self._is_blocked(client_ip):
            raise SessionCodeRateLimitExceeded(
                "Se superó el límite de intentos de validación "
                "del código."
            )

        try:
            session = self.validate_display_code(code)

        except (
            LensSessionNotFound,
            LensSessionExpired,
        ):
            self._register_failed_attempt(client_ip)

            self._audit_event(
                AUDIT_SESSION_CODE_RESOLVE_FAILED,
                actor_ctx,
                {"reason": "not_found_or_expired"},
            )

            raise

        self._reset_failed_attempts(client_ip)

        self._audit_event(
            AUDIT_SESSION_CODE_RESOLVED,
            actor_ctx,
            {
                "session_id": session["session_id"],
            },
        )

        return session
