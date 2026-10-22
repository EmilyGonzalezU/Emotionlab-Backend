"""Servicio de logging hacia Amazon CloudWatch Logs.

Ademas de registrar los logs de Flask, incluye helpers para registrar
eventos estructurados en CloudWatch y (cuando no esta disponible AWS)
realiza fallback a logger local por consola.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import boto3


class CloudWatchLogService:
    """Wrapper de CloudWatch Logs con fallback a stdout."""

    _instance: Optional["CloudWatchLogService"] = None

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
        self.log_group_name = os.getenv("CLOUDWATCH_LOG_GROUP", "/emotionlab/dev/backend")
        self.sequence_token: Optional[str] = None

        self._enabled = os.getenv("CLOUDWATCH_ENABLED", "1") in ("1", "true", "True", "TRUE")

        if self._enabled:
            try:
                self._client = boto3.client("logs", region_name=region)
                self._ensure_log_stream_exists()
            except Exception as err:
                print(f"[CLOUDWATCH] No se pudo inicializar (tipo={type(err).__name__})", flush=True)
                self._enabled = False

        self._initialized = True

    def _log_stream_name(self) -> str:
        env = os.getenv("ENV", "dev")
        pid = os.getpid()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{env}/{today}/{pid}"

    def _ensure_log_stream_exists(self) -> None:
        if not self._enabled:
            return
        try:
            self._client.create_log_group(logGroupName=self.log_group_name)
        except self._client.exceptions.ResourceAlreadyExistsException:
            pass
        except Exception:
            pass
        try:
            self._client.create_log_stream(
                logGroupName=self.log_group_name,
                logStreamName=self._log_stream_name(),
            )
        except self._client.exceptions.ResourceAlreadyExistsException:
            pass
        except Exception:
            pass

    @staticmethod
    def _event(level: str, message: str, **extra) -> dict[str, Any]:
        return {
            "timestamp": int(time.time() * 1000),
            "message": str(
                {
                    "level": level,
                    "msg": message,
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    **extra,
                }
            ),
        }

    def _put(self, events: list[dict[str, Any]]) -> None:
        if not self._enabled or not events:
            return
        try:
            kwargs = {
                "logGroupName": self.log_group_name,
                "logStreamName": self._log_stream_name(),
                "logEvents": events,
            }
            if self.sequence_token:
                kwargs["sequenceToken"] = self.sequence_token
            response = self._client.put_log_events(**kwargs)
            self.sequence_token = response.get("nextSequenceToken")
        except self._client.exceptions.InvalidSequenceTokenException as err:
            self.sequence_token = str(err).split()[-1]
            try:
                kwargs["sequenceToken"] = self.sequence_token
                response = self._client.put_log_events(**kwargs)
                self.sequence_token = response.get("nextSequenceToken")
            except Exception as err2:
                print(f"[CLOUDWATCH] PutLogEvents failed (tipo={type(err2).__name__})", flush=True)
        except Exception as err:
            print(f"[CLOUDWATCH] PutLogEvents failed (tipo={type(err).__name__})", flush=True)

    # ------------------------------------------------------------------
    # API publica de niveles de log
    # ------------------------------------------------------------------
    def info(self, msg: str, **fields) -> None:
        print(f"[INFO] {msg}", flush=True)
        self._put([self._event("INFO", msg, **fields)])

    def warn(self, msg: str, **fields) -> None:
        print(f"[WARN] {msg}", flush=True)
        self._put([self._event("WARN", msg, **fields)])

    def error(self, msg: str, exception: Optional[BaseException] = None, **fields) -> None:
        exception_type = type(exception).__name__ if exception else None
        print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
        self._put([self._event("ERROR", msg, exception_type=exception_type, **fields)])

    def debug(self, msg: str, **fields) -> None:
        if os.getenv("DEBUG_LOGS") in ("1", "true", "True"):
            print(f"[DEBUG] {msg}", flush=True)
            self._put([self._event("DEBUG", msg, **fields)])

    def event(
        self,
        name: str,
        *,
        level: str = "INFO",
        user_uid: Optional[str] = None,
        role: Optional[str] = None,
        **extra,
    ) -> None:
        """Registra un evento de negocio estructurado (ej: login, report_requested)."""
        self._put(
            [
                self._event(
                    level,
                    f"event:{name}",
                    event=name,
                    user_uid=user_uid,
                    role=role,
                    **extra,
                )
            ]
        )


# Alias para compatibilidad con nombres usados en el DI factory app.py.
CloudWatchService = CloudWatchLogService

