import unittest
from copy import deepcopy

from services.session_service import (
    STATUS_COMPLETED,
    STATUS_IMPORT_ERROR,
    STATUS_PENDING_ASSIGNMENT,
    STATUS_PENDING_PREDICTION,
    SessionService,
)
from core.exceptions import SessionNotFound


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

    def delete_document(self, collection, document_id):
        del self.documents[collection][document_id]


class SessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.firebase = FakeFirebase()
        self.service = SessionService(self.firebase)

    def test_create_and_filter_sessions(self):
        self.service.create_session(
            "session-1",
            {"source": "unity"},
            status=STATUS_PENDING_PREDICTION,
            fecha_inicio="2026-07-14 10:00:00",
            edad=16,
            genero="Femenino",
            difficulty="dificil",
        )
        self.service.create_session(
            "session-2",
            {"source": "unity"},
            status=STATUS_PENDING_ASSIGNMENT,
            fecha_inicio="2026-07-15 10:00:00",
        )

        result = self.service.list_sessions_by_date("2026-07-15")

        self.assertEqual([session["session_id"] for session in result], ["session-2"])
        self.assertEqual(self.service.list_dates(), ["2026-07-15", "2026-07-14"])
        self.assertEqual(result[0]["fecha"], "2026-07-15")
        self.assertEqual(result[0]["hora"], "10:00")
        self.assertEqual(len(self.service.list_pending_sessions()), 2)
        self.assertEqual(self.service.list_unassigned_sessions(), [])

    def test_prediction_and_assignment_update_status(self):
        self.service.create_session("session-1", {"source": "unity"})

        self.service.update_prediction(
            "session-1", {"bienestar": "SI", "probabilidad": 0.93, "confianza": 0.97}
        )
        self.service.update_name("session-1", "Ana Lopez")

        session = self.service.get_session("session-1")
        self.assertEqual(session["estado"], "identificada")
        self.assertEqual(session["nombre"], "Ana Lopez")
        self.assertEqual(session["prediction"]["bienestar"], "SI")

    def test_teacher_can_update_name_and_observations(self):
        self.service.create_session("session-1", {"source": "unity"})

        self.service.update_session(
            "session-1",
            {"nombre": "Ana Lopez", "observaciones": "Mostro nerviosismo inicial."},
        )

        session = self.service.get_session("session-1")
        self.assertEqual(session["nombre"], "Ana Lopez")
        self.assertEqual(session["estado"], "identificada")
        self.assertEqual(session["observaciones"], "Mostro nerviosismo inicial.")

    def test_assignment_requires_an_existing_session(self):
        with self.assertRaises(SessionNotFound):
            self.service.assign_participant("missing", "participant-1")

    def test_explicit_status_and_detail(self):
        self.service.create_session("session-1", {"source": "unity"})

        self.service.update_status("session-1", STATUS_IMPORT_ERROR)

        detail = self.service.get_session_detail("session-1")
        self.assertEqual(detail["status"], STATUS_IMPORT_ERROR)
        self.assertEqual(detail["raw_json"], {"source": "unity"})
