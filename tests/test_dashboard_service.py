import unittest
from copy import deepcopy

from services.dashboard_service import DashboardService
from services.session_service import (
    STATUS_COMPLETED,
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


class DashboardServiceTests(unittest.TestCase):
    def setUp(self):
        sessions = SessionService(FakeFirebase())
        sessions.create_session(
            "s1", {"raw": 1}, status=STATUS_COMPLETED, fecha_inicio="2026-07-14 10:00:00",
            genero="Femenino", difficulty="dificil", prediction={"bienestar": "SI"},
        )
        sessions.create_session(
            "s2", {"raw": 2}, status=STATUS_PENDING_ASSIGNMENT,
            fecha_inicio="2026-07-14 11:00:00", genero="Masculino", difficulty="facil",
            prediction={"bienestar": "NO"},
        )
        sessions.create_session(
            "s3", {"raw": 3}, status=STATUS_PENDING_PREDICTION,
            fecha_inicio="2026-07-15 11:00:00", genero="Femenino", difficulty="dificil",
        )
        self.dashboard = DashboardService(sessions)

    def test_summary_and_recent_sessions_use_indexed_fields(self):
        summary = self.dashboard.get_summary(genero="Femenino")

        self.assertEqual(summary["total_sesiones"], 2)
        self.assertEqual(summary["bienestar_si"], 1)
        self.assertEqual(summary["pending_prediction"], 1)
        self.assertNotIn("raw_json", self.dashboard.get_recent_sessions()[0])

    def test_chart_groups_sessions_by_date(self):
        self.assertEqual(
            self.dashboard.get_sessions_by_date(),
            [
                {"fecha": "2026-07-14", "total": 2, "SI": 1, "NO": 1},
                {"fecha": "2026-07-15", "total": 1, "SI": 0, "NO": 0},
            ],
        )
