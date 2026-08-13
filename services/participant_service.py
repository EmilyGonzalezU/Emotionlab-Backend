"""Administracion de participantes y su relacion con sesiones."""

from typing import TYPE_CHECKING, Optional

from core.constants import COLLECTION_PARTICIPANTS
from core.exceptions import ParticipantNotFound

if TYPE_CHECKING:
    from services.firebase_service import FirebaseService


class ParticipantService:
    """Encapsula los documentos de participantes en Firestore."""

    def __init__(
        self,
        firebase_service: Optional["FirebaseService"] = None,
    ):
        if firebase_service is None:
            from services.firebase_service import firebase

            firebase_service = firebase
        self.firebase = firebase_service
        self.collection = COLLECTION_PARTICIPANTS

    def create_participant(
        self,
        participant_id: str,
        nombre: str,
        *,
        observaciones: Optional[str] = None,
    ) -> dict:
        """Crea un participante sin duplicar la lista de sesiones."""
        if not participant_id or not nombre.strip():
            raise ValueError("participant_id y nombre son obligatorios")
        data = {
            "participant_id": participant_id,
            "nombre": nombre.strip(),
            "observaciones": observaciones,
            "created_at": self.firebase.server_timestamp(),
            "updated_at": self.firebase.server_timestamp(),
        }
        self.firebase.create_document(self.collection, participant_id, data)
        return data

    def update_observations(self, participant_id: str, observaciones: Optional[str]) -> None:
        """Actualiza exclusivamente las observaciones del participante."""
        self._require_participant(participant_id)
        if observaciones is not None and not isinstance(observaciones, str):
            raise ValueError("observaciones debe ser texto o null")
        self.firebase.update_document(
            self.collection,
            participant_id,
            {"observaciones": observaciones, "updated_at": self.firebase.server_timestamp()},
        )

    def get_participant(self, participant_id: str) -> Optional[dict]:
        return self.firebase.get_document(self.collection, participant_id)

    def search_by_name(self, nombre: str) -> list[dict]:
        """Busca por coincidencia parcial, sin depender de mayusculas."""
        query = nombre.strip().casefold()
        if not query:
            return []
        return [
            participant
            for participant in self.list_participants()
            if query in participant.get("nombre", "").casefold()
        ]

    def list_participants(self) -> list[dict]:
        return self.firebase.list_documents(self.collection)

    def _require_participant(self, participant_id: str) -> dict:
        participant = self.get_participant(participant_id)
        if not participant:
            raise ParticipantNotFound("El participante no existe")
        return participant
