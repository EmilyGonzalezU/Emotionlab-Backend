"""Consulta `/data` del API Gateway de los lentes y correlaciona el registro
Unity que corresponde a una experiencia de EmotionLab.

Integración (cuenta AWS externa, por HTTPS):

    GET {LENS_API_BASE_URL}/data

La respuesta es una **lista** de registros; cada registro describe un archivo
de Unity subido a S3 del lente, con su `s3_key`, la marca `timestamp_subida` y
el contenido en `session_data` (`idParticipante`, `fechaInicio`, `fechaFin`,
`respuestas`, `eventos`, …). La respuesta también puede venir envuelta en una
propiedad contenedora (`data`, `records`, …); `extract_records` la normaliza.

Este servicio NO persiste nada ni interpreta el contenido de la experiencia:
solo obtiene los registros y decide cuál corresponde a la captura.

Correlación (`select_candidate`), en este orden:

    1. `lens_participant_id` (el `idParticipante` que devolvió la confirmación).
    2. Recuperación temporal para capturas antiguas sin `lens_participant_id`:
       intersección entre el intervalo de la sesión Unity y el de la captura.

Reglas de la correlación temporal:

    - Los timestamps de Unity (`fechaInicio`/`fechaFin`) son hora LOCAL del
      dispositivo y se convierten a UTC con `UNITY_LOCAL_TIMEZONE`.
    - Una sesión Unity puede COMENZAR antes de la captura de EmotionLab: eso
      NO la descarta (se compara la intersección de intervalos).
    - Un registro sin `fechaFin` está incompleto y no se selecciona.
    - Nunca se aplica una tolerancia temporal arbitraria.

Resultado:

    - Un candidato inequívoco -> `matched`.
    - Ningún candidato        -> `none` (la captura queda pendiente).
    - Más de un candidato     -> `ambiguous` (no se importa nada).

Identificadores: este servicio NUNCA crea `session_id`; EmotionLab agrupa todo
bajo un único `session_id` por experiencia.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import requests

from services.lens_session_service import (
    LensApiError,
    LensApiRateLimited,
    LensApiUnavailable,
)


DEFAULT_DATA_PATH = "/data"
DEFAULT_TIMEOUT = 15
# Reintentos ante fallos transitorios (timeout / 5xx) de la consulta de datos.
DEFAULT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.5

# Los timestamps locales de Unity se expresan en la zona horaria del
# dispositivo. Debe confirmarse con datos reales antes de fijarla.
DEFAULT_LOCAL_TIMEZONE = "America/Santiago"

# Propiedades contenedoras habituales de la respuesta de `/data`.
_CONTAINER_KEYS = ("data", "records", "items", "sesiones", "results", "body")

_PARTICIPANT_KEYS = ("idParticipante", "participant_id", "participantId")
_START_KEYS = ("fechaInicio", "started_at", "session_started_at")
_END_KEYS = ("fechaFin", "ended_at", "session_ended_at")

# Marcas de forma de un registro de `/data`.
_RECORD_MARKERS = ("session_data", "s3_key", "timestamp_subida")

STATUS_MATCHED = "matched"
STATUS_NONE = "none"
STATUS_AMBIGUOUS = "ambiguous"


class UnityDataError(LensApiError):
    """La respuesta de `/data` es inválida o inesperada."""


class SessionMatchAmbiguous(Exception):
    """Más de un registro Unity coincide con la captura: no se importa nada."""


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Normaliza `datetime`, epoch o texto ISO 8601 a `datetime`."""

    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e12:  # milisegundos
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None

        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text

        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue

        return None

    return None


def _to_utc(value: Any, *, naive_tz: Optional[Any] = None) -> Optional[datetime]:
    """Convierte un valor heterogéneo a UTC.

    Un texto/datetime SIN zona se interpreta con `naive_tz` (por defecto UTC).
    """

    moment = _parse_datetime(value)
    if moment is None:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=naive_tz or timezone.utc)

    return moment.astimezone(timezone.utc)


def _intersects(
    a_start: Optional[datetime],
    a_end: Optional[datetime],
    b_start: Optional[datetime],
    b_end: Optional[datetime],
) -> bool:
    """Intersección inclusiva de dos intervalos (extremos `None` = infinito)."""

    if a_end is not None and b_start is not None and a_end < b_start:
        return False

    if a_start is not None and b_end is not None and a_start > b_end:
        return False

    return True


