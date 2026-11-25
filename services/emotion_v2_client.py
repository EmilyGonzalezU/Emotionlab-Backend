"""Cliente del servicio de inferencia V2 (K-Means congelado) de EmotionLab.

El backend Flask NO carga el artefacto ``.joblib`` en su entorno principal.
Delega la inferencia en un servicio dedicado (Lambda/imagen Docker) que
ejecuta el Pipeline con las versiones EXACTAS de scikit-learn con las que el
artefacto fue empaquetado.

Configuración única:

* ``EMOTION_MODEL_V2_API_URL``  URL del servicio de inferencia V2.
* ``EMOTION_MODEL_V2_TIMEOUT``  timeout de la petición, en segundos (15 por
  defecto).

El contrato de entrada es exclusivamente el de las seis variables del modelo
(ver ``services.emotion_model_v2_service``). El servicio responde con la
trazabilidad completa (imputación, medianas aplicadas, indicadores y grupo
K-Means) para que el backend pueda persistirla bajo el mismo ``session_id``.

Este módulo NO replica la lógica del motor: su única responsabilidad es
transportar la solicitud y validar la respuesta.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping, Optional

import requests

from services.emotion_model_v2_contract import (
    VARIABLES_MODELO,
    ModelV2ConfigError,
    ModelV2Error,
    build_error_block,
)


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0


class EmotionV2Client:
    """Adaptador HTTP del backend hacia el servicio de inferencia V2."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
        transport=None,
    ) -> None:
        configured_url = (
            api_url
            if api_url is not None
            else os.getenv("EMOTION_MODEL_V2_API_URL", "")
        )

        self.api_url = (configured_url or "").strip()

        configured_timeout = (
            timeout
            if timeout is not None
            else os.getenv("EMOTION_MODEL_V2_TIMEOUT", str(DEFAULT_TIMEOUT))
        )

        try:
            self.timeout = float(configured_timeout)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT

        # ``transport`` permite inyectar un doble en pruebas; en producción es
        # el módulo ``requests``.
        self._transport = transport or requests

    @property
    def configured(self) -> bool:
        """Indica si el backend debe delegar la inferencia en el servicio V2."""

        return bool(self.api_url)

    # ------------------------------------------------------
    # INFERENCIA
    # ------------------------------------------------------

    def analyze(
        self,
        session_id: str,
        valores_originales: Mapping[str, Any],
        *,
        include_indicators: bool = True,
    ) -> dict[str, Any]:
        """Ejecuta la inferencia V2 remota para una sesión.

        Devuelve el bloque de trazabilidad tal como lo persiste el backend.
        Lanza ``ModelV2Error`` ante cualquier fallo de transporte, protocolo o
        validación de la respuesta: el llamador decide cómo degradar
        (normalmente ``processing_error``) sin invalidar la captura.
        """

        if not self.configured:
            raise ModelV2ConfigError(
                "EMOTION_MODEL_V2_API_URL no está configurada."
            )

        payload = {
            "session_id": str(session_id),
            "values": {
                variable: valores_originales.get(variable)
                for variable in VARIABLES_MODELO
            },
            "include_indicators": bool(include_indicators),
        }

        try:
            response = self._transport.post(
                self.api_url,
                json=payload,
                timeout=self.timeout,
            )
        except Exception as error:  # transporte: timeout, DNS, TLS, etc.
            raise ModelV2Error(
                "No se pudo contactar el servicio de inferencia V2 "
                f"({type(error).__name__})."
            ) from error

        status_code = getattr(response, "status_code", None)

        if status_code != 200:
            logger.error(
                "El servicio de inferencia V2 respondió HTTP %s",
                status_code,
            )

            raise ModelV2Error(
                "El servicio de inferencia V2 devolvió "
                f"HTTP {status_code}."
            )

        try:
            body = response.json()
        except Exception as error:
            raise ModelV2Error(
                "El servicio de inferencia V2 devolvió una respuesta ilegible."
            ) from error

        if not isinstance(body, Mapping) or body.get("ok") is not True:
            raise ModelV2Error(
                "El servicio de inferencia V2 no completó el análisis."
            )

        analisis = body.get("analysis")

        if not isinstance(analisis, Mapping):
            raise ModelV2Error(
                "El servicio de inferencia V2 no devolvió un análisis válido."
            )

        resultado = dict(analisis)
        resultado["session_id"] = str(session_id)

        return resultado

    @staticmethod
    def error_block(error: Exception) -> dict[str, Any]:
        """Bloque homogéneo de error, sin datos sensibles."""

        return build_error_block(error)


