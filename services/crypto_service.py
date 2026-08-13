"""Operaciones criptograficas reutilizables de la aplicacion."""

import os
from typing import Union

from cryptography.fernet import Fernet, InvalidToken
from passlib.context import CryptContext

from core.security import FERNET_KEY_ENV


class CryptoService:
    """Hashea contrasenas y cifra datos sensibles con Fernet.

    La clave de Fernet se lee de ``FERNET_KEY`` solo al cifrar o descifrar.
    Esto permite usar los metodos de contrasenas sin necesitar esa clave.
    """

    _password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def __init__(self, fernet_key: Union[str, bytes, None] = None):
        self._fernet_key = fernet_key
        self._fernet = None

    def hash_password(self, password: str) -> str:
        """Devuelve un hash bcrypt seguro para una contrasena."""
        self._validate_secret(password, "password")
        return self._password_context.hash(password)

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Indica si una contrasena coincide con su hash almacenado."""
        self._validate_secret(plain_password, "plain_password")
        self._validate_secret(hashed_password, "hashed_password")
        try:
            return self._password_context.verify(plain_password, hashed_password)
        except (ValueError, TypeError):
            # Un hash corrupto o de un esquema no admitido no autentica a nadie.
            return False

    def encrypt(self, value: str) -> str:
        """Cifra texto y devuelve un token Fernet serializable."""
        self._validate_secret(value, "value")
        return self._get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: Union[str, bytes]) -> str:
        """Descifra un token Fernet; lanza ``InvalidToken`` si no es valido."""
        if not isinstance(token, (str, bytes)) or not token:
            raise ValueError("token must be a non-empty str or bytes")
        encoded_token = token.encode("utf-8") if isinstance(token, str) else token
        return self._get_fernet().decrypt(encoded_token).decode("utf-8")

    def _get_fernet(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet

        key = self._fernet_key or os.getenv(FERNET_KEY_ENV)
        if not key:
            raise RuntimeError(
                f"La variable de entorno {FERNET_KEY_ENV} es obligatoria para cifrar datos."
            )
        key_as_bytes = key.encode("utf-8") if isinstance(key, str) else key
        self._fernet = Fernet(key_as_bytes)
        return self._fernet

    @staticmethod
    def _validate_secret(value: str, name: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
