"""Handler del servicio de inferencia V2 (K-Means congelado) de EmotionLab.

Este servicio se despliega en una imagen Docker con las versiones EXACTAS de
las dependencias del artefacto congelado (scikit-learn 1.6.1). Es el ÚNICO
motor de IA vigente: el backend Flask delega aquí toda la inferencia para no
depender de su propia versión de scikit-learn.

Contrato de entrada (exclusivamente las seis variables del modelo):

    {
      "session_id": "<uuid de EmotionLab>",
      "values": {
        "inicial_nerviosismo": 5,
        "inicial_sentir_hoy": 6,
        "facil_nerviosismo_cierre": 4,
        "facil_sentir_hoy_cierre": 5,
        "dificil_nerviosismo_cierre": 7,
        "dificil_sentir_hoy_cierre": 6
      },
      "include_indicators": true
    }

Respuesta: ``{"ok": true, "analysis": {...}}`` con la trazabilidad completa
(valores originales, imputados, campos imputados, medianas aplicadas, matriz de
inferencia, indicadores derivados y grupo K-Means en ``{0, 1, 2}``).

La integridad del artefacto (SHA-256) se verifica ANTES de cargar el ``joblib``
y el entorno se valida de forma estricta; si algo no coincide, el servicio
responde 503 y el backend deja el análisis en ``processing_error``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

LAMBDA_TASK_ROOT = Path(__file__).resolve().parent

if str(LAMBDA_TASK_ROOT) not in sys.path:
    sys.path.insert(0, str(LAMBDA_TASK_ROOT))

# El motor exige el entorno congelado y resuelve el artefacto por esta ruta.
os.environ.setdefault("EMOTION_MODEL_V2_STRICT_ENV", "1")
os.environ.setdefault(
    "EMOTION_MODEL_V2_DIR",
    str(LAMBDA_TASK_ROOT / "ia" / "modelo_v2"),
)

from services.emotion_model_v2_service import (  # noqa: E402
    EmotionModelV2Service,
    ModelV2Error,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Carga perezosa: importar el handler nunca falla aunque el artefacto tarde.
_service = EmotionModelV2Service()


def _json_response(status_code: int, payload: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, ensure_ascii=False, allow_nan=False),
    }


def handler(event, context=None):
    try:
        body = event.get("body", event)

        if isinstance(body, str):
            body = json.loads(body)

        if not isinstance(body, dict):
            raise ValueError("La solicitud debe ser un objeto JSON.")

        session_id = body.get("session_id")

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("Falta el identificador de sesión.")

        session_id = session_id.strip()[:128]

        values = body.get("values")

        if not isinstance(values, dict):
            raise ValueError("El campo 'values' debe ser un objeto JSON.")

        analisis = _service.analyze(
            session_id,
            values,
            include_indicators=bool(body.get("include_indicators", True)),
        )

        return _json_response(200, {"ok": True, "analysis": analisis})

    except ValueError as error:
        logger.warning(
            "Solicitud de inferencia V2 inválida (tipo=%s)",
            type(error).__name__,
        )
        return _json_response(
            400,
            {"ok": False, "error": "Solicitud de inferencia V2 inválida."},
        )

    except ModelV2Error as error:
        logger.error(
            "Motor V2 no disponible (tipo=%s)",
            type(error).__name__,
        )
        return _json_response(
            503,
            {
                "ok": False,
                "error": "El servicio de inferencia V2 no está disponible.",
            },
        )

    except Exception as error:
        logger.error(
            "Error interno del servicio V2 (tipo=%s)",
            type(error).__name__,
        )
        return _json_response(
            500,
            {"ok": False, "error": "Error interno del servicio de inferencia V2."},
        )
