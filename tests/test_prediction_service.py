import unittest

from services.prediction_service import PredictionService


class FakeModel:
    def predecir(self, features):
        self.features = features
        return {
            "prediccion_raw": 1,
            "clasificacion": "Alto Bienestar",
            "probabilidad": 91,
            "confianza": 97,
        }


class PredictionServiceTests(unittest.TestCase):
    def test_returns_domain_prediction_without_infrastructure(self):
        model = FakeModel()
        service = PredictionService(model)

        result = service.predict({"edad": 16})

        self.assertEqual(model.features, {"edad": 16})
        self.assertEqual(
            result,
            {"bienestar": "SI", "probabilidad": 0.91, "confianza": 0.97},
        )

    def test_rejects_a_model_result_without_confidence(self):
        class IncompleteModel:
            def predecir(self, features):
                return {"prediccion_raw": 0, "probabilidad": 20}

        with self.assertRaisesRegex(ValueError, "confianza"):
            PredictionService(IncompleteModel()).predict({"edad": 16})
