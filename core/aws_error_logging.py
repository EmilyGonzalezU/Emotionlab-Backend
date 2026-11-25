"""Diagnóstico seguro de errores de AWS (botocore) para el log local.

Extrae de un ``ClientError`` únicamente metadatos NO sensibles:

    - servicio AWS
    - operación AWS
    - código de error
    - mensaje de error
    - Request ID
    - módulo de la aplicación donde ocurrió

Nunca registra contraseñas, tokens JWT, secretos, credenciales AWS,
datos personales innecesarios ni los parámetros completos de la petición.
"""

from __future__ import annotations

import re
from typing import Any

from botocore.exceptions import ClientError


# Las respuestas de AccessDenied incluyen "perform: <servicio>:<Accion>".
_SERVICE_IN_MESSAGE = re.compile(r"perform:\s*([a-z0-9-]+):")


def aws_error_context(error: ClientError) -> dict[str, Any]:
    """Devuelve el contexto no sensible de un ``ClientError``."""

    response = getattr(error, "response", None) or {}
    err = response.get("Error") or {}
    metadata = response.get("ResponseMetadata") or {}

    message = err.get("Message") or ""
    match = _SERVICE_IN_MESSAGE.search(message)

    return {
        "aws_service": match.group(1) if match else "",
        "aws_operation": getattr(error, "operation_name", "") or "",
        "error_code": err.get("Code") or "",
        "error_message": message,
        "request_id": metadata.get("RequestId") or "",
        "module": _origin_module(error),
    }


def _origin_module(error: BaseException) -> str:
    """Módulo de la aplicación donde se originó el error.

    Recorre el traceback y conserva el último frame que NO proviene de
    dependencias instaladas (``site-packages``/``.venv``), es decir, el
    punto de código de EmotionLab que invocó AWS.
    """

    module = ""
    tb = error.__traceback__
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if "site-packages" not in filename and ".venv" not in filename:
            module = tb.tb_frame.f_globals.get("__name__", module)
        tb = tb.tb_next
    return module


def log_aws_client_error(logger: Any, error: ClientError) -> None:
    """Registra el diagnóstico seguro conservando el traceback técnico."""

    context = aws_error_context(error)
    logger.exception(
        "AWS ClientError: service=%s operation=%s code=%s module=%s "
        "request_id=%s message=%s",
        context["aws_service"],
        context["aws_operation"],
        context["error_code"],
        context["module"],
        context["request_id"],
        context["error_message"],
    )
