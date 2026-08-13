import unittest
from copy import deepcopy
from unittest.mock import patch

from core.constants import COLLECTION_USER_ACTIVITY, COLLECTION_USERS
from services.user_service import UserService


class FakeFirebase:
    def __init__(self):
        self.documents = {}

    def server_timestamp(self):
        return "SERVER_TIMESTAMP"

    def create_document(self, collection, document_id, data):
        self.documents.setdefault(collection, {})[document_id] = deepcopy(data)

    def get_document(self, collection, document_id):
        data = self.documents.get(collection, {}).get(document_id)
        return deepcopy(data) if data is not None else None

    def list_documents(self, collection):
        return [deepcopy(data) for data in self.documents.get(collection, {}).values()]

    def query_documents(self, collection, field, operator, value):
        return [
            deepcopy(data)
            for data in self.documents.get(collection, {}).values()
            if operator == "==" and data.get(field) == value
        ]

    def update_document(self, collection, document_id, data):
        self.documents.setdefault(collection, {})[document_id].update(deepcopy(data))


class UserServiceTests(unittest.TestCase):
    def setUp(self):
        self.firebase = FakeFirebase()
        self.firebase.create_document(
            COLLECTION_USERS,
            "admin-1",
            {
                "uid": "admin-1",
                "nombre": "Administrador",
                "email": "admin@emotionlab.test",
                "rol": "admin",
                "activo": True,
            },
        )
        self.patcher = patch("services.user_service.firebase", self.firebase)
        self.patcher.start()
        self.service = UserService()

    def tearDown(self):
        self.patcher.stop()

    def test_log_activity_stores_actor_snapshot(self):
        self.service.log_activity("admin-1", "Creación de usuario", target_uid="teacher-1")

        activity = next(iter(self.firebase.documents[COLLECTION_USER_ACTIVITY].values()))

        self.assertEqual(activity["usuario"], "admin-1")
        self.assertEqual(activity["actor_nombre"], "Administrador")
        self.assertEqual(activity["actor_email"], "admin@emotionlab.test")
        self.assertEqual(activity["accion"], "Creación de usuario")
        self.assertEqual(activity["target_uid"], "teacher-1")

    def test_list_activity_enriches_legacy_records(self):
        self.firebase.create_document(
            COLLECTION_USER_ACTIVITY,
            "activity-1",
            {
                "fecha": "SERVER_TIMESTAMP",
                "usuario": "admin-1",
                "accion": "Inicio de sesión",
                "target_uid": "admin-1",
            },
        )

        activities = self.service.list_activity()

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["actor_nombre"], "Administrador")
        self.assertEqual(activities[0]["actor_email"], "admin@emotionlab.test")
        self.assertEqual(activities[0]["accion"], "Inicio de sesión")
