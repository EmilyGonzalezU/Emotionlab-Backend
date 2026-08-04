import unittest
from copy import deepcopy

from core.exceptions import ParticipantNotFound
from services.participant_service import ParticipantService


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


class ParticipantServiceTests(unittest.TestCase):
    def setUp(self):
        self.firebase = FakeFirebase()
        self.participants = ParticipantService(self.firebase)

    def test_create_search_and_update_observations(self):
        self.participants.create_participant("participant-1", "Ana López")
        self.participants.update_observations("participant-1", "Seguimiento inicial")

        self.assertEqual(
            [item["participant_id"] for item in self.participants.search_by_name("lópez")],
            ["participant-1"],
        )
        self.assertEqual(
            self.participants.get_participant("participant-1")["observaciones"],
            "Seguimiento inicial",
        )

    def test_update_observations_requires_existing_participant(self):
        with self.assertRaises(ParticipantNotFound):
            self.participants.update_observations("missing", "Seguimiento inicial")
