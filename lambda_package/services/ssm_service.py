"""Servicio para leer configuracion y secretos desde AWS Systems Manager Parameter Store
y Secrets Manager.

Orden de resolucion de una configuracion:
1. Variable de entorno local (.env / entorno Python)
2. AWS SSM Parameter Store (String / SecureString)
3. AWS Secrets Manager (Secret JSON o raw)
"""

from __future__ import annotations

import os
import json
import time
from typing import Any, Optional

import boto3


class SSMConfigService:
    """Unifica lectura desde variables de entorno, SSM Parameter Store y Secrets Manager."""

    _instance: Optional["SSMConfigService"] = None

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
        self._ssm = boto3.client("ssm", region_name=region)
        self._secrets = boto3.client("secretsmanager", region_name=region)

        self._cache: dict[str, Any] = {}
        self._cache_ttl_seconds = 3600
        self._cache_set_at: dict[str, float] = {}

        self.env_prefix = os.getenv("SSM_PREFIX", "/emotionlab/dev/")
        self._initialized = True

    # ------------------------------------------------------------------
    # Cache interna
    # ------------------------------------------------------------------
    def _cache_get(self, key: str) -> Optional[Any]:
        now = time.time()
        if key not in self._cache:
            return None
        if now - self._cache_set_at.get(key, 0) > self._cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        return self._cache[key]

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self._cache_set_at[key] = time.time()

    # ------------------------------------------------------------------
    # SSM Parameter Store
    # ------------------------------------------------------------------
    def get_ssm_parameter(
        self,
        name: str,
        *,
        with_decryption: bool = True,
        use_prefix: bool = True,
    ) -> Optional[str]:
        full_name = f"{self.env_prefix}{name}" if use_prefix and not name.startswith("/") else name
        cache_key = f"ssm:{full_name}"

        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            response = self._ssm.get_parameter(
                Name=full_name,
                WithDecryption=with_decryption,
            )
            value = response.get("Parameter", {}).get("Value")
            self._cache_set(cache_key, value)
            return value
        except self._ssm.exceptions.ParameterNotFound:
            return None
        except Exception as err:
            print(f"[SSM] Error leyendo parametro {full_name}: {err}", flush=True)
            return None

    # ------------------------------------------------------------------
    # Secrets Manager
    # ------------------------------------------------------------------
    def get_secret(
        self,
        secret_id: str,
        key: Optional[str] = None,
    ) -> Optional[Any]:
        cache_key = f"secret:{secret_id}#{key or '*'}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            response = self._secrets.get_secret_value(SecretId=secret_id)
        except Exception as err:
            print(f"[SecretsManager] Error leyendo {secret_id}: {err}", flush=True)
            return None

        raw = response.get("SecretString")
        if raw is None:
            return None

        try:
            parsed: Any = json.loads(raw)
        except (ValueError, TypeError):
            parsed = raw

        result: Any = parsed.get(key) if isinstance(parsed, dict) and key else parsed
        self._cache_set(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # Resolucion de alto nivel (entorno -> SSM -> Secrets -> default)
    # ------------------------------------------------------------------
    def get(
        self,
        name: str,
        *,
        env_var: Optional[str] = None,
        ssm_param: Optional[str] = None,
        secret_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        default: Any = None,
    ) -> Any:
        var_name = env_var or name.upper().replace(":", "_").replace("/", "_").replace("-", "_")
        from_env = os.getenv(var_name)
        if from_env is not None and from_env != "":
            return from_env

        if ssm_param:
            from_ssm = self.get_ssm_parameter(ssm_param)
            if from_ssm is not None and from_ssm != "":
                return from_ssm

        if secret_id:
            from_secret = self.get_secret(secret_id, key=secret_key)
            if from_secret is not None and from_secret != "":
                return from_secret

        return default

    def get_jwt_secret(self) -> str:
        """Obtiene JWT_SECRET, priorizando variables locales y luego Secrets Manager."""
        local = os.getenv("JWT_SECRET")
        if local:
            return local
        from_secret = self.get_secret(
            os.getenv("SECRET_ARN_JWT", "emotionlab/jwt"),
            key="JWT_SECRET",
        )
        if from_secret:
            return str(from_secret)
        raise RuntimeError("JWT_SECRET no esta configurado en entorno ni Secrets Manager")

    def get_fernet_key(self) -> str:
        local = os.getenv("FERNET_KEY")
        if local:
            return local
        from_secret = self.get_secret(
            os.getenv("SECRET_ARN_FERNET", "emotionlab/fernet"),
            key="FERNET_KEY",
        )
        if from_secret:
            return str(from_secret)
        raise RuntimeError("FERNET_KEY no esta configurado en entorno ni Secrets Manager")


# Alias para compatibilidad con nombres usados en el DI factory app.py.
SSMService = SSMConfigService

