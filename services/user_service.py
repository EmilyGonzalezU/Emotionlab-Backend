"""Servicio encargado de administrar los usuarios del sistema."""

from typing import List, Optional
from uuid import uuid4

from core.constants import COLLECTION_USER_ACTIVITY, COLLECTION_USERS
from core.roles import ROLE_ADMIN, ROLE_TEACHER, VALID_ROLES
from services.firebase_service import firebase


class UserService:
    """Encapsula el acceso a los documentos de usuarios en Firestore."""

    def __init__(self):
        self.firebase = firebase
        self.collection = COLLECTION_USERS
        self.activity_collection = COLLECTION_USER_ACTIVITY

    def create_user(
        self,
        uid: str,
        nombre: str,
        email: str,
        rol: str,
        password_hash: Optional[str] = None,
    ) -> dict:
        """Crea un usuario; ``password_hash`` debe venir de ``CryptoService``."""
        if rol not in VALID_ROLES:
            raise ValueError(f"Rol no valido: {rol}")

        data = {
            "uid": uid,
            "nombre": nombre,
            "email": email.lower(),
            "rol": rol,
            "activo": True,
            "created_at": self.firebase.server_timestamp(),
        }

        if password_hash is not None:
            data["password_hash"] = password_hash

        self.firebase.create_document(self.collection, uid, data)

        persisted = self.firebase.get_document(self.collection, uid)

        if persisted is None:
            safe = dict(data)
            safe.pop("created_at", None)
            return safe

        return persisted

    def get_user(self, uid: str) -> Optional[dict]:
        return self.firebase.get_document(self.collection, uid)

    def get_by_email(self, email: str) -> Optional[dict]:
        users = self.firebase.query_documents(
            collection=self.collection,
            field="email",
            operator="==",
            value=email.lower(),
        )
        return users[0] if users else None

    def list_users(self) -> List[dict]:
        """Lista todos los usuarios."""
        return self.firebase.list_documents(self.collection)

    @staticmethod
    def public_user(user: dict) -> dict:
        """Elimina credenciales antes de exponer un usuario."""
        private_fields = {"password", "password_hash"}
        return {
            key: value
            for key, value in user.items()
            if key not in private_fields
        }

    def list_public_users(self) -> List[dict]:
        return [self.public_user(user) for user in self.list_users()]

    def list_teachers(self) -> List[dict]:
        """Lista únicamente los visualizadores."""
        return self.firebase.query_documents(
            collection=self.collection,
            field="rol",
            operator="==",
            value=ROLE_TEACHER,
        )

    def list_active_teachers(self) -> List[dict]:
        return [
            user
            for user in self.list_teachers()
            if user.get("activo", False)
        ]

    def update_user(self, uid: str, data: dict) -> None:
        """Actualiza datos generales del usuario."""
        self.firebase.update_document(self.collection, uid, data)

    def update_password(self, uid: str, password_hash: str) -> None:
        """Actualiza únicamente el hash de la contraseña."""
        self.firebase.update_document(
            self.collection,
            uid,
            {"password_hash": password_hash},
        )

    def log_activity(
        self,
        actor_uid: str,
        action: str,
        target_uid: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> None:
        actor = self.get_user(actor_uid) or {}
        data = {
            "fecha": self.firebase.server_timestamp(),
            "usuario": actor_uid,
            "accion": action,
            "actor_nombre": actor.get("nombre", ""),
            "actor_email": actor.get("email", ""),
        }

        if target_uid is not None:
            data["target_uid"] = target_uid
        if details:
            data["detalles"] = details

        self.firebase.create_document(
            self.activity_collection,
            uuid4().hex,
            data,
        )

    def list_activity(
        self,
            uid: Optional[str] = None,
            limit: Optional[int] = None,
        ) -> List[dict]:
            allowed_actions = {
                "Creación de usuario",
                "Actualización de usuario",
                "Cuenta eliminada",
            }

            activities = [
                activity
                for activity in self.firebase.list_documents(self.activity_collection)
                if activity.get("accion") in allowed_actions
            ]

            if uid is not None:
                activities = [
                    activity
                    for activity in activities
                    if activity.get("usuario") == uid
                    or activity.get("target_uid") == uid
                ]

            if limit is not None:
                activities = activities[:limit]

            return [self._enrich_activity(activity) for activity in activities]
    
    def _enrich_activity(self, activity: dict) -> dict:
        enriched = dict(activity)
        actor_uid = enriched.get("usuario")

        if actor_uid:
            actor = self.get_user(actor_uid) or {}
            enriched.setdefault("actor_nombre", actor.get("nombre", ""))
            enriched.setdefault("actor_email", actor.get("email", ""))

        enriched.setdefault("actor_nombre", "")
        enriched.setdefault("actor_email", "")
        return enriched

    def update_last_login(self, uid: str) -> None:
        self.update_user(
            uid,
            {"ultimo_login": self.firebase.server_timestamp()},
        )

    def disable_user(self, uid: str) -> None:
        self.update_user(uid, {"activo": False})

    def enable_user(self, uid: str) -> None:
        self.update_user(uid, {"activo": True})

    def delete_user(self, uid: str) -> None:
        self.firebase.delete_document(self.collection, uid)

    def exists(self, uid: str) -> bool:
        return self.get_user(uid) is not None

    def is_admin(self, uid: str) -> bool:
        user = self.get_user(uid)
        return bool(user and user.get("rol") == ROLE_ADMIN)

    def is_teacher(self, uid: str) -> bool:
        user = self.get_user(uid)
        return bool(user and user.get("rol") == ROLE_TEACHER)

    def is_active(self, uid: str) -> bool:
        user = self.get_user(uid)
        return bool(user and user.get("activo", False))