"""Servicio de integracion con Amazon Cognito.

Gestiona:
- Creacion de usuarios especialistas (con contrasena temporal)
- Login y desafio NEW_PASSWORD_REQUIRED
- Recuperacion de contrasena (forgot/confirm)
- Habilitacion/deshabilitacion de usuarios
- Verificacion de tokens RS256 emitidos por Cognito
"""

from __future__ import annotations

import os
import logging
from typing import Any, Mapping, Optional

import boto3
from botocore.exceptions import ClientError
from jose import jwt as jose_jwt
from jose.backends.base import Key
from jose.utils import base64url_decode

from core.exceptions import (
    AuthenticationError,
    InvalidCredentials,
    UserNotFound,
)


logger = logging.getLogger(__name__)


class CognitoService:
    """Adaptador del User Pool de Cognito."""

    _instance: Optional["CognitoService"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        self.region = region
        self.user_pool_id = os.getenv("COGNITO_USER_POOL_ID", "").strip()
        self.app_client_id = os.getenv("COGNITO_APP_CLIENT_ID", "").strip()

        if not self.user_pool_id or not self.app_client_id:
            raise RuntimeError(
                "COGNITO_USER_POOL_ID y COGNITO_APP_CLIENT_ID son variables obligatorias"
            )

        self.idp = boto3.client("cognito-idp", region_name=region)
        self._jwks: list[dict[str, Any]] = self._fetch_jwks()

        self._initialized = True

    # ------------------------------------------------------------------
    # JWKS para verificacion de tokens
    # ------------------------------------------------------------------
    def _fetch_jwks(self) -> list[dict[str, Any]]:
        import urllib.request
        import json

        url = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}/.well-known/jwks.json"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("keys", [])
        except Exception as err:
            raise RuntimeError(
                f"No se pudo descargar JWKS de Cognito ({url}): {err}"
            ) from err

    def _get_key(self, kid: str) -> Optional[Key]:
        import json
        from jose.backends import RSAKey

        for key in self._jwks:
            if key.get("kid") == kid:
                public_key = RSAKey(key, algorithm=key["alg"])
                return public_key
        return None

    # ------------------------------------------------------------------
    # Verificacion de tokens
    # ------------------------------------------------------------------
    def verify_token(self, token: str, token_use: Optional[str] = None) -> dict[str, Any]:
        """Verifica un Access Token o ID Token de Cognito. Retorna claims."""
        if not isinstance(token, str) or not token:
            raise AuthenticationError("El token es obligatorio")

        try:
            unverified_header = jose_jwt.get_unverified_header(token)
        except jose_jwt.JWTError as err:
            raise AuthenticationError("Token invalido o corrupto") from err

        if unverified_header.get("alg") != "RS256":
            raise AuthenticationError("Cognito solo admite tokens firmados con RS256")

        kid = unverified_header.get("kid")
        if not kid:
            raise AuthenticationError("Token no contiene kid")

        public_key = self._get_key(kid)
        if not public_key:
            raise AuthenticationError("Clave publica Cognito no encontrada para kid")

        issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"
        try:
            claims = jose_jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                issuer=issuer,
                options={"verify_at_hash": False, "verify_aud": False},
            )
        except jose_jwt.ExpiredSignatureError:
            raise AuthenticationError("Token expirado")
        except jose_jwt.JWTError as err:
            raise AuthenticationError(f"Token invalido: {err}") from err

        if token_use and claims.get("token_use") != token_use:
            raise AuthenticationError(f"Token use no es {token_use}")

        # Cognito ID tokens carry the app client in ``aud``; access tokens
        # carry it in ``client_id`` and normally have no ``aud`` claim.
        # Validate the appropriate claim explicitly after signature/issuer/
        # expiry validation, rather than rejecting every access token.
        actual_token_use = claims.get("token_use")
        if actual_token_use == "id":
            client_matches = claims.get("aud") == self.app_client_id
        elif actual_token_use == "access":
            client_matches = claims.get("client_id") == self.app_client_id
        else:
            raise AuthenticationError("Token Cognito debe ser de tipo access o id")

        if not client_matches:
            raise AuthenticationError("Token Cognito no pertenece al App Client configurado")

        return claims

    @staticmethod
    def claims_to_user(claims: Mapping[str, Any]) -> dict[str, Any]:
        """Convierte claims Cognito al formato de usuario usado por EmotionLab.

        Mapea:
            sub   -> uid (nuevo UID oficial de la app)
            email -> email
            name  -> nombre
            custom:rol -> rol
        """
        return {
            "uid": claims.get("sub"),
            "email": claims.get("email"),
            "nombre": claims.get("name") or claims.get("username"),
            "rol": claims.get("custom:rol") or claims.get("custom_rol") or "",
            "activo": True,
        }

    # ------------------------------------------------------------------
    # Administracion de usuarios
    # ------------------------------------------------------------------
    def admin_create_user(
        self,
        email: str,
        nombre: str,
        rol: str,
        legacy_uid: Optional[str] = None,
    ) -> dict[str, Any]:
        """Crea un usuario y deja que Cognito genere y envie la invitacion.

        Retorna diccionario con:
            cognito_username, uid (sub), email, nombre, rol y estado.
        """
        if not email or not nombre or not rol:
            raise ValueError("email, nombre y rol son obligatorios")

        user_attributes = [
            {"Name": "email", "Value": email.lower()},
            {"Name": "name", "Value": nombre},
            {"Name": "email_verified", "Value": "true"},
        ]
        if legacy_uid:
            user_attributes.append({"Name": "custom:legacy_uid", "Value": legacy_uid})

        username = email.lower()
        logger.info("[COGNITO] Creating user with email=%s", username)
        try:
            resp = self.idp.admin_create_user(
                UserPoolId=self.user_pool_id,
                Username=username,
                UserAttributes=user_attributes,
                DesiredDeliveryMediums=["EMAIL"],
            )
        except self.idp.exceptions.UsernameExistsException:
            raise ValueError("Ya existe un usuario con ese correo")

        user = resp.get("User", {})
        attributes = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
        logger.info(
            "[COGNITO] AdminCreateUser completed email=%s status=%s",
            username,
            user.get("UserStatus"),
        )
        return {
            "cognito_username": user.get("Username"),
            "uid": attributes.get("sub", user.get("Username")),
            "email": username,
            "nombre": nombre,
            "rol": rol,
            "enabled": user.get("Enabled", True),
            "user_status": user.get("UserStatus"),
        }

    def admin_update_user_attributes(
        self,
        username: str,
        attributes: Mapping[str, str],
    ) -> None:
        """Actualiza atributos de un usuario (name, custom:rol, etc)."""
        cognito_attrs = [
            {"Name": k, "Value": str(v)} for k, v in attributes.items()
        ]
        try:
            self.idp.admin_update_user_attributes(
                UserPoolId=self.user_pool_id,
                Username=username,
                UserAttributes=cognito_attrs,
            )
        except self.idp.exceptions.UserNotFoundException as err:
            raise UserNotFound("Usuario no existe en Cognito") from err

    def admin_disable_user(self, username: str) -> None:
        try:
            self.idp.admin_disable_user(
                UserPoolId=self.user_pool_id,
                Username=username,
            )
        except self.idp.exceptions.UserNotFoundException as err:
            raise UserNotFound("Usuario no existe") from err

    def admin_enable_user(self, username: str) -> None:
        try:
            self.idp.admin_enable_user(
                UserPoolId=self.user_pool_id,
                Username=username,
            )
        except self.idp.exceptions.UserNotFoundException as err:
            raise UserNotFound("Usuario no existe") from err

    def admin_delete_user(self, username: str) -> None:
        try:
            self.idp.admin_delete_user(
                UserPoolId=self.user_pool_id,
                Username=username,
            )
        except self.idp.exceptions.UserNotFoundException as err:
            raise UserNotFound("Usuario no existe") from err

    def admin_get_user(self, username: str) -> dict[str, Any]:
        try:
            resp = self.idp.admin_get_user(
                UserPoolId=self.user_pool_id,
                Username=username,
            )
        except self.idp.exceptions.UserNotFoundException as err:
            raise UserNotFound("Usuario no existe") from err

        attributes = {a["Name"]: a["Value"] for a in resp.get("UserAttributes", [])}
        return {
            "username": resp.get("Username"),
            "enabled": resp.get("Enabled"),
            "user_status": resp.get("UserStatus"),
            "email": attributes.get("email"),
            "nombre": attributes.get("name"),
            "rol": attributes.get("custom:rol"),
            "uid": attributes.get("sub", resp.get("Username")),
            "legacy_uid": attributes.get("custom:legacy_uid"),
            "email_verified": attributes.get("email_verified") == "true",
        }

    def list_users(self, limit: int = 60) -> list[dict[str, Any]]:
        """Lista usuarios del pool (paginado simple hasta 60 usuarios)."""
        users: list[dict[str, Any]] = []
        next_token: Optional[str] = None
        remaining = limit
        while True:
            kwargs: dict[str, Any] = {"UserPoolId": self.user_pool_id, "Limit": min(remaining, 60)}
            if next_token:
                kwargs["PaginationToken"] = next_token
            resp = self.idp.list_users(**kwargs)
            for user in resp.get("Users", []):
                attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
                users.append({
                    "uid": attrs.get("sub", user.get("Username")),
                    "username": user.get("Username"),
                    "email": attrs.get("email"),
                    "nombre": attrs.get("name"),
                    "rol": attrs.get("custom:rol"),
                    "legacy_uid": attrs.get("custom:legacy_uid"),
                    "enabled": user.get("Enabled"),
                    "user_status": user.get("UserStatus"),
                    "created_at": str(user.get("UserCreateDate")),
                    "last_modified": str(user.get("UserLastModifiedDate")),
                })
            next_token = resp.get("PaginationToken")
            remaining = limit - len(users)
            if not next_token or remaining <= 0:
                break
        return users

    # ------------------------------------------------------------------
    # Autenticacion
    # ------------------------------------------------------------------
    def login(self, email: str, password: str) -> dict[str, Any]:
        """Inicia sesion con email + password.

        Retorna:
         - Challenge NEW_PASSWORD_REQUIRED si el usuario debe cambiar la clave.
         - Tokens (access, id, refresh) si el login es OK.
        """
        if not email or not password:
            raise InvalidCredentials("email y password son obligatorios")

        try:
            resp = self.idp.admin_initiate_auth(
                AuthFlow="ADMIN_NO_SRP_AUTH",
                ClientId=self.app_client_id,
                UserPoolId=self.user_pool_id,
                AuthParameters={
                    "USERNAME": email.lower(),
                    "PASSWORD": password,
                },
            )
        except self.idp.exceptions.NotAuthorizedException as err:
            raise InvalidCredentials("Correo o contrasena incorrectos") from err
        except self.idp.exceptions.UserNotFoundException as err:
            raise InvalidCredentials("Correo o contrasena incorrectos") from err
        except self.idp.exceptions.UserNotConfirmedException as err:
            raise AuthenticationError("Usuario no confirmado") from err
        except self.idp.exceptions.InvalidPasswordException as err:
            raise AuthenticationError("Contrasena invalida o temporal expirada") from err

        if resp.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
            return {
                "challenge": "NEW_PASSWORD_REQUIRED",
                "session": resp.get("Session"),
                "user_id_for_srp": resp.get("ChallengeParameters", {}).get("USER_ID_FOR_SRP"),
                "required_attributes": resp.get("ChallengeParameters", {}).get("requiredAttributes"),
            }

        auth_result = resp.get("AuthenticationResult", {})
        return {
            "access_token": auth_result.get("AccessToken"),
            "id_token": auth_result.get("IdToken"),
            "refresh_token": auth_result.get("RefreshToken"),
            "expires_in": auth_result.get("ExpiresIn"),
            "token_type": auth_result.get("TokenType"),
        }

    def respond_new_password_required(
        self,
        session: str,
        username: str,
        new_password: str,
    ) -> dict[str, Any]:
        """Responde al desafio NEW_PASSWORD_REQUIRED."""
        if not session or not username or not new_password:
            raise ValueError("session, username y new_password son obligatorios")
        if len(new_password) < 8:
            raise ValueError("La nueva contrasena debe tener al menos 8 caracteres")

        try:
            resp = self.idp.admin_respond_to_auth_challenge(
                Session=session,
                ChallengeName="NEW_PASSWORD_REQUIRED",
                ClientId=self.app_client_id,
                UserPoolId=self.user_pool_id,
                ChallengeResponses={
                    "USERNAME": username,
                    "NEW_PASSWORD": new_password,
                },
            )
        except self.idp.exceptions.InvalidPasswordException as err:
            raise ValueError(f"Contrasena no cumple los requisitos: {err}") from err
        except self.idp.exceptions.ExpiredCodeException as err:
            raise AuthenticationError("Sesion expirada; vuelve a iniciar sesion") from err

        if resp.get("ChallengeName"):
            return {
                "challenge": resp["ChallengeName"],
                "session": resp.get("Session"),
            }

        auth_result = resp.get("AuthenticationResult", {})
        return {
            "access_token": auth_result.get("AccessToken"),
            "id_token": auth_result.get("IdToken"),
            "refresh_token": auth_result.get("RefreshToken"),
            "expires_in": auth_result.get("ExpiresIn"),
            "token_type": auth_result.get("TokenType"),
        }

    def respond_to_new_password_challenge(
        self,
        *,
        session: str,
        user_id_for_srp: str,
        new_password: str,
    ) -> dict[str, Any]:
        """Responde al desafio NEW_PASSWORD_REQUIRED con la interfaz que
        AuthService espera (usando ``admin_respond_to_auth_challenge``).

        El flujo ``AdminInitiateAuth`` (ADMIN_NO_SRP_AUTH) retorna una
        ``Session`` y un ``USER_ID_FOR_SRP`` en ``ChallengeParameters``.
        Para resolver el challenge hay que usar
        ``AdminRespondToAuthChallenge`` (NO el respond_to_auth_challenge
        publico, ya que la sesion fue creada por el API admin).
        """
        if not session:
            raise ValueError("session es obligatorio")
        if not user_id_for_srp:
            raise ValueError("user_id_for_srp es obligatorio")
        if not isinstance(new_password, str) or len(new_password) < 8:
            raise ValueError("new_password debe tener al menos 8 caracteres")

        username = user_id_for_srp
        try:
            resp = self.idp.admin_respond_to_auth_challenge(
                UserPoolId=self.user_pool_id,
                ClientId=self.app_client_id,
                ChallengeName="NEW_PASSWORD_REQUIRED",
                Session=session,
                ChallengeResponses={
                    "USERNAME": username,
                    "NEW_PASSWORD": new_password,
                },
            )
        except self.idp.exceptions.InvalidPasswordException as err:
            raise ValueError(
                f"La nueva contrasena no cumple los requisitos de Cognito: {err}"
            ) from err
        except self.idp.exceptions.ExpiredCodeException as err:
            raise AuthenticationError(
                "La sesion de cambio de contrasena expiro; vuelve a iniciar sesion"
            ) from err
        except self.idp.exceptions.CodeMismatchException as err:
            raise AuthenticationError(
                "Sesion invalida para este challenge; vuelve a iniciar sesion"
            ) from err
        except self.idp.exceptions.NotAuthorizedException as err:
            raise AuthenticationError(
                "No autorizado para completar este challenge"
            ) from err
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", str(err))
            msg = err.response.get("Error", {}).get("Message", str(err))
            raise AuthenticationError(f"{code}: {msg}") from err

        if resp.get("ChallengeName"):
            return {
                "challenge": resp["ChallengeName"],
                "session": resp.get("Session"),
            }

        auth_result = resp.get("AuthenticationResult", {})
        return {
            "access_token": auth_result.get("AccessToken"),
            "id_token": auth_result.get("IdToken"),
            "refresh_token": auth_result.get("RefreshToken"),
            "expires_in": auth_result.get("ExpiresIn"),
            "token_type": auth_result.get("TokenType"),
        }

    # ------------------------------------------------------------------
    # Recuperacion de contrasena
    # ------------------------------------------------------------------
    def forgot_password(self, email: str) -> None:
        """Solicita el envio del codigo de 6 digitos por correo."""
        try:
            self.idp.forgot_password(
                ClientId=self.app_client_id,
                Username=email.lower(),
            )
        except (
            self.idp.exceptions.UserNotFoundException,
            self.idp.exceptions.InvalidParameterException,
        ):
            return

    def confirm_forgot_password(
        self,
        email: str,
        code: str,
        new_password: str,
    ) -> None:
        """Confirma el codigo de recuperacion y actualiza la contrasena."""
        if not email or not code or not new_password:
            raise ValueError("email, code y new_password son obligatorios")
        if len(new_password) < 8:
            raise ValueError("La nueva contrasena debe tener al menos 8 caracteres")
        try:
            self.idp.confirm_forgot_password(
                ClientId=self.app_client_id,
                Username=email.lower(),
                ConfirmationCode=code,
                Password=new_password,
            )
        except self.idp.exceptions.CodeMismatchException as err:
            raise ValueError("Codigo incorrecto") from err
        except self.idp.exceptions.ExpiredCodeException as err:
            raise ValueError("Codigo expirado; solicita uno nuevo") from err
        except self.idp.exceptions.InvalidPasswordException as err:
            raise ValueError(f"Contrasena no cumple los requisitos: {err}") from err
        except self.idp.exceptions.UserNotFoundException as err:
            raise ValueError("Usuario no encontrado") from err

    # ------------------------------------------------------------------
    # Cambio de contrasena (cuando el usuario ya esta logueado)
    # ------------------------------------------------------------------
    def change_password(
        self,
        access_token: str,
        old_password: str,
        new_password: str,
    ) -> None:
        if not access_token or not old_password or not new_password:
            raise ValueError("access_token, old_password y new_password son obligatorios")
        if len(new_password) < 8:
            raise ValueError("La nueva contrasena debe tener al menos 8 caracteres")
        try:
            self.idp.change_password(
                AccessToken=access_token,
                PreviousPassword=old_password,
                ProposedPassword=new_password,
            )
        except self.idp.exceptions.NotAuthorizedException as err:
            raise InvalidCredentials("Contrasena actual incorrecta o token invalido") from err
        except self.idp.exceptions.InvalidPasswordException as err:
            raise ValueError(f"Contrasena no cumple los requisitos: {err}") from err
