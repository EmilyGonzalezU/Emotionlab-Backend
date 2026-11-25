"""emotionlabtest-confirmSession — validación y confirmación del PIN del lente.

Esta Lambda **pertenece a la cuenta de los lentes** (339713058978) y se
invoca desde el API Gateway REST `emotionlab-api-test` (id `cntbcwfel0`)
en la ruta `POST /session/confirm`. Su despliegue es **manual** y requiere
autorización en esa cuenta: EmotionLab-Backend solo versiona el código.

Se expone aquí como referencia para sustituir la función existente en la
consola AWS de los lentes. No se despliega desde este repositorio.

Dos modalidades:

    validateOnly = true   -> valida el PIN SIN modificar DynamoDB.
    validateOnly = false  -> confirma la sesión (pending -> confirmed).

Tabla consultada (de la cuenta de los lentes):

    emotionlab-sessions
        PK  sessionId
        GSI DisplayCodeIndex (PK displayCode)

Atributos relevantes: sessionId, displayCode, sessionToken, idParticipante,
status, createdAt, ttl.

Contrato del índice `DisplayCodeIndex`:

    El GSI debe proyectar `ALL` o, como mínimo, los atributos `status`, `ttl`,
    `idParticipante` y `createdAt`. Sin ellos no es posible validar ni
    confirmar la sesión. La configuración del índice NO se modifica desde este
    código: si una consulta devuelve registros sin esos atributos, la Lambda
    responde 500 y NO confirma nada (proyección insuficiente).

Variables de entorno:

    DYNAMODB_TABLE      nombre de la tabla (compatibilidad con la Lambda
                        anterior). Si no está, se usa `SESSIONS_TABLE`.
    SESSIONS_TABLE      alternativa a `DYNAMODB_TABLE`.
    DISPLAY_CODE_INDEX  nombre del GSI (por defecto `DisplayCodeIndex`).
    AWS_REGION          región de DynamoDB.

Seguridad:

    - Nunca se devuelve `sessionToken`.
    - El PIN no se registra en los logs.
    - La transición de estado es condicional (protege la concurrencia).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# PIN del lente: exactamente 6 dígitos.
DISPLAY_CODE_PATTERN = re.compile(r"[0-9]{6}")

DEFAULT_TABLE_NAME = "emotionlab-sessions"
DEFAULT_DISPLAY_CODE_INDEX = "DisplayCodeIndex"

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"

# Compatibilidad con la Lambda original: si el Dashboard no informa
# `specialistId`, se registra este valor anónimo.
DEFAULT_SPECIALIST_ID = "especialista_anonimo"

# Atributos que el GSI `DisplayCodeIndex` debe proyectar como mínimo.
REQUIRED_INDEX_ATTRIBUTES = ("status", "ttl", "idParticipante", "createdAt")

# La escritura del GSI es eventualmente consistente: ante una lectura
# inmediatamente posterior a `createSession` se reintenta unas pocas veces
# breves y limitadas antes de responder 404.
QUERY_RETRY_ATTEMPTS = 3
QUERY_RETRY_DELAY_SECONDS = 0.15

# Compatibilidad con la Lambda anterior (CORS habilitado).
CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}


# ==========================================================
# VALIDACIONES Y HELPERS
# ==========================================================


def _now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).replace(
        microsecond=0
    ).isoformat()


def _coerce_ttl(value):
    """Normaliza `ttl` a epoch (segundos). Devuelve None si no es válido."""

    if value is None or isinstance(value, bool):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_expired(item, now_epoch: int) -> bool:
    """Comprueba la vigencia con `ttl` (segundos epoch), no con el TTL físico.

    DynamoDB puede tardar hasta 48 h en borrar un registro vencido, por lo
    que la expiración se decide aquí de forma explícita. Una sesión **sin un
    `ttl` numérico válido no es confirmable** y se considera vencida.
    """

    ttl_epoch = _coerce_ttl(item.get("ttl"))

    if ttl_epoch is None:
        return True

    return now_epoch >= ttl_epoch


def _is_valid_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _missing_index_attributes(item) -> tuple:
    """Atributos del contrato del índice que faltan en el registro.

    Detecta proyecciones insuficientes del GSI (p. ej. `KEYS_ONLY`). Si falta
    alguno de los atributos requeridos, la sesión no puede validarse ni
    confirmarse de forma fiable.
    """

    return tuple(
        name
        for name in REQUIRED_INDEX_ATTRIBUTES
        if item.get(name) is None
    )


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": dict(CORS_HEADERS),
        "body": json.dumps(body, ensure_ascii=False),
    }


def _bad_request(message: str) -> dict:
    return _response(400, {"success": False, "error": message})


def _internal_error() -> dict:
    return _response(
        500, {"success": False, "error": "Error interno."}
    )


def _parse_body(event) -> dict:
    """Acepta invocación por API Gateway (proxy) o directa."""

    if not isinstance(event, dict):
        return {}

    body = event.get("body")

    if isinstance(body, str):
        raw = body
        if event.get("isBase64Encoded"):
            try:
                raw = base64.b64decode(body).decode("utf-8")
            except Exception:  # noqa: BLE001
                return {}
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # Invocación directa (consola/pruebas): los campos van en el evento.
    return event


def _public_view(item: dict) -> dict:
    """Campos permitidos en la respuesta. Nunca incluye `sessionToken`."""

    return {
        "sessionId": item.get("sessionId"),
        "idParticipante": item.get("idParticipante"),
        "status": item.get("status"),
        "createdAt": item.get("createdAt"),
    }


def _confirm_condition(now_epoch: int):
    """Condición de la transición: sigue pendiente y aún vigente."""

    return Attr("status").eq(STATUS_PENDING) & Attr("ttl").gt(now_epoch)


def _query_by_display_code(table, display_code: str) -> list:
    """Consulta el GSI recorriendo TODAS las páginas (`LastEvaluatedKey`)."""

    index = os.environ.get(
        "DISPLAY_CODE_INDEX", DEFAULT_DISPLAY_CODE_INDEX
    )

    items = []
    start_key = None

    while True:
        params = {
            "IndexName": index,
            "KeyConditionExpression": Key("displayCode").eq(display_code),
        }

        if start_key is not None:
            params["ExclusiveStartKey"] = start_key

        response = table.query(**params)
        items.extend(response.get("Items") or [])

        start_key = response.get("LastEvaluatedKey")

        if not start_key:
            break

    return items


def _query_with_retry(
    table,
    display_code: str,
    *,
    attempts: int = QUERY_RETRY_ATTEMPTS,
    delay: float = QUERY_RETRY_DELAY_SECONDS,
    sleep=None,
) -> list:
    """Reintenta unas pocas veces si el GSI aún no refleja la sesión."""

    sleep = sleep or time.sleep
    items = []

    for attempt in range(attempts):
        items = _query_by_display_code(table, display_code)

        if items:
            return items

        if attempt < attempts - 1:
            sleep(delay)

    return items


# ==========================================================
# NÚCLEO (testeable de forma aislada)
# ==========================================================


def _reload(table, session_id):
    """Recupera el estado actual con lectura consistente.

    No oculta errores de DynamoDB: si la operación falla, la excepción se
    propaga para que el llamador responda 500 (nunca 409 espurio).
    """

    if not session_id:
        return None

    response = table.get_item(
        Key={"sessionId": session_id},
        ConsistentRead=True,
    )
    return response.get("Item")


def _confirm(
    table,
    target: dict,
    specialist_id: str,
    patient_id: str,
    confirmation_request_id,
    now: datetime,
):
    """Transición condicional pending -> confirmed, con idempotencia."""

    from botocore.exceptions import ClientError

    session_id = target.get("sessionId")
    now_epoch = int(now.timestamp())

    values = {
        ":confirmed": STATUS_CONFIRMED,
        ":specialist": specialist_id,
        ":patient": patient_id or "",
        ":confirmed_at": _now_iso(now),
    }

    set_parts = [
        "#st = :confirmed",
        "specialistId = :specialist",
        "patientId = :patient",
        "confirmedAt = :confirmed_at",
    ]

    if confirmation_request_id:
        set_parts.append("confirmationRequestId = :rid")
        values[":rid"] = confirmation_request_id

    try:
        response = table.update_item(
            Key={"sessionId": session_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression=_confirm_condition(now_epoch),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )

    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")

        if error_code != "ConditionalCheckFailedException":
            logger.exception("Fallo confirmando la sesión del lente.")
            return _internal_error()

        # Otro intento cambió el estado: comprobar idempotencia.
        try:
            current = _reload(table, session_id)
        except Exception:  # noqa: BLE001
            logger.exception("Fallo recuperando la sesión del lente.")
            return _internal_error()

        if (
            current
            and current.get("status") == STATUS_CONFIRMED
            and confirmation_request_id
            and current.get("confirmationRequestId")
            == confirmation_request_id
        ):
            view = _public_view(current)
            view["success"] = True
            view["idempotent"] = True
            return _response(200, view)

        return _response(
            409,
            {
                "success": False,
                "error": "La sesión del lente ya fue confirmada.",
            },
        )

    except Exception:  # noqa: BLE001
        logger.exception("Fallo confirmando la sesión del lente.")
        return _internal_error()

    item = response.get("Attributes") or target

    view = _public_view(item)
    view["success"] = True
    return _response(200, view)


def _handle(event, *, table, now: datetime, sleep=None) -> dict:
    # Preflight CORS (no altera métodos ni rutas de API Gateway).
    if isinstance(event, dict) and event.get("httpMethod") == "OPTIONS":
        return _response(200, {"success": True})

    body = _parse_body(event)

    if not body:
        return _bad_request("Cuerpo JSON inválido.")

    display_code = body.get("displayCode")

    if not isinstance(display_code, str) or not DISPLAY_CODE_PATTERN.fullmatch(
        display_code.strip()
    ):
        return _bad_request("displayCode inválido.")

    display_code = display_code.strip()

    validate_only_raw = body.get("validateOnly", False)

    if not isinstance(validate_only_raw, bool):
        return _bad_request("validateOnly debe ser booleano.")

    validate_only = validate_only_raw

    specialist_raw = body.get("specialistId")

    if specialist_raw is not None and not isinstance(specialist_raw, str):
        return _bad_request("specialistId inválido.")

    specialist_id = (specialist_raw or "").strip() or DEFAULT_SPECIALIST_ID

    patient_raw = body.get("patientId")

    if patient_raw is not None and not isinstance(patient_raw, str):
        return _bad_request("patientId inválido.")

    patient_id = (patient_raw or "").strip()

    # El Dashboard anterior puede omitir el identificador; si llega, debe ser
    # un UUID válido (EmotionLab usa UUID v5 derivado del token de inicio).
    rid_raw = body.get("confirmationRequestId")

    if rid_raw is None:
        confirmation_request_id = None
    elif not isinstance(rid_raw, str) or not _is_valid_uuid(rid_raw.strip()):
        return _bad_request("confirmationRequestId inválido.")
    else:
        confirmation_request_id = rid_raw.strip()

    try:
        items = _query_with_retry(
            table, display_code, sleep=sleep
        )
    except Exception:  # noqa: BLE001
        logger.exception("Fallo consultando la sesión del lente.")
        return _internal_error()

    if not items:
        return _response(
            404,
            {
                "success": False,
                "error": "El código no corresponde a una sesión vigente.",
            },
        )

    # El GSI `DisplayCodeIndex` debe proyectar los atributos mínimos. Si los
    # registros devueltos no incluyen `status`, el índice no permite validar la
    # sesión: no se confirma nada y se señala la mala configuración.
    if not any(item.get("status") is not None for item in items):
        logger.error(
            "El índice %s no proyecta `status`; no se puede validar la sesión.",
            os.environ.get(
                "DISPLAY_CODE_INDEX", DEFAULT_DISPLAY_CODE_INDEX
            ),
        )
        return _response(
            500,
            {
                "success": False,
                "error": "Índice de sesiones sin la proyección requerida.",
            },
        )

    now_epoch = int(now.timestamp())

    alive_pending = [
        item
        for item in items
        if item.get("status") == STATUS_PENDING
        and not _is_expired(item, now_epoch)
    ]

    if len(alive_pending) > 1:
        # Nunca se elige arbitrariamente entre varias sesiones pendientes.
        logger.error(
            "Múltiples sesiones pendientes para un mismo displayCode (%d).",
            len(alive_pending),
        )
        return _response(
            409,
            {
                "success": False,
                "error": (
                    "Existe más de una sesión pendiente para este código."
                ),
            },
        )

    if len(alive_pending) == 1:
        target = alive_pending[0]

        # Proyección insuficiente: el registro no permite devolver ni validar
        # una sesión completa. No se confirma.
        missing = _missing_index_attributes(target)

        if missing:
            logger.error(
                "El registro del lente no incluye los atributos requeridos "
                "(%s); revise la proyección del índice.",
                ", ".join(missing),
            )
            return _response(
                500,
                {
                    "success": False,
                    "error": (
                        "Registro de sesión sin la proyección requerida."
                    ),
                },
            )

        if validate_only:
            view = _public_view(target)
            view["success"] = True
            return _response(200, view)

        return _confirm(
            table,
            target,
            specialist_id,
            patient_id,
            confirmation_request_id,
            now,
        )

    # No hay sesiones pendientes vigentes.
    confirmed = [
        item for item in items if item.get("status") == STATUS_CONFIRMED
    ]

    if confirmed:
        if confirmation_request_id:
            match = [
                item
                for item in confirmed
                if item.get("confirmationRequestId")
                == confirmation_request_id
            ]
            if match:
                view = _public_view(match[0])
                view["success"] = True
                view["idempotent"] = True
                return _response(200, view)

        return _response(
            409,
            {
                "success": False,
                "error": "La sesión del lente ya fue confirmada.",
            },
        )

    expired = [item for item in items if _is_expired(item, now_epoch)]

    if expired:
        return _response(
            410,
            {"success": False, "error": "El código del lente expiró."},
        )

    return _response(
        409,
        {
            "success": False,
            "error": "La sesión del lente no está disponible.",
        },
    )


# ==========================================================
# ENTRYPOINT AWS LAMBDA
# ==========================================================


def _get_table():
    import boto3

    # Compatibilidad: la Lambda anterior usaba `DYNAMODB_TABLE`.
    table_name = (
        os.environ.get("DYNAMODB_TABLE")
        or os.environ.get("SESSIONS_TABLE")
        or DEFAULT_TABLE_NAME
    )
    region = os.environ.get("AWS_REGION")

    resource = (
        boto3.resource("dynamodb", region_name=region)
        if region
        else boto3.resource("dynamodb")
    )

    return resource.Table(table_name)


def handler(event, context):  # noqa: ARG001
    try:
        return _handle(
            event,
            table=_get_table(),
            now=datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Error interno en confirmSession.")
        return _internal_error()
