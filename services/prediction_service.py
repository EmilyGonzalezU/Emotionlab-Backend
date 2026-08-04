"""Servicio aislado para ejecutar el modelo de bienestar.

No conoce Firestore, AWS ni la estructura de las sesiones: recibe solamente
las *features* normalizadas y devuelve el contrato de prediccion del dominio.
"""

from typing import Any, Mapping, Optional


class PredictionService:
    """Adaptador del modelo congelado al contrato usado por EmotionLab."""

    def __init__(self, model: Optional[Any] = None):
        # La carga es diferida para que iniciar la API no dependa del archivo
        # del modelo cuando todavia no se ha solicitado una prediccion.
        self._model = model

    def predict(self, features: Mapping[str, Any]) -> dict:
        """Predice bienestar a partir de features ya normalizadas."""
        if not isinstance(features, Mapping):
            raise ValueError("features debe ser un objeto")

        result = self._get_model().predecir(dict(features))
        return self._normalise_prediction(result)

    def _get_model(self) -> Any:
        if self._model is None:
            from utils.predictor import Predictor

            self._model = Predictor()
        return self._model

    @staticmethod
    def _normalise_prediction(model_result: Mapping[str, Any]) -> dict:
        if not isinstance(model_result, Mapping):
            raise ValueError("El modelo debe retornar un objeto de prediccion")

        raw_prediction = model_result.get("prediccion_raw")
        classification = model_result.get("clasificacion")
        if raw_prediction is None and classification is None:
            raise ValueError("La prediccion no contiene una clasificacion")

        probability = model_result.get("probabilidad")
        confidence = model_result.get("confianza")
        if probability is None or confidence is None:
            raise ValueError("La prediccion no contiene probabilidad y confianza")

        return {
            "bienestar": "SI" if raw_prediction == 1 or classification == "Alto Bienestar" else "NO",
            "probabilidad": PredictionService._as_fraction(probability),
            "confianza": PredictionService._as_fraction(confidence),
        }

    @staticmethod
    def _as_fraction(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("La probabilidad debe ser numerica") from error
        return number / 100 if number > 1 else number
