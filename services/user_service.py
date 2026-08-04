"""Servicio encargado de administrar los usuarios del sistema."""

from typing import List, Optional

from core.constants import COLLECTION_USERS
from core.roles import ROLE_ADMIN, ROLE_TEACHER, VALID_ROLES
from services.firebase_service import firebase


class UserService:
    """Encapsula el acceso a los documentos de usuarios en Firestore."""

    def __init__(self):
        self.firebase = firebase
        self.collection = COLLECTION_USERS

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
            # Fallback defensivo: evita exponer el sentinel de server_timestamp.
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

    def list_teachers(self) -> List[dict]:
        return self.firebase.query_documents(
            collection=self.collection,
            field="rol",
            operator="==",
            value=ROLE_TEACHER,
        )

    def list_users(self) -> List[dict]:
        """Lista todos los usuarios, incluidos administradores y profesores."""
        return self.firebase.list_documents(self.collection)

    def update_user(self, uid: str, data: dict) -> None:
        self.firebase.update_document(self.collection, uid, data)

    def update_last_login(self, uid: str) -> None:
        self.update_user(uid, {"ultimo_login": self.firebase.server_timestamp()})

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

    def list_active_teachers(self) -> List[dict]:
        return [user for user in self.list_teachers() if user.get("activo", False)]

    # Compatibilidad temporal para llamadas existentes del dominio de profesores.
    def create_teacher(self, uid: str, nombre: str, email: str) -> dict:
        return self.create_user(uid, nombre, email, ROLE_TEACHER)

    def update_teacher(self, uid: str, data: dict) -> None:
        self.update_user(uid, data)

    def disable_teacher(self, uid: str) -> None:
        self.disable_user(uid)

    def enable_teacher(self, uid: str) -> None:
        self.enable_user(uid)

    def delete_teacher(self, uid: str) -> None:
        self.delete_user(uid)
