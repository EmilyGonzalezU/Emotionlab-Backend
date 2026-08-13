"""Servicio encargado de administrar los usuarios del sistema."""

from collections.abc import Mapping
from typing import List, Optional
from uuid import uuid4

from core.constants import COLLECTION_USER_ACTIVITY, COLLECTION_USERS
from core.roles import ROLE_ADMIN, ROLE_TEACHER, VALID_ROLES
from services.cache_service import USER_CACHE, invalidate_user_cache
from services.firebase_service import firebase


class UserService:
    """Encapsula el acceso a los documentos de usuarios en Firestore."""

    def __init__(self):
        self.firebase = firebase
        self.collection = COLLECTION_USERS
        self.activity_collection = COLLECTION_USER_ACTIVITY

        
    def generate_password_reset_link(self, email: str) -> str:
        """Genera un link de recuperación usando Firebase Auth."""
        from firebase_admin import auth
        return auth.generate_password_reset_link(email)

    # =========================================================
    # USUARIOS
    # =========================================================

    def create_user(
        self,
        uid: str,
        nombre: str,
        email: str,
        rol: str,
        password_hash: Optional[str] = None,
    ) -> dict:
        """Crea un usuario y retorna su representación pública sin re-leer Firestore."""

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

        self.firebase.create_document(
            self.collection,
            uid,
            data,
        )
        invalidate_user_cache(uid)

        return self.public_user(data)

    def get_user(self, uid: str) -> Optional[dict]:
        """Devuelve un usuario por uid, con caché TTL para reducir lecturas."""
        cache_key = ("user", uid)
        cached = USER_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)
        user = self.firebase.get_document(self.collection, uid)
        if user is not None:
            USER_CACHE.set(cache_key, dict(user))
            return user
        return None

    def get_by_email(self, email: str) -> Optional[dict]:
        users = self.firebase.query_documents(
            collection=self.collection,
            field="email",
            operator="==",
            value=email.lower(),
        )
        if not users:
            return None
        user = users[0]
        uid = user.get("uid")
        if uid:
            USER_CACHE.set(("user", uid), dict(user))
        return user

    def list_users(self) -> List[dict]:
        """Lista todos los usuarios."""
        return self.firebase.list_documents(
            self.collection
        )

    @staticmethod
    def public_user(user: dict) -> dict:
        """Elimina credenciales antes de exponer un usuario y sanitiza fechas."""

        private_fields = {
            "password",
            "password_hash",
        }

        safe_user = {
            key: value
            for key, value in user.items()
            if key not in private_fields
        }

        if "created_at" in safe_user:
            safe_user["created_at"] = str(safe_user["created_at"])

        return safe_user

    def list_public_users(self) -> List[dict]:
        return [
            self.public_user(user)
            for user in self.list_users()
        ]

    def list_teachers(self) -> List[dict]:
        """Lista únicamente los teachers."""

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

    def update_user(
        self,
        uid: str,
        data: dict,
    ) -> None:
        """Actualiza datos generales del usuario e invalida su caché."""

        self.firebase.update_document(
            self.collection,
            uid,
            data,
        )
        invalidate_user_cache(uid)

    def update_password(
        self,
        uid: str,
        password_hash: str,
    ) -> None:
        """Actualiza únicamente el hash de la contraseña e invalida caché."""

        self.firebase.update_document(
            self.collection,
            uid,
            {
                "password_hash": password_hash,
            },
        )
        invalidate_user_cache(uid)

    def disable_user(self, uid: str) -> None:
        self.update_user(
            uid,
            {
                "activo": False,
            },
        )

    def enable_user(self, uid: str) -> None:
        self.update_user(
            uid,
            {
                "activo": True,
            },
        )

    def delete_user(self, uid: str) -> None:
        self.firebase.delete_document(
            self.collection,
            uid,
        )
        invalidate_user_cache(uid)

    def exists(self, uid: str) -> bool:
        return self.get_user(uid) is not None

    def is_admin(self, uid: str) -> bool:
        user = self.get_user(uid)

        return bool(
            user
            and user.get("rol") == ROLE_ADMIN
        )

    def is_teacher(self, uid: str) -> bool:
        user = self.get_user(uid)

        return bool(
            user
            and user.get("rol") == ROLE_TEACHER
        )

    def is_active(self, uid: str) -> bool:
        user = self.get_user(uid)

        return bool(
            user
            and user.get("activo", False)
        )

    def update_last_login(self, uid: str) -> None:
        self.update_user(
            uid,
            {
                "ultimo_login": self.firebase.server_timestamp()
            },
        )

    # =========================================================
    # ACTIVIDAD ADMINISTRATIVA
    # =========================================================

    @staticmethod
    def _allowed_activity_actions() -> set[str]:
        """
        Acciones que pueden aparecer en el historial administrativo.

        IMPORTANTE:
        Solo estas tres acciones se almacenan en Firestore.
        """

        return {
            "Creación de usuario",
            "Actualización de usuario",
            "Cuenta eliminada",
        }

    def log_activity(
        self,
        actor_uid: str,
        action: str,
        target_uid: Optional[str] = None,
        details: Optional[dict] = None,
        *,
        actor_info: Optional[dict] = None,
    ) -> None:
        """
        Registra una actividad administrativa.

        ``actor_info`` (opcional) = diccionario con el usuario ya cargado
        (``rol``, ``nombre``, ``email``). Si se provee se evita una lectura
        Firestore. De lo contrario se consulta al actor.
        """

        allowed_actions = self._allowed_activity_actions()

        if action not in allowed_actions:
            return

        # -----------------------------------------------------
        # Obtener info del actor (preferentemente precargada)
        # -----------------------------------------------------

        actor = actor_info if isinstance(actor_info, Mapping) else (self.get_user(actor_uid) or {})

        if actor.get("rol") != ROLE_ADMIN:
            return

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
        """
        Lista únicamente actividades administrativas válidas.

        Elimina el patrón N+1: ``actor_nombre`` y ``actor_email`` ya
        se persistieron al crear el log, por lo que no se vuelven a
        consultar los usuarios en Firestore.
        """

        allowed_actions = self._allowed_activity_actions()

        activities = [
            activity
            for activity in self.firebase.list_documents(
                self.activity_collection
            )
            if activity.get("accion") in allowed_actions
        ]

        if uid is not None:
            activities = [
                activity
                for activity in activities
                if (
                    activity.get("usuario") == uid
                    or activity.get("target_uid") == uid
                )
            ]

        if limit is not None:
            activities = activities[:limit]

        return [
            self._enrich_activity(activity)
            for activity in activities
        ]

    def _enrich_activity(
        self,
        activity: dict,
    ) -> dict:
        """
        Asegura campos de presentación.

        Los valores de ``actor_nombre`` y ``actor_email`` se grabaron
        originalmente en el momento del log y son la fuente oficial.
        Para registros históricos sin estos campos se usan valores
        por defecto para no disparar consultas N+1.
        """

        enriched = dict(activity)

        if not isinstance(enriched.get("actor_nombre"), str):
            enriched["actor_nombre"] = ""
        if not isinstance(enriched.get("actor_email"), str):
            enriched["actor_email"] = ""

        return enriched