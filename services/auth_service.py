"""AutenticaciÃ³n mediante Amazon Cognito (ETAPA 2).

La autenticación oficial se gestiona mediante Amazon Cognito.
Ahora:
  * Amazon Cognito como fuente de verdad para auth (passwords, activaciÃ³n, flows).
  * Verify token: Cognito RS256 via JWKS PÃšBLICO (python-jose), con FALLBACK al
    legacy JWT HS256 (JWT_SECRET) durante la migracion gradual del frontend.
  * Flujos: login (NEW_PASSWORD_REQUIRED challenge), forgot/reset password
    (codigo 6 digitos Cognito/SES), confirm-new-password tras challenge.
  * Correos: SESService (welcome temp password, forgot code, etc.).
  * NUNCA guardamos contraseÃ±as en DynamoDB.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Mapping, Optional, TYPE_CHECKING

import jwt as _legacy_jwt  # fallback legacy HS256 JWT_SECRET durante migracion

from core.exceptions import AuthenticationError, InvalidCredentials, UserNotFound
from core.jwt_config import JWT_ALGORITHM as _LEGACY_JWT_ALG, JWT_EXPIRATION, JWT_SECRET
from core.roles import normalize_role
from services.crypto_service import CryptoService

from services.cognito_service import CognitoService
from services.user_service import UserService
from services.dynamodb_service import DynamoDBService

try:
    from services.audit_service import AuditService, audit_service
except Exception:  # pragma: no cover
    AuditService = None  # type: ignore
    def audit_service(): return None  # type: ignore

try:
    from services.ses_service import SESService, ses_service as _ses_default
except Exception:  # pragma: no cover
    SESService = None  # type: ignore
    def _ses_default(): return None  # type: ignore

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")


class AuthService:
    """Autentica usuarios activos (Cognito) y valida tokens (doble RS256 + HS256)."""

    def __init__(
        self,
        user_service: Optional["UserService"] = None,
        cognito_service: Optional["CognitoService"] = None,
        crypto_service: Optional["CryptoService"] = None,
        jwt_secret: Optional[str] = None,
        audit_svc: Optional["AuditService"] = None,  # type: ignore
        ses_svc: Optional["SESService"] = None,
    ):
        if user_service is None:
            from services.user_service import UserService
            try:
                user_service = UserService()
            except Exception:
                user_service = None
        self.user_service: UserService = user_service  # type: ignore[assignment]

        if cognito_service is not None:
            self.cognito: Optional["CognitoService"] = cognito_service
        else:
            try:
                self.cognito = CognitoService()
            except Exception:
                self.cognito = None

        try:
            self.crypto_service = crypto_service or CryptoService()
        except Exception:
            self.crypto_service = None

        self.jwt_secret = jwt_secret if jwt_secret is not None else JWT_SECRET

        if audit_svc is not None:
            self.audit = audit_svc
        else:
            try:
                self.audit = audit_service()
            except Exception:
                self.audit = None

        if ses_svc is not None:
            self.ses = ses_svc
        else:
            try:
                self.ses = _ses_default()
            except Exception:
                self.ses = None

    # ==========================================================
    # LOGIN â€” Cognito ADMIN_NO_SRP_AUTH + NEW_PASSWORD_REQUIRED challenge
    # ==========================================================

    def login(self, email: str, password: str) -> dict:
        """Valida credenciales en Cognito. Retorna tokens o challenge NEW_PASSWORD_REQUIRED.

        Casos:
        * Login OK â†’ {"token": "<access_token|legacy_jwt>", "refresh_token": "...",
                      "id_token": "...", "token_type": "cognito", "user": {...}}
        * NEW_PASSWORD_REQUIRED challenge â†’ {"challenge": "NEW_PASSWORD_REQUIRED",
                          "session": "...", "user_id_for_srp": "...",
                          "user_metadata": {"email":..., "nombre":...,...}}
        """
        if not isinstance(email, str) or not email.strip():
            raise InvalidCredentials("Correo o contraseÃ±a incorrectos.")
        email = email.strip().lower()

        cognito_result = self.cognito.login(email, password)
        challenge_name = cognito_result.get("challenge")

        # ==========================================================
        # CASO A: Primer login, obligado cambio contraseÃ±a
        # ==========================================================
        if challenge_name == "NEW_PASSWORD_REQUIRED":
            if self.audit:
                try:
                    self.audit.log(
                        "NEW_PASSWORD_REQUIRED",
                        actor_email=email,
                        extra={"flow": "first_login_temporary_password"},
                    )
                except Exception:
                    pass
            # Intentamos enriquecer con metadata DynamoDB/Cognito
            user_meta: dict[str, Any] = {"email": email}
            try:
                u = self.user_service.get_by_email(email)
                if u:
                    user_meta.update({
                        "nombre": u.get("nombre"),
                        "rol": u.get("rol"),
                        "uid": u.get("uid"),
                    })
            except Exception:
                pass
            return {
                "challenge": "NEW_PASSWORD_REQUIRED",
                "session": cognito_result.get("session"),
                "user_id_for_srp": cognito_result.get("user_id_for_srp"),
                "user_metadata": user_meta,
            }

        # ==========================================================
        # CASO B: Login normal exitoso â†’ tokens Cognito + user publico
        # ==========================================================
        access_token = cognito_result.get("access_token")
        id_token = cognito_result.get("id_token")
        refresh_token = cognito_result.get("refresh_token")
        if not access_token:
            if self.audit:
                try:
                    self.audit.log_login_fail(email, reason="cognito_no_access_token")
                except Exception:
                    pass
            raise InvalidCredentials("Correo o contraseÃ±a incorrectos.")

        # Verificar claims Cognito y obtener usuario local DynamoDB
        try:
            claims = self.cognito.verify_token(id_token or access_token)
        except Exception as exc:
            if self.audit:
                try:
                    self.audit.log_login_fail(email, reason=f"verify_fail:{exc}")
                except Exception:
                    pass
            raise InvalidCredentials("Correo o contraseÃ±a incorrectos.") from exc

        cognito_sub = claims.get("sub") or claims.get("username")
        if not cognito_sub:
            raise AuthenticationError("Token Cognito no contiene sub claim")
        user = self.user_service.get_user(cognito_sub)
        if not user:
            # Cognito authentication alone never grants an application role.
            # Accounts must be provisioned by an administrator into both
            # Cognito and DynamoDB; do not silently promote unknown users.
            raise AuthenticationError(
                "Usuario autenticado en Cognito pero no aprovisionado en EmotionLab"
            )

        if not user.get("activo", False):
            if self.audit:
                try:
                    self.audit.log_login_fail(email, reason="usuario_inactivo")
                except Exception:
                    pass
            raise AuthenticationError("El usuario esta inactivo")

        user_role = normalize_role(user.get("rol"))
        if user_role is None:
            raise AuthenticationError("El usuario tiene un rol desconocido")

        uid = user.get("uid") or cognito_sub

        try:
            self.user_service.update_last_login(uid)
        except Exception:
            pass

        actor_rol = user_role
        if self.audit:
            try:
                self.audit.log_login_ok(
                    actor_uid=uid, actor_email=email, actor_rol=actor_rol,
                )
            except Exception:
                pass
        try:
            self.user_service.log_activity(
                uid, "Inicio de sesiÃ³n", target_uid=uid, actor_info=user,
            )
        except Exception:
            pass

        # ==========================================================
        # Compatibilidad con frontend legacy que espera token unico.
        # Entregamos Cognito tokens + un token legacy HS256 transicional.
        # ==========================================================
        try:
            legacy_token = self.generate_token(user)
        except Exception:
            legacy_token = None

        response: dict[str, Any] = {
            "user": {
                **self._public_user(user),
                "rol": user_role,
            },
            "token": access_token,
            "token_type": "cognito",
            "access_token": access_token,
            "id_token": id_token,
            "refresh_token": refresh_token,
        }
        if legacy_token:
            response["legacy_token"] = legacy_token
            response["all_tokens"] = {
                "cognito_access": access_token,
                "cognito_id": id_token,
                "cognito_refresh": refresh_token,
                "legacy_hs256": legacy_token,
            }
        return response

    # ==========================================================
    # CONFIRMAR NUEVA CONTRASEÃ‘A TRAS CHALLENGE NEW_PASSWORD_REQUIRED
    # ==========================================================

    def confirm_new_password(
        self,
        *,
        session: str,
        user_id_for_srp: str,
        new_password: str,
        email: Optional[str] = None,
    ) -> dict:
        """Resuelve el challenge NEW_PASSWORD_REQUIRED (primer login)."""
        if not session or not user_id_for_srp or not new_password:
            raise ValueError("session, user_id_for_srp y new_password obligatorios")
        if not isinstance(new_password, str) or len(new_password) < 8:
            raise ValueError("La nueva contraseÃ±a debe tener al menos 8 caracteres.")

        cognito_result = self.cognito.respond_to_new_password_challenge(
            session=session,
            user_id_for_srp=user_id_for_srp,
            new_password=new_password,
        )
        challenge = cognito_result.get("challenge")
        if challenge == "NEW_PASSWORD_REQUIRED":
            # Todavia requiere otra ronda? No, no deberia pasar.
            raise AuthenticationError(
                "No fue posible completar el cambio de contraseÃ±a. Intenta nuevamente."
            )

        # Ahora deberiamos tener tokens.
        access_token = cognito_result.get("access_token")
        id_token = cognito_result.get("id_token")
        refresh_token = cognito_result.get("refresh_token")
        if not access_token:
            raise AuthenticationError(
                "Cambio de contraseÃ±a OK pero Cognito no retornÃ³ tokens."
            )

        try:
            claims = self.cognito.verify_token(id_token or access_token)
        except Exception as exc:
            raise AuthenticationError("Tokens post cambio contraseÃ±a invÃ¡lidos.") from exc
        cognito_sub = claims.get("sub")
        if not cognito_sub:
            raise AuthenticationError(
                "El token post cambio de contraseÃ±a no contiene sub.")

        user = self.user_service.get_user(cognito_sub)
        if not user and email:
            user = self.user_service.get_by_email(email)

        if not user:
            raise AuthenticationError(
                "Usuario autenticado en Cognito no encontrado en DynamoDB.")

        role = normalize_role(user.get("rol"))
        if role is None:
            raise AuthenticationError(
                "El usuario autenticado tiene un rol desconocido.")

        uid = user.get("uid") or cognito_sub

        # Sincronizar estado de Cognito en DynamoDB después de completar
        # correctamente el challenge NEW_PASSWORD_REQUIRED.
        try:
            self.user_service.dynamo.update_item(
                self.user_service.table,
                {"uid": uid},
                {
                    "user_status": "CONFIRMED",
                    "updated_at": DynamoDBService.now_iso(),
                },
            )
        except Exception as exc:
            print(
                f"[AuthService] No se pudo sincronizar user_status en DynamoDB: {exc}",
                flush=True,
            )

        try:
            self.user_service.update_last_login(uid)
        except Exception:
            pass

        actor_email = user.get("email") or email or claims.get("email", "")
        actor_rol = role
        if self.audit and uid:
            try:
                self.audit.log_login_ok(uid, actor_email, actor_rol,
                    extra={"after_challenge": "NEW_PASSWORD_REQUIRED"})
                self.audit.log_password_changed(uid, actor_email, actor_rol)
            except Exception:
                pass
        if user and uid:
            try:
                self.user_service.log_activity(
                    uid, "Cambio de contraseÃ±a inicial", target_uid=uid,
                    actor_info=user,
                )
            except Exception:
                pass
        if self.ses and actor_email:
            try:
                self.ses.send_password_changed_confirmation(to_email=actor_email)
            except Exception:
                pass

        response: dict[str, Any] = {
            "token": access_token,
            "token_type": "cognito",
            "access_token": access_token,
            "id_token": id_token,
            "refresh_token": refresh_token,
            "user": {
                "uid": uid,
                "email": user.get("email") or actor_email,
                "rol": role,
                "nombre": user.get("nombre"),
            },
        }
        try:
            response["legacy_token"] = self.generate_token(user)
        except Exception:
            pass
        return response

    # ==========================================================
    # TOKENS: legacy HS256 generator + doble verification RS256/HS256
    # ==========================================================

    def generate_token(self, user: dict) -> str:
        """Genera JWT legacy HS256 (compatibilidad frontend durante migracion)."""
        if not self.jwt_secret:
            raise RuntimeError("JWT_SECRET no configurado para token legacy")
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
            "token_source": "legacy_hs256",
        }
        return _legacy_jwt.encode(payload, self.jwt_secret, algorithm=_LEGACY_JWT_ALG)

    def verify_token(self, token: str) -> dict:
        """Verifica un token. Doble pipeline:
        1. Intentar Cognito RS256 via JWKS publico.
        2. Fallback: legacy HS256 JWT_SECRET (durante migracion).
        Normaliza claims a: sub, email, rol, token_source.
        """
        if not isinstance(token, str) or not token:
            raise AuthenticationError("El token es obligatorio")

        try:
            header = _legacy_jwt.get_unverified_header(token)
        except _legacy_jwt.PyJWTError as error:
            raise AuthenticationError("Token invalido o corrupto") from error

        algorithm = header.get("alg")

        # Los tokens legacy se verifican exclusivamente con JWT_SECRET.
        if algorithm == _LEGACY_JWT_ALG:
            try:
                if not self.jwt_secret:
                    raise AuthenticationError("JWT_SECRET no configurado")
                legacy = _legacy_jwt.decode(
                    token,
                    self.jwt_secret,
                    algorithms=[_LEGACY_JWT_ALG],
                )
                legacy.setdefault("token_source", "legacy_hs256")
                return legacy
            except _legacy_jwt.PyJWTError as error:
                raise AuthenticationError("Token legacy invalido o expirado") from error

        if algorithm != "RS256":
            raise AuthenticationError("Algoritmo JWT no permitido")

        # Paso 1: Cognito RS256
        try:
            # HTTP Authorization must carry a Cognito access token; ID tokens
            # are accepted only during the login exchange that validates them
            # directly through CognitoService.
            claims = self.cognito.verify_token(token, token_use="access")
            cognito_sub = claims.get("sub") or claims.get("username")
            cognito_email = claims.get("email") or claims.get("username")
            cognito_rol = (
                claims.get("custom:rol")
                or claims.get("cognito:groups", [None])[0]
                or ""
            )
            norm = {
                "sub": cognito_sub,
                "email": cognito_email,
                "rol": cognito_rol,
                "token_source": "cognito_rs256",
            }
            # Pasamos claims originales por si algÃºn middleware los necesita.
            norm["_raw_cognito_claims"] = claims
            return norm
        except AuthenticationError:
            raise
        except Exception as exc_1:
            raise AuthenticationError(
                f"Token Cognito invalido: {type(exc_1).__name__}"
            ) from exc_1

    def get_current_user(self, token: str) -> dict:
        """Obtiene el usuario desde DynamoDB basado en token verificado."""
        claims = self.verify_token(token)
        uid = claims.get("sub")
        if not uid:
            raise AuthenticationError("El token no contiene el identificador de usuario")
        user = self.user_service.get_user(uid)
        if not user:
            # Fallback: buscar por email si el claim lo trae
            email = claims.get("email")
            if email:
                user = self.user_service.get_by_email(email)
        if not user:
            raise UserNotFound("El usuario del token no existe")
        if not user.get("activo", False):
            raise AuthenticationError("El usuario esta inactivo")
        public_user = self._public_user(user)
        public_user.setdefault("uid", uid)
        role = normalize_role(public_user.get("rol"))
        if role is None:
            raise AuthenticationError("El usuario tiene un rol desconocido")
        public_user["rol"] = role
        return public_user

    # ==========================================================
    # FORGOT / RESET PASSWORD via Cognito ForgotPassword API (codigo 6 digitos)
    # ==========================================================

    def forgot_password(self, email: str) -> dict:
        """Solicita cÃ³digo 6 dÃ­gitos de recuperaciÃ³n vÃ­a Cognito + SES.

        Nunca exponemos si el email existe (prevenir enumeraciÃ³n).
        """
        email_ok = email.strip().lower() if isinstance(email, str) else ""
        sent_ok = False
        if email_ok:
            try:
                self.cognito.forgot_password(email_ok)
                sent_ok = True
                if self.audit:
                    try:
                        self.audit.log_password_reset(email_ok)
                    except Exception:
                        pass
            except Exception as exc:
                # Cognito UserNotFoundException: no propagar (prevenir enumeraciÃ³n)
                print(f"[AUTH] forgot_password intern error: {exc}")
        return {
            "mensaje": (
                "Si el correo existe en nuestro sistema, te enviamos un cÃ³digo "
                "de 6 dÃ­gitos. Ãšsalo para crear una nueva contraseÃ±a."
            ),
            "method": "code_6_digits",
            "code_length": 6,
        }

    def reset_password(
        self,
        *args,
        token: Optional[str] = None,
        code: Optional[str] = None,
        email: Optional[str] = None,
        new_password: Optional[str] = None,
        **kwargs,
    ) -> dict:
        """Restablece contraseÃ±a. Soporte DUAL:

        Modo legacy (token JWT): reset_password(token, new_password)
        Modo Cognito (codigo 6 dÃ­gitos): reset_password(email=..., code=..., new_password=...)
        """
        # Normalizar argumentos. Signature legacy: reset_password(token, new_password).
        if args and len(args) >= 2:
            token = args[0]
            new_password = args[1]
        elif args and len(args) == 1 and token is None:
            token = args[0]

        if not new_password:
            raise ValueError("new_password es obligatorio")
        if not isinstance(new_password, str) or len(new_password) < 8:
            raise ValueError("La nueva contraseÃ±a debe tener al menos 8 caracteres.")

        # ==========================================================
        # MODO 1: Cognito con codigo 6 digitos (preferido ETAPA 2)
        # ==========================================================
        if email and code:
            email_ok = email.strip().lower()
            code_str = str(code).strip()
            try:
                self.cognito.confirm_forgot_password(
                    email_ok, code_str, new_password,
                )
            except Exception as exc:
                msg = str(exc).lower()
                if "code" in msg and ("mismatch" in msg or "expired" in msg or "invalid" in msg):
                    raise ValueError(
                        "CÃ³digo de recuperaciÃ³n invÃ¡lido o expirado. "
                        "Solicita uno nuevo."
                    ) from exc
                if "password" in msg and "not" in msg and ("match" in msg or "require" in msg):
                    raise ValueError(str(exc)) from exc
                raise ValueError(
                    "No se pudo actualizar la contraseÃ±a. Intenta de nuevo."
                ) from exc
            # Auditoria + correo confirmacion
            if self.audit:
                try:
                    user = self.user_service.get_by_email(email_ok)
                    if user:
                        self.audit.log_password_changed(
                            actor_uid=user.get("uid", ""),
                            actor_email=email_ok,
                            actor_rol=user.get("rol", ""),
                            extra={"via": "forgot_code"},
                        )
                except Exception:
                    pass
            if self.ses:
                try:
                    self.ses.send_password_changed_confirmation(to_email=email_ok)
                except Exception:
                    pass
            return {"mensaje": "ContraseÃ±a actualizada exitosamente."}

        # ==========================================================
        # MODO 2: Legacy JWT token (transicional mientras frontend migra)
        # ==========================================================
        if not token:
            raise ValueError("Se requiere email+code o bien token de recuperaciÃ³n legacy")
        try:
            payload = _legacy_jwt.decode(token, self.jwt_secret, algorithms=[_LEGACY_JWT_ALG])
            if payload.get("type") != "password_reset":
                raise ValueError("El enlace no es vÃ¡lido para esta acciÃ³n.")
            uid = payload.get("sub")
            if not uid:
                raise ValueError("El enlace es invÃ¡lido.")
            user = self.user_service.get_user(uid)
            if not user:
                raise ValueError("El usuario no existe.")
            # Cambio de contraseÃ±a se debe realizar EN COGNITO, no en DynamoDB.
            email_from_user = user.get("email")
            if email_from_user:
                # Utilizamos flujo admin_set_user_password en Cognito PERMANENTE.
                try:
                    self.cognito.admin_set_user_password(
                        email_from_user, new_password, permanent=True,
                    )
                except Exception as exc:
                    raise ValueError(
                        f"No se pudo actualizar la contraseÃ±a en Cognito: {exc}"
                    ) from exc
            if self.ses and email_from_user:
                try:
                    self.ses.send_password_changed_confirmation(to_email=email_from_user)
                except Exception:
                    pass
            return {"mensaje": "ContraseÃ±a actualizada exitosamente."}
        except _legacy_jwt.ExpiredSignatureError:
            raise ValueError(
                "El enlace de recuperaciÃ³n ya expirÃ³. Solicita uno nuevo."
            )
        except _legacy_jwt.InvalidTokenError:
            raise ValueError(
                "El enlace de recuperaciÃ³n es invÃ¡lido o estÃ¡ corrupto."
            )
        except ValueError as ve:
            raise ve
        except Exception:
            raise ValueError("No se pudo actualizar la contraseÃ±a. Intenta de nuevo.")

    # ==========================================================
    # CAMBIO CONTRASEÃ‘A AUTENTICADO (usuario logueado)
    # ==========================================================

    def change_password(
        self,
        access_token: str,
        old_password: str,
        new_password: str,
    ) -> dict:
        """Cambio de contraseÃ±a estando autenticado (API Cognito ChangePassword)."""
        if not access_token or not old_password or not new_password:
            raise ValueError("Todos los campos son obligatorios")
        if not isinstance(new_password, str) or len(new_password) < 8:
            raise ValueError("La nueva contraseÃ±a debe tener al menos 8 caracteres.")
        try:
            self.cognito.change_password(access_token, old_password, new_password)
        except Exception as exc:
            msg = str(exc).lower()
            if "incorrect" in msg and "password" in msg:
                raise ValueError("ContraseÃ±a actual incorrecta.") from exc
            if "password" in msg and "not" in msg and ("match" in msg or "require" in msg):
                raise ValueError(str(exc)) from exc
            raise
        return {"mensaje": "ContraseÃ±a actualizada exitosamente."}

    # ==========================================================
    # HELPERS (igual a legacy)
    # ==========================================================

    @staticmethod
    def _public_user(user: Mapping[str, Any]) -> dict:
        """Elimina credenciales antes de retornar un usuario en respuesta API."""
        public_user = dict(user)
        if not public_user.get("uid") and public_user.get("id"):
            public_user["uid"] = public_user["id"]
        private_fields = {
            "password", "password_hash", "temporary_password",
            "legacy_password_hash",
        }
        return {
            k: v for k, v in public_user.items() if k not in private_fields
        }

    # ==========================================================
    # CORREOS â€” delegamos a SESService (NO Resend hardcoded key)
    # ==========================================================

    def send_welcome_temporary_password(
        self,
        *,
        to_email: str,
        nombre_usuario: str,
        temporary_password: str,
        rol: Optional[str] = None,
    ) -> bool:
        """EnvÃ­a correo de bienvenida con contraseÃ±a temporal generada por Cognito."""
        if self.ses is None:
            print(f"[AUTH][WARN] SESService no disponible; no se envÃ­a bienvenida a {to_email}")
            return False
        try:
            self.ses.send_welcome_with_temp_password(
                to_email=to_email,
                nombre=nombre_usuario,
                temp_password=temporary_password,
                rol=rol,
            )
            return True
        except Exception as exc:
            print(f"[AUTH][ERROR] send_welcome_temporary_password fallÃ³: {exc}")
            return False
    def enviar_correo(self, destinatario: str, link: str) -> bool:
        """Wrapper legacy (mantenido para compatibilidad). Delega a SES si puede."""
        if self.ses is None:
            # Fallback: imprimir en logs (nunca usar Resend).
            print(f"[AUTH][LEGACY] enviar_correo() sin SES; link no enviado a {destinatario}")
            return False
        try:
            self.ses.send_custom_html(
                to_email=destinatario,
                subject="RecuperaciÃ³n de ContraseÃ±a - EmotionLab",
                html_body=f"""
                <div style="font-family: Arial, sans-serif; padding: 20px; color: #17315E;">
                    <h2>EmotionLab</h2>
                    <p>Hola,</p>
                    <p>Hemos recibido una solicitud para recuperar tu contraseÃ±a:</p>
                    <a href="{link}" style="background-color:#17315E;color:white;padding:12px 20px;
                        text-decoration:none;border-radius:5px;display:inline-block;margin:15px 0;">
                        Restablecer mi contraseÃ±a
                    </a>
                    <p style="font-size:11px;color:#666;margin-top:20px;">
                        Si no solicitaste esto, ignora este mensaje.
                    </p>
                </div>
                """,
            )
            return True
        except Exception as exc:
            print(f"[AUTH][ERROR] enviar_correo() fallÃ³ SES: {exc}")
            return False
