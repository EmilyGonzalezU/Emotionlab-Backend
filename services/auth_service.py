"""Servicio de autenticacion de usuarios.

Coordina los servicios de usuarios y criptografia; no accede a Firebase de
forma directa ni implementa operaciones CRUD.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import jwt

from core.exceptions import AuthenticationError, InvalidCredentials, UserNotFound
from core.jwt_config import JWT_ALGORITHM, JWT_EXPIRATION, JWT_SECRET
from services.crypto_service import CryptoService

if TYPE_CHECKING:
    from services.user_service import UserService


class AuthService:
    """Autentica usuarios activos y emite tokens JWT."""

    def __init__(
        self,
        user_service: Optional["UserService"] = None,
        crypto_service: Optional[CryptoService] = None,
        jwt_secret: Optional[str] = None,
    ):
        if user_service is None:
            # UserService inicializa Firebase; se difiere hasta que sea necesario.
            from services.user_service import UserService

            user_service = UserService()
        self.user_service = user_service
        self.crypto_service = crypto_service or CryptoService()
        self.jwt_secret = jwt_secret if jwt_secret is not None else JWT_SECRET

    def login(self, email: str, password: str) -> dict:
        """Valida credenciales, registra el acceso y devuelve usuario y token."""
        user = self.user_service.get_by_email(email)
        if not user:
            raise InvalidCredentials("Correo o contraseña incorrectos.")

        password_hash = user.get("password_hash")
        if not password_hash or not self.crypto_service.verify_password(password, password_hash):
            raise InvalidCredentials("Correo o contraseña incorrectos.")

        if not user.get("activo", False):
            raise AuthenticationError("El usuario esta inactivo")

        uid = user.get("uid") or user.get("id")
        if not uid:
            raise AuthenticationError("El usuario no tiene un identificador valido")

        self.user_service.update_last_login(uid)
        self.user_service.log_activity(uid, "Inicio de sesión", target_uid=uid)
        return {
            "token": self.generate_token(user),
            "user": self._public_user(user),
        }

    def generate_token(self, user: dict) -> str:
        """Genera un JWT con la identidad y el rol del usuario."""
        if not self.jwt_secret:
            raise RuntimeError("La variable de entorno JWT_SECRET es obligatoria")

        uid = user.get("uid") or user.get("id")
        if not uid:
            raise AuthenticationError("El usuario no tiene un identificador valido")

        now = datetime.now(timezone.utc)
        payload = {
            "sub": uid,
            "email": user.get("email"),
            "rol": user.get("rol"),
            "iat": now,
            "exp": now + JWT_EXPIRATION,
        }
        return jwt.encode(payload, self.jwt_secret, algorithm=JWT_ALGORITHM)

    def verify_token(self, token: str) -> dict:
        """Verifica firma y expiracion de un JWT y devuelve sus claims."""
        if not isinstance(token, str) or not token:
            raise AuthenticationError("El token es obligatorio")

        try:
            return jwt.decode(token, self.jwt_secret, algorithms=[JWT_ALGORITHM])
        except jwt.PyJWTError as error:
            raise AuthenticationError("Token invalido o expirado") from error

    def get_current_user(self, token: str) -> dict:
        """Obtiene el usuario activo asociado al JWT verificado."""
        uid = self.verify_token(token).get("sub")
        if not uid:
            raise AuthenticationError("El token no contiene el identificador de usuario")

        user = self.user_service.get_user(uid)
        if not user:
            raise UserNotFound("El usuario del token no existe")
        if not user.get("activo", False):
            raise AuthenticationError("El usuario esta inactivo")
        return self._public_user(user)

    @staticmethod
    def _public_user(user: dict) -> dict:
        """Elimina credenciales antes de incluir al usuario en una respuesta."""
        private_fields = {"password", "password_hash"}
        return {key: value for key, value in user.items() if key not in private_fields}
