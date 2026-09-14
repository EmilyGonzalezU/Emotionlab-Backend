"""Cliente de solo lectura para el lote de sesiones publicado en AWS."""

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

DEFAULT_UNITY_DATA_URL = "https://c2a1zfsvcj.execute-api.us-east-1.amazonaws.com/prod/data"


class AWSService:
    """Obtiene el lote JSON de Unity sin conocer la logica de importacion."""

    def __init__(self, data_url: str | None = None, timeout: int = 120):
        self.data_url = data_url or os.getenv("AWS_UNITY_DATA_URL", DEFAULT_UNITY_DATA_URL)
        self.timeout = timeout

    def fetch_sessions(self) -> dict[str, Any]:
        try:
            with urlopen(self.data_url, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ValueError("No fue posible obtener el lote de sesiones desde AWS") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("sesiones"), list):
            raise ValueError("AWS no devolvio un lote de sesiones valido")
        return payload
