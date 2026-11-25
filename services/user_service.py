"""Servicio de usuarios sobre DynamoDB + Cognito.

ETAPA 2:
- Cognito es fuente de verdad para autenticacion, passwords, activacion.
- DynamoDB emotionlab-users almacena metadata adicional: ultimo_login,
  relaciones, sincronizacion de perfil.
- Los passwords NO se guardan en DynamoDB.
"""

from __future__ import annotations

import re
from typing import List, Mapping, Optional

from boto3.dynamodb.conditions import Key

from core.constants import (
    DYNAMODB_TABLE_USERS,
    PK_TYPE_USER,
)
from core.roles import ROLE_ADMIN, ROLE_TEACHER, normalize_role
from services.cache_service import (
    ACTIVITY_CACHE,
    USER_CACHE,
    invalidate_activity_cache,
    invalidate_user_cache,
)
from services.dynamodb_service import DynamoDBService, dynamodb
from services.cognito_service import CognitoService


class UserService:
    """Encapsula operaciones de usuarios en DynamoDB + Cognito."""

    def __init__(
        self,
        dynamodb_service: Optional[DynamoDBService] = None,
        cognito_service: Optional[CognitoService] = None,
        audit_svc=None,
    ):
        self.dynamo: DynamoDBService = dynamodb_service or dynamodb
        self.table = DYNAMODB_TABLE_USERS
        self.audit = audit_svc
        try:
            self.cognito: CognitoService = cognito_service or CognitoService()
        except Exception:
            self.cognito = None  # solo se exige cuando se usa Cognito

    # =========================================================
    # HELPERS: Lectura desde Cognito y escritura en DynamoDB
    # =========================================================

    def _sync_from_cognito_to_dynamo(
        self,
        cognito_user: Mapping[str, str],
    ) -> dict:
        """Toma un usuario de Cognito y crea/actualiza su item en DynamoDB.
        Retorna el item DynamoDB resultante.
        """
        uid = cognito_user.get("uid") or cognito_user.get("username")
        if not uid:
            raise ValueError("Usuario Cognito sin uid/username")

        existing = self.dynamo.get_item(self.table, {"uid": uid}) or {}

        now = DynamoDBService.now_iso()
        item = {
            "uid": uid,
            "nombre": cognito_user.get("nombre") or existing.get("nombre", ""),
            "email": (cognito_user.get("email") or existing.get("email", "")).lower(),
            "rol": cognito_user.get("rol") or existing.get("rol", ROLE_TEACHER),
            "activo": bool(cognito_user.get("enabled", True)),
            "cognito_username": cognito_user.get("username") or uid,
            "user_status": cognito_user.get("user_status") or existing.get("user_status", "CONFIRMED"),
            "legacy_uid": cognito_user.get("legacy_uid") or existing.get("legacy_uid"),
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "ultimo_login": existing.get("ultimo_login"),
            "pk_type": PK_TYPE_USER,
        }
        self.dynamo.put_item(self.table, item)
        invalidate_user_cache(uid)
        return item

    # =========================================================
    # CREAR USUARIO
    # =========================================================

    def create_user(
        self,
        uid: str,
        nombre: str,
        email: str,
        rol: str,
        password_hash: Optional[str] = None,  # Ignorado en ETAPA 2 (Cognito)
        *,
        temporal_password: Optional[str] = None,
        legacy_uid: Optional[str] = None,
    ) -> dict:
        """Crea usuario en Cognito + DynamoDB. Retorna representacion publica.

        En ETAPA 2:
        - `password_hash` y `temporal_password` se ignoran por compatibilidad.
        - Cognito genera y envia la contrasena temporal automaticamente.
        """
        canonical_role = normalize_role(rol)
        if canonical_role is None:
            raise ValueError(f"Rol no valido: {rol}")
        if not nombre or not email:
            raise ValueError("nombre y email son obligatorios")

        if self.cognito is None:
            raise RuntimeError("CognitoService no disponible; verifica COGNITO_USER_POOL_ID")

        cognito_result = self.cognito.admin_create_user(
            email=email,
            nombre=nombre,
            rol=canonical_role,
            legacy_uid=legacy_uid,
        )
        cognito_uid = cognito_result["uid"] or cognito_result["cognito_username"]

        now = DynamoDBService.now_iso()
        dynamo_item = {
            "uid": cognito_uid,
            "nombre": nombre,
            "email": email.lower(),
            "rol": canonical_role,
            "activo": True,
            "cognito_username": cognito_result.get("cognito_username") or cognito_uid,
            "user_status": cognito_result.get("user_status", "FORCE_CHANGE_PASSWORD"),
            "legacy_uid": legacy_uid,
            "created_at": now,
            "updated_at": now,
            "pk_type": PK_TYPE_USER,
        }
        try:
            ok = self.dynamo.put_item(
                self.table,
                dynamo_item,
                condition_expression="attribute_not_exists(uid)",
            )
            if not ok:
                existing = self.dynamo.get_item(self.table, {"uid": cognito_uid}) or {}
                dynamo_item["created_at"] = existing.get("created_at", now)
                dynamo_item["updated_at"] = now
                self.dynamo.put_item(self.table, dynamo_item)
        except Exception:
            try:
                self.cognito.admin_delete_user(
                    cognito_result.get("cognito_username") or cognito_uid
                )
            except Exception:
                pass
            raise

        invalidate_user_cache(cognito_uid)

        # 🛡️ MARTILLO DE AUDITORÍA: Registrar la creación del usuario
        self.log_activity(
            actor_uid="SYSTEM",  # Por defecto el sistema para no romper firmas de la ruta
            action="Creación de usuario",
            target_uid=cognito_uid,
            details={"email": email.lower(), "rol": canonical_role}
        )

        public = self.public_user(dynamo_item)
        public["email"] = email.lower()
        return public

    # =========================================================
    # CONSULTAS
    # =========================================================

    def get_user(self, uid: str) -> Optional[dict]:
        cache_key = ("user", uid)
        cached = USER_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

        user = self.dynamo.get_item(self.table, {"uid": uid})
        if user is None:
            return None
        # Asegurar que activo sea booleano
        if "activo" not in user:
            user["activo"] = True
        USER_CACHE.set(cache_key, dict(user))
        return user

    def get_by_email(self, email: str) -> Optional[dict]:
        if not email:
            return None
        email_lc = email.lower()
        items, _ = self.dynamo.query(
            self.table,
            Key("email").eq(email_lc),
            index_name="gsi-users-email",
            limit=1,
        )
        if not items:
            # Fallback: scan por email (evitar pero por consistencia en transiciones)
            all_items = self.dynamo.scan_all(self.table, max_items=500)
            items = [u for u in all_items if str(u.get("email", "")).lower() == email_lc]
        if not items:
            return None
        user = items[0]
        uid = user.get("uid")
        if uid:
            USER_CACHE.set(("user", uid), dict(user))
        return user

    def list_users(self) -> List[dict]:
        return self.dynamo.scan_all(self.table)

    @staticmethod
    def public_user(user: Mapping) -> dict:
        private_fields = {"password", "password_hash"}
        safe = {
            k: (str(v) if "timestamp" in k.lower() or k in {"created_at", "updated_at", "ultimo_login"} and v is not None else v)
            for k, v in user.items()
            if k not in private_fields
        }
        return safe

    def list_public_users(self) -> List[dict]:
        return [self.public_user(u) for u in self.list_users()]

    def list_teachers(self) -> List[dict]:
        items, _ = self.dynamo.query(
            self.table,
            Key("rol").eq(ROLE_TEACHER),
            index_name="gsi-users-rol",
        )
        return items

    def list_active_teachers(self) -> List[dict]:
        return [u for u in self.list_teachers() if u.get("activo", False)]

    # =========================================================
    # ACTUALIZACIONES
    # =========================================================

    def update_user(
        self,
        uid: str,
        data: Mapping,
    ) -> None:
        user = self.get_user(uid)
        if not user:
            from core.exceptions import UserNotFound
            raise UserNotFound("Usuario no existe")

        allowed = {"nombre", "email", "rol", "activo"}
        changes = {k: v for k, v in data.items() if k in allowed}
        if not changes:
            return

        if "email" in changes:
            changes["email"] = str(changes["email"]).lower()

        changes["updated_at"] = DynamoDBService.now_iso()

        if self.cognito is not None:
            cognito_attrs = {}
            if "nombre" in changes:
                cognito_attrs["name"] = changes["nombre"]
            if cognito_attrs:
                try:
                    self.cognito.admin_update_user_attributes(
                        user.get("cognito_username") or user.get("email") or uid,
                        cognito_attrs,
                    )
                except Exception as err:
                    print(f"[UserService] Error actualizando Cognito (tipo={type(err).__name__})", flush=True)

            if "activo" in changes:
                try:
                    if changes["activo"]:
                        self.cognito.admin_enable_user(
                            user.get("cognito_username") or user.get("email") or uid,
                        )
                    else:
                        self.cognito.admin_disable_user(
                            user.get("cognito_username") or user.get("email") or uid,
                        )
                except Exception as err:
                    print(f"[UserService] Error enable/disable Cognito (tipo={type(err).__name__})", flush=True)

        self.dynamo.update_item(self.table, {"uid": uid}, changes)
        invalidate_user_cache(uid)
        
        # 🛡️ MARTILLO DE AUDITORÍA: Registrar la actualización (opcional pero buena práctica)
        self.log_activity(
            actor_uid="SYSTEM",
            action="Actualización de usuario",
            target_uid=uid,
            details={"campos": list(changes.keys())}
        )

    def update_password(
        self,
        uid: str,
        password_hash: str,
    ) -> None:
        """En ETAPA 2 este metodo es NO OP. Cognito gestiona las contrasenas.

        Se mantiene la firma para no romper imports/legacy code, pero NO
        escribe password_hash en DynamoDB ni en Cognito.
        """
        # Las contrasenas se gestionan desde CognitoService.
        # Aqui solo actualizamos una marca de updated_at para historial.
        self.dynamo.update_item(
            self.table,
            {"uid": uid},
            {"updated_at": DynamoDBService.now_iso()},
        )
        invalidate_user_cache(uid)

    def disable_user(self, uid: str) -> None:
        self.update_user(uid, {"activo": False})

    def enable_user(self, uid: str) -> None:
        self.update_user(uid, {"activo": True})

    def delete_user(self, uid: str) -> None:
        user = self.get_user(uid)
        if not user:
            return
        if self.cognito is not None:
            try:
                self.cognito.admin_delete_user(
                    user.get("cognito_username") or user.get("email") or uid
                )
            except Exception as err:
                print(f"[UserService] Error eliminando usuario Cognito (tipo={type(err).__name__})", flush=True)
                
        self.dynamo.delete_item(self.table, {"uid": uid})
        invalidate_user_cache(uid)

        # 🛡️ MARTILLO DE AUDITORÍA: Registrar la eliminación
        self.log_activity(
            actor_uid="SYSTEM",
            action="Cuenta eliminada",
            target_uid=uid,
            details={"email": user.get("email")}
        )

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

    def update_last_login(self, uid: str) -> None:
        self.dynamo.update_item(
            self.table,
            {"uid": uid},
            {"ultimo_login": DynamoDBService.now_iso()},
        )
        invalidate_user_cache(uid)

    # =========================================================
    # ACTIVIDAD ADMINISTRATIVA (legacy, mantenida por compatibilidad)
    # =========================================================

    @staticmethod
    def _allowed_activity_actions() -> set[str]:
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
        extra: Optional[Mapping[str, object]] = None,
    ) -> None:
        """Delega al AuditService de DynamoDB.

        Mantiene la firma original para no romper código legacy.
        Las actividades permitidas por el legacy user_activity se guardan
        también en la tabla nueva de auditoría.
        """
        try:
            actor = actor_info if isinstance(actor_info, Mapping) else (self.get_user(actor_uid) or {})
            target = (self.get_user(target_uid) or {}) if target_uid else {}
            audit = self.audit
            if audit is None:
                from services.audit_service import audit_service
                audit = audit_service()
            audit_extra = dict(extra or {})
            if details is not None:
                audit_extra.setdefault("details", details)
            if action.startswith("Creación de usuario"):
                audit.log_user_created(
                    actor_uid=actor_uid,
                    actor_email=actor.get("email", ""),
                    actor_rol=actor.get("rol", ""),
                    target_uid=target_uid or "",
                    target_email=target.get("email", ""),
                    target_rol=target.get("rol", ""),
                    **audit_extra,
                )
            elif action == "Actualización de usuario":
                audit.log_user_updated(
                    actor_uid=actor_uid,
                    actor_email=actor.get("email", ""),
                    actor_rol=actor.get("rol", ""),
                    target_uid=target_uid or "",
                    changes=(details or {}).get("campos", []) if isinstance(details, dict) else [],
                    **audit_extra,
                )
            elif action == "Cuenta eliminada":
                audit.log_user_deleted(
                    actor_uid=actor_uid,
                    actor_email=actor.get("email", ""),
                    actor_rol=actor.get("rol", ""),
                    target_uid=target_uid or "",
                    target_email=target.get("email", ""),
                    **audit_extra,
                )
        except Exception as err:
            print(f"[UserService.log_activity] Error registrando auditoria (tipo={type(err).__name__})", flush=True)
        finally:
            invalidate_activity_cache()

    def list_activity(
        self,
        uid: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[dict]:
        """Muestra actividad de auditoria en el formato del panel legacy.

        Lee desde AuditService (DynamoDB) y adapta la estructura.
        """
        try:
            from services.audit_service import AuditService, audit
            actions_map = {
                "USER_CREATED": "Creación de usuario",
                "USER_UPDATED": "Actualización de usuario",
                "USER_DELETED": "Cuenta eliminada",
            }
            if uid:
                logs = audit.list_by_actor_or_target(uid, limit=limit or 50)
            else:
                logs = audit.list_recent(limit=limit or 200)
            result = []
            for log in logs:
                legacy_action = actions_map.get(log.get("action", ""))
                if not legacy_action:
                    continue
                enriched = {
                    "fecha": log.get("timestamp"),
                    "usuario": log.get("actor_uid"),
                    "accion": legacy_action,
                    "actor_nombre": log.get("actor_email"),
                    "actor_email": log.get("actor_email"),
                    "target_uid": log.get("target_uid"),
                    "detalles": log.get("details"),
                }
                result.append(enriched)
            return result
        except Exception as err:
            print(f"[UserService.list_activity] Error (tipo={type(err).__name__})", flush=True)
            return []
