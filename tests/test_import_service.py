import unittest
from copy import deepcopy

from services.import_service import ImportService
from services.session_service import (
    STATUS_PENDING_ASSIGNMENT,
    STATUS_PENDING_PREDICTION,
    SessionService,
)


class FakeFirebase:
    def __init__(self):
        self.documents = {}

    def server_timestamp(self):
        return "SERVER_TIMESTAMP"

    def create_document(self, collection, document_id, data):
        self.documents.setdefault(collection, {})[document_id] = deepcopy(data)

    def get_document(self, collection, document_id):
        data = self.documents.get(collection, {}).get(document_id)
        return deepcopy(data) if data else None

    def list_documents(self, collection):
        return [deepcopy(data) for data in self.documents.get(collection, {}).values()]

    def query_documents(self, collection, field, operator, value):
        return [
            deepcopy(data)
            for data in self.documents.get(collection, {}).values()
            if operator == "==" and data.get(field) == value
        ]

    def update_document(self, collection, document_id, data):
        self.documents[collection][document_id].update(deepcopy(data))


class FakePredictor:
    def predecir(self, features):
        if features["intentos"] != 1 and features["intentos"] != 3:
            raise ValueError("Cantidad de intentos invalida")
        return {
            "prediccion_raw": 1,
            "clasificacion": "Alto Bienestar",
            "probabilidad": 93.0,
            "confianza": 97.0,
        }


class FakeAWSService:
    def __init__(self, payload):
        self.payload = payload

    def fetch_sessions(self):
        return self.payload


class ImportServiceTests(unittest.TestCase):
    def setUp(self):
        self.firebase = FakeFirebase()
        self.sessions = SessionService(self.firebase)
        self.service = ImportService(self.sessions, FakePredictor())

    @staticmethod
    def _record(*, attempts=None):
        data = {
            "idParticipante": "unity-participant-1",
            "fechaInicio": "2026-07-17 10:00:00",
            "fechaFin": "2026-07-17 10:05:00",
            "difficulty_level": "dificil",
            "respuestas": [
                {"idPregunta": "edad", "respuestaLabel": "16", "escena": "waiting_room_inicial"},
                {"idPregunta": "genero", "respuestaLabel": "Femenino"},
            ],
            "eventos": [],
        }
        if attempts is not None:
            data["intentos"] = attempts
        return {"s3_key": f"data/session-{attempts}.json", "session_data": data}

    def test_imports_sessions_with_explicit_or_default_attempts(self):
        second = self._record()
        second["session_data"]["fechaInicio"] = "2026-07-17 10:10:00"
        result = self.service.import_batch(
            {"total_sesiones": 2, "sesiones": [self._record(attempts=3), second]}
        )

        self.assertEqual(len(result["pending_identification"]), 1)
        self.assertEqual(len(result["pending_prediction"]), 1)
        predicted = self.sessions.get_session(result["pending_identification"][0])
        self.assertEqual(predicted["estado"], "pendiente_identificacion")
        self.assertEqual(predicted["prediction"]["bienestar"], "SI")
        self.assertEqual(predicted["prediction"]["probabilidad"], 0.93)
        second_prediction = self.sessions.get_session(result["pending_prediction"][0])
        self.assertEqual(second_prediction["estado"], "pending_prediction")

    def test_preserves_every_unity_upload_as_an_independent_session(self):
        partial = self._record(attempts=3)
        partial["timestamp_subida"] = "2026-07-17T10:00:00"
        partial["session_data"]["respuestas"].append({"idPregunta": "genero"})

        complete = self._record(attempts=3)
        complete["s3_key"] = "data/session-3-final.json"
        complete["timestamp_subida"] = "2026-07-17T10:01:00"
        complete["session_data"]["fechaFin"] = "2026-07-17 10:05:00"
        complete["session_data"]["respuestas"].extend(
            [{"idPregunta": "genero"}, {"idPregunta": "sentir_hoy"}]
        )

        result = self.service.import_batch({"sesiones": [partial, complete]})

        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["pending_identification"]), 2)
        saved = [self.sessions.get_session(session_id) for session_id in result["pending_identification"]]
        self.assertEqual(
            sorted(len(item["raw_json"]["session_data"]["respuestas"]) for item in saved),
            [3, 4],
        )

    def test_imports_a_batch_fetched_from_aws(self):
        result = self.service.import_from_aws(FakeAWSService({"sesiones": [self._record(attempts=3)]}))

        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["pending_identification"]), 1)

    def test_imports_activity_estimated_duration_and_defaults(self):
        record = self._record(attempts=3)
        data = record["session_data"]
        data.pop("fechaFin")
        data["respuestas"] = [{"timestamp": "2026-07-17 10:05:00"}]

        result = self.service.import_batch({"sesiones": [record]})

        session = self.sessions.get_session(result["pending_identification"][0])
        self.assertEqual(session["edad"], 15)
        self.assertEqual(session["genero"], "Prefiero no decir")
        self.assertTrue(session["duracion_estimada"])

    def test_leaves_pending_when_no_end_or_activity_exists(self):
        record = self._record(attempts=3)
        data = record["session_data"]
        data.pop("fechaFin")
        data["eventos"] = []
        data["respuestas"] = []

        result = self.service.import_batch({"sesiones": [record]})

        session = self.sessions.get_session(result["pending_prediction"][0])
        self.assertEqual(session["estado"], STATUS_PENDING_PREDICTION)
        self.assertEqual(
            session["prediction_missing_fields"],
            ["fechaFin/session_completed/actividad"],
        )

    def test_skips_only_the_same_aws_file_on_a_second_sync(self):
        payload = {"sesiones": [self._record(attempts=3)]}
        self.service.import_batch(payload)

        result = self.service.import_batch(payload)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pending_identification"], [])
        self.assertEqual(result["skipped_existing"], ["data/session-3.json"])
