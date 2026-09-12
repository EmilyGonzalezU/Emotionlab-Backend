"""Servicio de predicción de EmotionLab.

La predicción se ejecuta remotamente mediante la API de AWS:
API Gateway -> Lambda -> modelo congelado.

El resto del backend mantiene el mismo contrato de predicción.
"""

import logging
import os
from typing import Any, Mapping, Optional

import httpx


logger = logging.getLogger(__name__)


class PredictionService:
    """Adaptador del backend hacia la API de predicción de AWS."""

    def __init__(
        self,
        model: Optional[Any] = None,
        api_url: Optional[str] = None,
    ):
        # ``model`` se mantiene por compatibilidad con tests existentes,
        # pero en producción la predicción se realiza mediante AWS.
        self._model = model

        self.api_url = (
            api_url
            or os.getenv("PREDICTION_API_URL", "").strip()
        )

        if not self.api_url:
            raise RuntimeError(
                "PREDICTION_API_URL no está configurada"
            )

    def predict(self, features: Mapping[str, Any]) -> dict:
        """Envía las features normalizadas a AWS y devuelve el contrato del dominio."""

        if not isinstance(features, Mapping):
            raise ValueError("features debe ser un objeto")

        payload = dict(features)

        try:
            response = httpx.post(
                self.api_url,
                json=payload,
                timeout=30.0,
            )
        except httpx.TimeoutException as error:
            logger.exception("Timeout al consultar la API de predicción")
            raise RuntimeError(
                "La API de predicción no respondió a tiempo"
            ) from error

        except httpx.HTTPError as error:
            logger.exception(
                "Error de comunicación con la API de predicción"
            )
            raise RuntimeError(
                "No se pudo conectar con la API de predicción"
            ) from error

        if response.status_code == 400:
            try:
                data = response.json()
                error_message = data.get(
                    "error",
                    "Datos inválidos para la predicción",
                )
            except ValueError:
                error_message = "Datos inválidos para la predicción"

            raise ValueError(error_message)

        if response.status_code >= 500:
            logger.error(
                "La API de predicción respondió HTTP %s",
                response.status_code,
            )
            raise RuntimeError(
                "La API de predicción presentó un error interno"
            )

        if response.status_code != 200:
            logger.error(
                "Respuesta inesperada de la API de predicción: HTTP %s",
                response.status_code,
            )
            raise RuntimeError(
                f"Error de la API de predicción: HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as error:
            logger.exception(
                "La API de predicción devolvió JSON inválido"
            )
            raise RuntimeError(
                "La API de predicción devolvió una respuesta inválida"
            ) from error

        if not isinstance(data, Mapping):
            raise ValueError(
                "La respuesta de predicción debe ser un objeto"
            )

        if data.get("ok") is not True:
            raise ValueError(
                data.get(
                    "error",
                    "La predicción no pudo completarse",
                )
            )

        resultado = data.get("resultado")

        if not isinstance(resultado, Mapping):
            raise ValueError(
                "La respuesta no contiene un resultado de predicción"
            )

        return self._normalise_prediction(resultado)

    @staticmethod
    def _normalise_prediction(model_result: Mapping[str, Any]) -> dict:
        """Normaliza la respuesta de AWS al contrato usado por EmotionLab."""

        if not isinstance(model_result, Mapping):
            raise ValueError(
                "El modelo debe retornar un objeto de predicción"
            )

        raw_prediction = model_result.get("prediccion_raw")
        classification = model_result.get("clasificacion")

        if raw_prediction is None and classification is None:
            raise ValueError(
                "La predicción no contiene una clasificación"
            )

        probability = model_result.get("probabilidad")
        confidence = model_result.get("confianza")

        if probability is None or confidence is None:
            raise ValueError(
                "La predicción no contiene probabilidad y confianza"
            )

        return {
            "bienestar": (
                "SI"
                if raw_prediction == 1
                or classification == "Alto Bienestar"
                else "NO"
            ),
            "probabilidad": PredictionService._as_fraction(
                probability
            ),
            "confianza": PredictionService._as_fraction(
                confidence
            ),
        }

    @staticmethod
    def _as_fraction(value: Any) -> float:
        """Convierte porcentajes como 63.8 a fracción 0.638."""

        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "La probabilidad debe ser numérica"
            ) from error

        return number / 100 if number > 1 else number