class UnityDataService:
    """Obtiene los registros de `/data` y resuelve el candidato de la captura.

    No almacena estado entre llamadas y no persiste nada.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
        http_client=None,
        retries: Optional[int] = None,
        local_timezone: Optional[str] = None,
    ):
        self._explicit_base_url = base_url
        self._explicit_timeout = timeout
        self._explicit_retries = retries
        self.http = http_client or requests.Session()
        self._local_tz_name = (
            local_timezone
            or os.getenv("UNITY_LOCAL_TIMEZONE")
            or DEFAULT_LOCAL_TIMEZONE
        )

    # ==========================================================
    # CONFIGURACIÓN
    # ==========================================================

    def _base_url(self) -> str:
        url = (
            self._explicit_base_url
            or os.getenv("LENS_API_BASE_URL")
            or ""
        ).strip().rstrip("/")

        if not url:
            raise UnityDataError(
                "LENS_API_BASE_URL no está configurado."
            )

        return url

    def _timeout(self) -> int:
        if self._explicit_timeout is not None:
            return int(self._explicit_timeout)

        raw = os.getenv("LENS_API_TIMEOUT")

        try:
            return int(raw) if raw else DEFAULT_TIMEOUT
        except ValueError:
            return DEFAULT_TIMEOUT

    def _retries(self) -> int:
        if self._explicit_retries is not None:
            return int(self._explicit_retries)

        raw = os.getenv("LENS_API_DATA_RETRIES")

        try:
            value = int(raw) if raw else DEFAULT_RETRIES
        except ValueError:
            value = DEFAULT_RETRIES

        return max(0, value)

    def _local_tz(self):
        try:
            return ZoneInfo(self._local_tz_name)
        except Exception:
            # Sin datos de zona disponibles se asume UTC (nunca se rompe la
            # importación por una configuración ausente).
            return timezone.utc

    # ==========================================================
    # CONSULTA
    # ==========================================================

    def fetch_records(self) -> list[dict]:
        """Consulta `GET /data` y devuelve la lista normalizada de registros.

        Reintenta ante fallos transitorios (timeout / 5xx). Los errores
        definitivos se propagan como excepciones tipadas:
        `LensApiUnavailable`, `LensApiRateLimited` o `UnityDataError`.
        """

        url = self._base_url() + DEFAULT_DATA_PATH
        attempts = self._retries() + 1
        last_reason = "No se pudo consultar los datos del lente."

        for attempt in range(attempts):

            try:
                response = self.http.get(url, timeout=self._timeout())
            except requests.exceptions.RequestException as error:
                last_reason = "No se pudo contactar el servicio de datos del lente."
                if attempt + 1 >= attempts:
                    raise LensApiUnavailable(last_reason) from error
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            status = response.status_code

            if status >= 500:
                last_reason = (
                    f"El servicio de datos del lente respondió {status}."
                )
                if attempt + 1 >= attempts:
                    raise LensApiUnavailable(last_reason)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            if status == 429:
                raise LensApiRateLimited(
                    "El servicio del lente limitó las consultas de datos."
                )

            if status >= 400:
                raise UnityDataError(
                    f"El servicio de datos del lente respondió {status}."
                )

            try:
                payload = response.json()
            except ValueError as error:
                raise UnityDataError(
                    "Respuesta inválida del servicio de datos del lente."
                ) from error

            return self.extract_records(payload)

        raise LensApiUnavailable(last_reason)

    @staticmethod
    def extract_records(payload: Any) -> list[dict]:
        """Normaliza la respuesta de `/data` a una lista de registros."""

        if payload is None:
            return []

        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, Mapping)]

        if isinstance(payload, Mapping):
            for key in _CONTAINER_KEYS:
                value = payload.get(key)
                if isinstance(value, list):
                    return [
                        dict(item)
                        for item in value
                        if isinstance(item, Mapping)
                    ]
            # Un único objeto con forma de registro.
            if any(marker in payload for marker in _RECORD_MARKERS):
                return [dict(payload)]

        raise UnityDataError(
            "Respuesta inválida del servicio de datos del lente."
        )

    # ==========================================================
    # HELPERS DE CORRELACIÓN
    # ==========================================================

    @staticmethod
    def _field(record: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
        """Busca la primera clave no vacía en el registro y sus contenedores."""

        for container in (None, "session_data", "record"):
            source: Any = record if container is None else record.get(container)
            if not isinstance(source, Mapping):
                continue
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @classmethod
    def _participant_of(cls, record: Mapping[str, Any]) -> Optional[str]:
        return cls._field(record, _PARTICIPANT_KEYS)

    @staticmethod
    def _result(
        matches: list[dict],
        *,
        matched_by: str,
    ) -> dict:
        if len(matches) == 1:
            return {
                "status": STATUS_MATCHED,
                "candidate": matches[0],
                "matches": matches,
                "matched_by": matched_by,
            }

        if len(matches) > 1:
            return {
                "status": STATUS_AMBIGUOUS,
                "candidate": None,
                "matches": matches,
                "matched_by": matched_by,
            }

        return {
            "status": STATUS_NONE,
            "candidate": None,
            "matches": [],
            "matched_by": matched_by,
        }

    # ==========================================================
    # CORRELACIÓN
    # ==========================================================

    def select_candidate(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        lens_participant_id: Optional[str] = None,
        capture_started_at: Any = None,
        capture_ended_at: Any = None,
        data_ready_at: Any = None,
    ) -> dict:
        """Selecciona el registro Unity de la captura.

        Devuelve `{"status", "candidate", "matches", "matched_by"}`. No lanza
        excepciones: `status = "ambiguous"` indica que hay que detenerse.
        """

        items = [dict(item) for item in records if isinstance(item, Mapping)]

        # 1) Correlación por identificador de participante del lente.
        if lens_participant_id:
            expected = str(lens_participant_id)
            matches = [
                item
                for item in items
                if self._participant_of(item) == expected
            ]
            return self._result(matches, matched_by="participant")

        # 2) Recuperación temporal (capturas antiguas sin participante).
        start = _to_utc(capture_started_at)
        end = _to_utc(capture_ended_at) or _to_utc(data_ready_at)
        local_tz = self._local_tz()

        matches = []

        for item in items:

            record_start = _to_utc(
                self._field(item, _START_KEYS), naive_tz=local_tz
            )
            record_end = _to_utc(
                self._field(item, _END_KEYS), naive_tz=local_tz
            )

            # Registro incompleto (sin inicio o sin término): no se importa.
            if record_start is None or record_end is None:
                continue

            if not _intersects(record_start, record_end, start, end):
                continue

            candidate = dict(item)
            candidate["_unity_started_at"] = record_start.isoformat()
            candidate["_unity_ended_at"] = record_end.isoformat()
            matches.append(candidate)

        return self._result(matches, matched_by="temporal")
