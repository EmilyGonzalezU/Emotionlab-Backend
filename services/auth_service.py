"""Servicio de autenticacion de usuarios.

Coordina los servicios de usuarios y criptografia; no accede a Firebase de
forma directa ni implementa operaciones CRUD.
"""

from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Optional

import os
import resend  # <-- necesario instalar, es la api

import jwt

from core.exceptions import AuthenticationError, InvalidCredentials, UserNotFound
from core.jwt_config import JWT_ALGORITHM, JWT_EXPIRATION, JWT_SECRET
from services.crypto_service import CryptoService

if TYPE_CHECKING:
    from services.user_service import UserService

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

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
        self.user_service.log_activity(
            uid,
            "Inicio de sesión",
            target_uid=uid,
            actor_info=user,
        )
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
        claims = self.verify_token(token)
        uid = claims.get("sub")
        if not uid:
            raise AuthenticationError("El token no contiene el identificador de usuario")

        user = self.user_service.get_user(uid)
        if not user:
            raise UserNotFound("El usuario del token no existe")
        if not user.get("activo", False):
            raise AuthenticationError("El usuario esta inactivo")
        public_user = self._public_user(user)
        public_user["uid"] = uid
        return public_user

    @staticmethod
    def _public_user(user: dict) -> dict:
        """Elimina credenciales antes de incluir al usuario en una respuesta."""
        public_user = dict(user)
        if not public_user.get("uid") and public_user.get("id"):
            public_user["uid"] = public_user["id"]
        private_fields = {"password", "password_hash"}
        return {
            key: value
            for key, value in public_user.items()
            if key not in private_fields
        }

    def forgot_password(self, email: str) -> dict:
        try:
            user = self.user_service.get_by_email(email)
            if user:
                uid = user.get("uid") or user.get("id")
                now = datetime.now(timezone.utc)
                payload = {
                    "sub": uid,
                    "email": email,
                    "type": "password_reset",
                    "iat": now,
                    "exp": now + timedelta(minutes=15)
                }

                reset_token = jwt.encode(payload, self.jwt_secret, algorithm=JWT_ALGORITHM)
                reset_link = f"{FRONTEND_URL}/#/reset-password?token={reset_token}"
                correo_enviado = self.enviar_correo(email, reset_link)
                
                if correo_enviado:
                    print(f"¡Correo enviado exitosamente a {email}!")
                else:
                    print(f"Falló el envío al correo {email}.")
            
        except Exception as e:
            print(f"Error interno: {e}")
            
        return {"mensaje": "Si el correo existe en nuestro sistema, te enviamos las instrucciones."}

    def reset_password(self, token: str, new_password: str) -> dict:
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[JWT_ALGORITHM])
            if payload.get("type") != "password_reset":
                raise ValueError("El enlace no es válido para esta acción.")
            
            uid = payload.get("sub")
            user = self.user_service.get_user(uid)
            if not user:
                raise ValueError("El usuario no existe.")

            old_password_hash = user.get("password_hash")
            if old_password_hash and self.crypto_service.verify_password(new_password, old_password_hash):
                raise ValueError("La nueva contraseña no puede ser igual a la anterior.")

            hashed_password = self.crypto_service.hash_password(new_password)
            self.user_service.update_user(uid, {"password_hash": hashed_password})
            
            return {"mensaje": "Contraseña actualizada exitosamente."}

        except jwt.ExpiredSignatureError:
            raise ValueError("El enlace de recuperación ya expiró. Solicita uno nuevo.")
        except jwt.InvalidTokenError:
            raise ValueError("El enlace de recuperación es inválido o está corrupto.")
        except ValueError as ve:
            raise ve
        except Exception as e:
            raise ValueError("No se pudo actualizar la contraseña. Intenta de nuevo.")

    def enviar_correo(self, destinatario: str, link: str) -> bool:
        resend.api_key = "re_Uj3iA4do_5EwqZytZkD39RcPoi8J24cPs"
        
        print(f"Enviando correo a: {destinatario}")
        
        try:
            # Resend usa este correo por defecto para cuentas gratis
            respuesta = resend.Emails.send({
                "from": "Soporte EmotionLab <onboarding@resend.dev>",
                "to": destinatario,
                "subject": "Recuperación de Contraseña - EmotionLab",
                "html": f"""
                    <div style="font-family: Arial, sans-serif; padding: 20px; color: #17315E;">
                        <h2>EmotionLab</h2>
                        <p>Hola,</p>
                        <p>Hemos recibido una solicitud para recuperar tu contraseña. Haz clic en el botón de abajo para crear una nueva (caduca en 15 minutos):</p>
                        
                        <a href="{link}" style="background-color: #17315E; color: white; padding: 12px 20px; text-decoration: none; border-radius: 5px; display: inline-block; margin-top: 15px; margin-bottom: 15px;">
                            Restablecer mi contraseña
                        </a>
                        
                        <p style="margin-top: 20px; font-size: 13px; color: #d9534f; font-weight: bold;">
                            IMPORTANTE: El link debes abrirlo en el mismo dispositivo donde se encuentra el programa. TELÉFONOS CELULARES NO.
                        </p>
                        
                        <p style="margin-top: 20px; font-size: 11px; color: #666;">
                            Si no solicitaste esto, puedes ignorar este mensaje.
                        </p>
                    </div>
                """
            })
            print("¡Correo enviado exitosamente!")
            return True

        except Exception as e:
            print(f"Error enviando correo: {str(e)}")
            return False