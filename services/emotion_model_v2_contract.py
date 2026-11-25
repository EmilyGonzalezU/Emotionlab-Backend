"""Contrato congelado del modelo V2 (K-Means) de EmotionLab.

Este módulo es deliberadamente PURO: no importa pandas, numpy, scikit-learn,
joblib ni boto3. Su única responsabilidad es exponer las constantes y
excepciones compartidas del modelo V2, además del bloque homogéneo de error,
de modo que los clientes ligeros (por ejemplo el cliente Lambda del backend)
puedan funcionar sin cargar el motor de inferencia ni el artefacto K-Means.

Los valores aquí definidos son INMUTABLES y deben coincidir exactamente con el
artefacto congelado.
"""

from __future__ import annotations

from typing import Any


# ==========================================================
# CONTRATO CONGELADO (no modificar)
# ==========================================================

VARIABLES_MODELO: tuple[str, ...] = (
    "inicial_nerviosismo",
    "inicial_sentir_hoy",
    "facil_nerviosismo_cierre",
    "facil_sentir_hoy_cierre",
    "dificil_nerviosismo_cierre",
    "dificil_sentir_hoy_cierre",
)

ETAPAS: dict[str, tuple[str, str]] = {
    "inicial": (
        "inicial_nerviosismo",
        "inicial_sentir_hoy",
    ),
    "facil": (
        "facil_nerviosismo_cierre",
        "facil_sentir_hoy_cierre",
    ),
    "dificil": (
        "dificil_nerviosismo_cierre",
        "dificil_sentir_hoy_cierre",
    ),
}

MODEL_VERSION_ESPERADA = "0.1.0-experimental"

SHA256_ESPERADO = (
    "6a2dd607599e7f860098f58049aff6151a7c1be9f2c6ed924a1a2224b14fdd1d"
)

N_GRUPOS = 3

# Estados del análisis de IA (separados del estado de captura).
ESTADO_ANALIZADO = "analyzed"
ESTADO_DATOS_INSUFICIENTES = "insufficient_data"
ESTADO_ERROR = "processing_error"


# ==========================================================
# ERRORES
# ==========================================================


class ModelV2Error(RuntimeError):
    """Error genérico del motor V2."""


class ModelV2ConfigError(ModelV2Error):
    """La configuración o el Pipeline no cumplen el contrato congelado."""


# ==========================================================
# BLOQUE DE ERROR COMPARTIDO
# ==========================================================


def build_error_block(error: Exception) -> dict[str, Any]:
    """Bloque homogéneo de error del análisis, sin datos sensibles."""

    return {
        "session_id": None,
        "model_version": MODEL_VERSION_ESPERADA,
        "algorithm": "K-Means",
        "n_clusters": N_GRUPOS,
        "artifact_sha256": SHA256_ESPERADO,
        "original_values": None,
        "imputed_values": None,
        "imputed_fields": [],
        "applied_medians": {},
        "inference_input": None,
        "derived_indicators": None,
        "prediction": {"group": None, "n_clusters": N_GRUPOS},
        "inference_at": None,
        "status": ESTADO_ERROR,
        "approved_production": False,
        "error": f"{type(error).__name__}: {error}"[:500],
    }
