"""Servicio de autenticacion de usuarios.

Coordina los servicios de usuarios y criptografia; no accede a Firebase de
forma directa ni implementa operaciones CRUD.
"""

from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Optional

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

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

    @staticmethod
    def _password_version(user: dict) -> str:
        """
        Calcula una "version" estable para contraseñas.

        Se usa para embeber en el JWT de recuperación y evitar que:
        - Un mismo token se pueda reutilizar DESPUÉS de haber cambiado la clave.
        - Un token generado ANTES de un cambio de contraseña siga siendo válido.

        Si el usuario no tiene password_hash (raro), retorna un string fijo.
        """
        ph = user.get("password_hash") or ""
        if not ph:
            return "no-password"
        # Tomamos los primeros 16 chars del hash bcrypt como versión (no es
        # sensible por sí solo y ya está guardado en Firestore).
        return ph[:16] if isinstance(ph, str) else str(ph)[:16]

    def forgot_password(self, email: str) -> dict:
        try:
            user = self.user_service.get_by_email(email)
            if user:
                uid = user.get("uid") or user.get("id")
                now = datetime.now(timezone.utc)
                password_version = self._password_version(user)
                payload = {
                    "sub": uid,
                    "email": email,
                    "type": "password_reset",
                    "pw_ver": password_version,
                    "iat": now,
                    "exp": now + timedelta(minutes=15),
                }

                reset_token = jwt.encode(payload, self.jwt_secret, algorithm=JWT_ALGORITHM)
                reset_link = f"{FRONTEND_URL}/#/reset-password?token={reset_token}"
                correo_enviado = self.enviar_correo(email, reset_link)
                
                if correo_enviado:
                    print(f"[AUTH] Correo de recuperación enviado a {email}")
                else:
                    print(f"[AUTH] Falló envío de correo de recuperación a {email}")
            
        except Exception as e:
            # Nunca exponemos detalles al cliente (enumeración de usuarios)
            print(f"[AUTH] Error en forgot_password (interno)")

        return {"mensaje": "Si el correo existe en nuestro sistema, te enviamos las instrucciones."}

    def reset_password(self, token: str, new_password: str) -> dict:
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[JWT_ALGORITHM])
            if payload.get("type") != "password_reset":
                raise ValueError("El enlace no es válido para esta acción.")

            uid = payload.get("sub")
            if not uid:
                raise ValueError("El enlace es inválido.")

            user = self.user_service.get_user(uid)
            if not user:
                raise ValueError("El usuario no existe.")

            # =============================================================
            # INVALIDACIÓN DE TOKEN POR USO / CAMBIO PREVIO
            # =============================================================
            # Comparamos la "version" del password embebida en el token con
            # la actual. Si el password YA FUE CAMBIADO desde que se generó
            # el token, el token deja de ser válido automáticamente.
            # Esto evita reutilización múltiple del mismo enlace.
            token_pw_ver = payload.get("pw_ver")
            current_pw_ver = self._password_version(user)
            if token_pw_ver is None or token_pw_ver != current_pw_ver:
                raise ValueError("El enlace ya fue utilizado o expiró. Solicita uno nuevo.")

            old_password_hash = user.get("password_hash")
            if old_password_hash and self.crypto_service.verify_password(new_password, old_password_hash):
                raise ValueError("La nueva contraseña no puede ser igual a la anterior.")

            if not isinstance(new_password, str) or len(new_password) < 8:
                raise ValueError("La nueva contraseña debe tener al menos 8 caracteres.")

            hashed_password = self.crypto_service.hash_password(new_password)
            self.user_service.update_password(uid, hashed_password)

            return {"mensaje": "Contraseña actualizada exitosamente."}

        except jwt.ExpiredSignatureError:
            raise ValueError("El enlace de recuperación ya expiró. Solicita uno nuevo.")
        except jwt.InvalidTokenError:
            raise ValueError("El enlace de recuperación es inválido o está corrupto.")
        except ValueError as ve:
            raise ve
        except Exception as e:
            print(f"[AUTH] Error en reset_password (interno)")
            raise ValueError("No se pudo actualizar la contraseña. Intenta de nuevo.")

    def enviar_correo(self, destinatario: str, link: str) -> bool:
        remitente = os.getenv("EMAIL_SENDER", "correo_por_defecto@gmail.com")
        password = os.getenv("EMAIL_PASSWORD", "")
        
        msg = MIMEMultipart()
        msg['From'] = f"Soporte EmotionLab <{remitente}>" 
        msg['To'] = destinatario 
        msg['Subject'] = "Recuperación de Contraseña - EmotionLab"
        
        cuerpo = f"""Hola,
        
Hemos recibido una solicitud para recuperar tu contraseña.
Haz clic en el siguiente enlace para crear una nueva:

{link}

Si no fuiste tú, puedes ignorar este mensaje. El enlace caduca en 15 minutos.

IMPORTANTE: El link debes abrirlo en el mismo dispositivo donde se encuentra el programa. TELÉFONOS CELULARES NO.
"""
        msg.attach(MIMEText(cuerpo, 'plain'))
        
        try:
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(remitente, password)
            server.send_message(msg)
            server.quit()
            return True
        except Exception as e:
            print(f"Error enviando correo: {e}")
            return False