class EmotionV2LambdaClient:
    """Cliente para invocar EmotionLabModelFunction directamente mediante boto3.

    Alternativa a ``EmotionV2Client`` cuando el backend y el motor V2 viven en
    la misma cuenta AWS: en lugar de una petición HTTP, invoca la función
    Lambda de inferencia con el MISMO contrato de entrada/salida.

    Configuración única:

    * ``EMOTION_MODEL_V2_FUNCTION_NAME``  nombre de la función Lambda
      (``EmotionLabModelFunction`` por defecto).
    """

    def __init__(
        self,
        function_name: Optional[str] = None,
        lambda_client=None,
    ) -> None:
        configured_name = (
            function_name
            if function_name is not None
            else os.getenv(
                "EMOTION_MODEL_V2_FUNCTION_NAME",
                "EmotionLabModelFunction",
            )
        )

        self.function_name = (configured_name or "").strip()

        # ``lambda_client`` permite inyectar un doble en pruebas; en producción
        # se construye un cliente boto3 perezosamente.
        self._lambda = lambda_client

    @property
    def configured(self) -> bool:
        """Indica si el backend debe delegar la inferencia en la Lambda V2."""

        return bool(self.function_name)

    @property
    def client(self):
        """Cliente boto3 de Lambda (creado perezosamente)."""

        if self._lambda is None:
            import boto3

            self._lambda = boto3.client("lambda")

        return self._lambda

    # ------------------------------------------------------
    # INFERENCIA
    # ------------------------------------------------------

    def analyze(
        self,
        session_id: str,
        valores_originales: Mapping[str, Any],
        *,
        include_indicators: bool = True,
    ) -> dict[str, Any]:
        """Ejecuta la inferencia V2 invocando la Lambda directamente.

        Devuelve el bloque de trazabilidad tal como lo persiste el backend.
        Lanza ``ModelV2Error`` ante cualquier fallo de invocación, protocolo o
        validación de la respuesta: el llamador decide cómo degradar
        (normalmente ``processing_error``) sin invalidar la captura.
        """

        if not self.configured:
            raise ModelV2ConfigError(
                "EMOTION_MODEL_V2_FUNCTION_NAME no está configurada."
            )

        payload = {
            "session_id": str(session_id),
            "values": {
                variable: valores_originales.get(variable)
                for variable in VARIABLES_MODELO
            },
            "include_indicators": bool(include_indicators),
        }

        try:
            response = self.client.invoke(
                FunctionName=self.function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode("utf-8"),
            )
        except Exception as error:  # invocación: permisos, red, recurso, etc.
            raise ModelV2Error(
                "No se pudo invocar la función Lambda de inferencia V2 "
                f"({type(error).__name__})."
            ) from error

        if response.get("FunctionError"):
            raise ModelV2Error(
                "La función Lambda de inferencia V2 devolvió un error de "
                "ejecución."
            )

        streaming_payload = response.get("Payload")

        if streaming_payload is None:
            raise ModelV2Error(
                "La función Lambda de inferencia V2 no devolvió respuesta."
            )

        # La función responde con el envoltorio tipo API: ``{"statusCode",
        # "headers", "body"}`` donde ``body`` es el JSON serializado.
        try:
            envelope = json.loads(streaming_payload.read())
        except Exception as error:
            raise ModelV2Error(
                "La función Lambda de inferencia V2 devolvió una respuesta "
                "ilegible."
            ) from error

        if not isinstance(envelope, Mapping):
            raise ModelV2Error(
                "La función Lambda de inferencia V2 devolvió una respuesta "
                "inválida."
            )

        status_code = envelope.get("statusCode")

        if status_code != 200:
            logger.error(
                "La función Lambda de inferencia V2 respondió HTTP %s",
                status_code,
            )

            raise ModelV2Error(
                "La función Lambda de inferencia V2 devolvió "
                f"HTTP {status_code}."
            )

        body = envelope.get("body")

        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception as error:
                raise ModelV2Error(
                    "La función Lambda de inferencia V2 devolvió un cuerpo "
                    "ilegible."
                ) from error

        if not isinstance(body, Mapping) or body.get("ok") is not True:
            raise ModelV2Error(
                "La función Lambda de inferencia V2 no completó el análisis."
            )

        analisis = body.get("analysis")

        if not isinstance(analisis, Mapping):
            raise ModelV2Error(
                "La función Lambda de inferencia V2 no devolvió un análisis "
                "válido."
            )

        resultado = dict(analisis)
        resultado["session_id"] = str(session_id)

        return resultado

    @staticmethod
    def error_block(error: Exception) -> dict[str, Any]:
        """Bloque homogéneo de error, sin datos sensibles."""

        return build_error_block(error)
