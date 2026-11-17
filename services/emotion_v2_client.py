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

import logging
import os
from typing import Any, Mapping, Optional

import requests

from services.emotion_model_v2_service import (
    VARIABLES_MODELO,
    ModelV2ConfigError,
    ModelV2Error,
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

        from services.emotion_model_v2_service import EmotionModelV2Service

        return EmotionModelV2Service.error_block(error)
