"""Enmascaramiento binario y bienestar promedio de EmotionLab V2.

Capa ADITIVA y deliberadamente pura (no importa Flask ni boto3):

* **Enmascaramiento binario**: transforma la clasificación ORIGINAL del
  K-Means (0, 1, 2) en una clasificación FINAL binaria (0, 1): ``2 -> 1``.
  Se aplica SIEMPRE después de la predicción y NUNCA modifica el
  entrenamiento, los parámetros ni el artefacto del modelo.
* **Bienestar promedio**: media aritmética simple (mismo peso) de los tres
  indicadores derivados ya existentes (``inicial``, ``facil``, ``dificil``).
  No se recalcula desde las respuestas ni se aplican ponderaciones.

La clasificación original se conserva en ``model_processing`` para
trazabilidad; la binaria es la que consume EmotionLab.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

# Escala de bienestar (idéntica a la de las respuestas del cuestionario).
ESCALA_BIENESTAR: tuple[int, int] = (1, 9)

# Indicadores requeridos, con el mismo peso, para el bienestar general.
ETAPAS_BIENESTAR: tuple[str, ...] = ("inicial", "facil", "dificil")

# Mapa de enmascaramiento: 0 -> 0, 1 -> 1, 2 -> 1.
_MASCARA_BINARIA: dict[int, int] = {0: 0, 1: 1, 2: 1}


# ==========================================================
# ENMASCARAMIENTO BINARIO
# ==========================================================


def enmascarar_clasificacion(grupo: Any) -> Optional[int]:
    """Devuelve la clasificación binaria (0 o 1) para un grupo del modelo.

    ``0 -> 0``, ``1 -> 1``, ``2 -> 1``. Cualquier valor no numérico o fuera
    del rango del modelo devuelve ``None`` (no se inventa una clase).
    """

    if isinstance(grupo, bool) or grupo is None:
        return None

    try:
        numero = int(grupo)
    except (TypeError, ValueError):
        return None

    if isinstance(grupo, float) and not float(grupo).is_integer():
        return None

    return _MASCARA_BINARIA.get(numero)


# ==========================================================
# BIENESTAR PROMEDIO
# ==========================================================


def _indicador_valido(valor: Any) -> Optional[float]:
    """Normaliza un indicador; ``None`` si falta o está fuera de escala."""

    if valor is None or isinstance(valor, bool):
        return None

    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None

    if math.isnan(numero) or math.isinf(numero):
        return None

    minimo, maximo = ESCALA_BIENESTAR

    if not minimo <= numero <= maximo:
        return None

    return numero


def promedio_bienestar(etapas: Any) -> dict[str, Any]:
    """Media aritmética de los tres indicadores (mismo peso).

    Requisitos del proyecto:

    * Se usan SOLO ``inicial``, ``facil`` y ``dificil``.
    * Los tres tienen exactamente el mismo peso.
    * Si falta alguno o está fuera de la escala 1-9, NO se calcula un
      promedio parcial: se devuelve el error de forma controlada.
    * Se conserva ``promedio_exacto`` (precisión interna) y ``promedio``
      con dos decimales para su presentación.
    """

    fuente = etapas if isinstance(etapas, Mapping) else {}

    indicadores: dict[str, float] = {}
    faltantes: list[str] = []

    for etapa in ETAPAS_BIENESTAR:
        valor = _indicador_valido(fuente.get(etapa))
        if valor is None:
            faltantes.append(etapa)
        else:
            indicadores[etapa] = valor

    resultado: dict[str, Any] = {
        "escala": list(ESCALA_BIENESTAR),
        "requeridos": list(ETAPAS_BIENESTAR),
        "indicadores": indicadores,
        "faltantes": faltantes,
    }

    if faltantes:
        resultado.update(
            {
                "valido": False,
                "promedio": None,
                "promedio_exacto": None,
                "error": "indicadores_incompletos",
            }
        )
        return resultado

    exacto = sum(indicadores.values()) / len(ETAPAS_BIENESTAR)

    resultado.update(
        {
            "valido": True,
            "promedio": round(exacto, 2),
            "promedio_exacto": exacto,
            "error": None,
        }
    )
    return resultado


# ==========================================================
# UTILIDADES SOBRE UN BLOQUE ``model_processing``
# ==========================================================


def etapas_bienestar(model_processing: Any) -> dict[str, Any]:
    """Extrae ``derived_indicators.etapas`` de un bloque de procesamiento."""

    if not isinstance(model_processing, Mapping):
        return {}

    derived = model_processing.get("derived_indicators")

    if not isinstance(derived, Mapping):
        return {}

    etapas = derived.get("etapas")

    return dict(etapas) if isinstance(etapas, Mapping) else {}


def clasificacion_original(model_processing: Any) -> Optional[int]:
    """Clasificación ORIGINAL del modelo, con soporte de registros históricos."""

    if not isinstance(model_processing, Mapping):
        return None

    if "classification_original" in model_processing:
        return model_processing.get("classification_original")

    prediction = model_processing.get("prediction")

    if isinstance(prediction, Mapping):
        return prediction.get("group")

    return None


def clasificacion_binaria(model_processing: Any) -> Optional[int]:
    """Clasificación FINAL binaria (0 o 1) de un bloque de procesamiento.

    Es compatible con registros históricos: si el bloque aún no contiene
    ``classification_final``, se enmascara al vuelo el ``prediction.group``.
    """

    if not isinstance(model_processing, Mapping):
        return None

    # V8's 0/1 values are K-Means cluster identifiers, not the legacy
    # binary masking contract. Preserve them only through classification_original.
    if (
        model_processing.get("algorithm") == "KMeans"
        and model_processing.get("n_clusters") == 2
        and model_processing.get("model_version") == "1.0.0-experimental"
    ):
        return None

    final = model_processing.get("classification_final")

    if isinstance(final, int) and not isinstance(final, bool) and final in (0, 1):
        return final

    prediction = model_processing.get("prediction")

    if isinstance(prediction, Mapping):
        return enmascarar_clasificacion(prediction.get("group"))

    return None


def bienestar_promedio(model_processing: Any) -> Optional[float]:
    """Promedio general de bienestar (1-9) de un bloque de procesamiento.

    Devuelve ``None`` si no hay análisis válido o si falta algún indicador
    (nunca un promedio parcial). Compatible con registros históricos: si el
    bloque no tiene el sub-bloque ``wellbeing``, se deriva de
    ``derived_indicators``.
    """

    if not isinstance(model_processing, Mapping):
        return None

    wellbeing = model_processing.get("wellbeing")

    if isinstance(wellbeing, Mapping):
        return wellbeing.get("promedio") if wellbeing.get("valido") else None

    etapas = etapas_bienestar(model_processing)

    if not etapas:
        return None

    return promedio_bienestar(etapas).get("promedio")


def enriquecer_analisis(analisis: Any) -> Any:
    """Añade clasificación (original + binaria) y bienestar a un análisis V2.

    Modifica el bloque en sitio (se reutiliza la misma estructura de
    ``model_processing``) y NO altera ``prediction`` ni ningún metadato
    previo del modelo.
    """

    if not isinstance(analisis, dict):
        return analisis

    prediction = analisis.get("prediction")
    original = prediction.get("group") if isinstance(prediction, Mapping) else None

    analisis["classification_original"] = original
    analisis["classification_final"] = enmascarar_clasificacion(original)
    analisis["wellbeing"] = promedio_bienestar(etapas_bienestar(analisis))

    return analisis
