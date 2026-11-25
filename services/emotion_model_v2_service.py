"""Motor de inferencia del modelo congelado V2 (K-Means) de EmotionLab.

Este módulo es deliberadamente puro: NO importa Flask ni boto3, de modo que
puede ejecutarse tanto dentro del backend como en un entorno aislado de
inferencia con las versiones exactas de scikit-learn con las que el artefacto
fue empaquetado.

Reglas del proyecto que aquí se respetan:

* El artefacto congelado NUNCA se reentrena, reexporta ni reemplaza.
* La integridad (SHA-256) se verifica ANTES de cargar el ``joblib``.
* El contrato de las seis variables y su orden es inmutable.
* La imputación usa la mediana histórica de CADA variable, tomada del propio
  artefacto (``SimpleImputer.statistics_``); no se recalculan medianas.
* Los indicadores derivados son una capa exploratoria independiente y NO
  sustituyen las seis variables del K-Means.
* Los grupos del K-Means son numéricos (0, 1, 2): no se les asigna nombre
  clínico ni orden de severidad.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

from services.emotion_model_v2_contract import (
    ESTADO_ANALIZADO,
    ESTADO_DATOS_INSUFICIENTES,
    ESTADO_ERROR,
    ETAPAS,
    MODEL_VERSION_ESPERADA,
    N_GRUPOS,
    SHA256_ESPERADO,
    VARIABLES_MODELO,
    ModelV2ConfigError,
    ModelV2Error,
    build_error_block,
)


# ==========================================================
# CONTRATO CONGELADO (constantes y excepciones compartidas)
# ==========================================================

# Medianas históricas de la versión congelada, en el orden de VARIABLES_MODELO.
# Se validan contra ``statistics_`` del artefacto; no son una segunda fuente.
MEDIANAS_ESPERADAS: tuple[float, ...] = (3.0, 2.0, 2.0, 1.0, 3.0, 2.0)

ESCALA_ENTRADA: tuple[int, int] = (1, 9)

PASOS_PIPELINE: tuple[str, ...] = ("imputacion", "escalado", "agrupamiento")

PESO_NERVIOSISMO_ESPERADO = 0.7
PESO_ESTADO_ESPERADO = 0.3

# Correspondencia semántica definida por el proyecto (ver especificación §3):
#   facil   -> medición PRE del estudiante (NO el modo fácil de Unity)
#   dificil -> medición de la etapa DIFÍCIL

ETIQUETAS_ETAPA: dict[str, str] = {
    "inicial": "INICIAL",
    "facil": "PRE",
    "dificil": "DIFÍCIL",
}

# Estados del análisis de IA (separados del estado de captura).
ESTADO_PENDIENTE = "pending"
ESTADO_EN_PROCESO = "processing"

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_ENTERO_RE = re.compile(r"[0-9]+")


# ==========================================================
# ERRORES
# ==========================================================


class ModelV2IntegrityError(ModelV2Error):
    """El artefacto no coincide con su SHA-256 esperado."""


class ModelV2EnvironmentError(ModelV2Error):
    """El entorno de inferencia no coincide con el del artefacto."""


class InvalidResponseError(ValueError):
    """Una respuesta presente no es un entero válido dentro de la escala."""


# ==========================================================
# UTILIDADES
# ==========================================================


def calcular_sha256(ruta: os.PathLike | str) -> str:
    """Devuelve el SHA-256 en hexadecimal del archivo indicado."""

    calculadora = hashlib.sha256()

    with Path(ruta).open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            calculadora.update(bloque)

    return calculadora.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_model_dir() -> Path:
    configured = os.getenv("EMOTION_MODEL_V2_DIR", "").strip()

    if configured:
        return Path(configured)

    return _repo_root() / "ia" / "modelo_v2"


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "")


# ==========================================================
# SERVICIO
# ==========================================================


class EmotionModelV2Service:
    """Carga el artefacto congelado y ejecuta la inferencia K-Means V2.

    El motor se carga de forma perezosa: instanciar el servicio NUNCA falla
    aunque el artefacto no exista todavía. El primer uso real (o
    ``load_engine()``) valida y carga el modelo.
    """

    def __init__(
        self,
        model_dir: Optional[os.PathLike | str] = None,
        *,
        engine: Optional[Mapping[str, Any]] = None,
        strict_environment: Optional[bool] = None,
    ) -> None:
        self.model_dir = Path(model_dir) if model_dir else _default_model_dir()
        self._engine: Optional[dict[str, Any]] = engine
        self.strict_environment = _as_bool(
            os.getenv("EMOTION_MODEL_V2_STRICT_ENV"),
            True,
        ) if strict_environment is None else strict_environment

    # ------------------------------------------------------
    # CARGA DEL MOTOR
    # ------------------------------------------------------

    @property
    def engine(self) -> dict[str, Any]:
        if self._engine is None:
            self._engine = self._load_engine()
        return self._engine

    def load_engine(self) -> dict[str, Any]:
        """Valida y carga el artefacto (idempotente)."""

        return self.engine

    def _load_engine(self) -> dict[str, Any]:
        ruta_modelo = self.model_dir / "emotionlab_modelo.joblib"
        ruta_config = self.model_dir / "emotionlab_config.json"

        if not ruta_modelo.is_file():
            raise ModelV2ConfigError(
                f"No se encontró el artefacto V2 en: {ruta_modelo}"
            )

        if not ruta_config.is_file():
            raise ModelV2ConfigError(
                f"No se encontró la configuración V2 en: {ruta_config}"
            )

        configuracion = json.loads(ruta_config.read_text(encoding="utf-8"))

        self._validar_configuracion(configuracion, ruta_modelo.name)

        # Integridad ANTES de cargar joblib.
        sha_esperado = configuracion["integridad"]["sha256_modelo"]
        sha_actual = calcular_sha256(ruta_modelo)

        if not hmac.compare_digest(sha_actual, sha_esperado):
            raise ModelV2IntegrityError(
                "La integridad del artefacto V2 no coincide con la "
                "configuración."
            )

        if not hmac.compare_digest(sha_actual, SHA256_ESPERADO):
            raise ModelV2IntegrityError(
                "El artefacto V2 no corresponde a la versión congelada "
                "esperada."
            )

        advertencias = self._validar_entorno(configuracion)

        pipeline = self._cargar_pipeline(ruta_modelo)

        self._validar_pipeline(pipeline, configuracion)

        medianas = tuple(
            float(valor)
            for valor in pipeline.named_steps["imputacion"].statistics_
        )

        if medianas != MEDIANAS_ESPERADAS:
            raise ModelV2ConfigError(
                "Las medianas históricas del artefacto no coinciden con la "
                "versión congelada."
            )

        heuristica = configuracion.get("heuristica") or {}

        return {
            "pipeline": pipeline,
            "configuracion": configuracion,
            "variables": list(VARIABLES_MODELO),
            "medianas": medianas,
            "pesos": (
                float(heuristica.get("peso_nerviosismo")),
                float(heuristica.get("peso_estado")),
            ),
            "environment_warnings": advertencias,
        }

    @staticmethod
    def _cargar_pipeline(ruta_modelo: Path):
        # Import diferido: solo se requiere sklearn al ejecutar la inferencia.
        import joblib  # noqa: WPS433 (import local intencional)
        from sklearn.pipeline import Pipeline

        pipeline = joblib.load(ruta_modelo)

        if not isinstance(pipeline, Pipeline):
            raise ModelV2ConfigError(
                "El artefacto no contiene un Pipeline de scikit-learn válido."
            )

        return pipeline

    @staticmethod
    def _validar_configuracion(
        configuracion: Mapping[str, Any],
        nombre_archivo: str,
    ) -> None:
        if not isinstance(configuracion, Mapping):
            raise ModelV2ConfigError(
                "La configuración V2 no es un objeto JSON."
            )

        modelo = configuracion.get("modelo")
        integridad = configuracion.get("integridad")

        if not isinstance(modelo, Mapping) or not isinstance(
            integridad, Mapping
        ):
            raise ModelV2ConfigError(
                "La configuración V2 no contiene las secciones esperadas."
            )

        if integridad.get("archivo_modelo") != nombre_archivo:
            raise ModelV2ConfigError(
                "El archivo del modelo no coincide con la configuración."
            )

        sha = integridad.get("sha256_modelo")

        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            raise ModelV2ConfigError(
                "La configuración V2 contiene un SHA-256 inválido."
            )

        if configuracion.get("version") != MODEL_VERSION_ESPERADA:
            raise ModelV2ConfigError(
                "La versión del modelo V2 no corresponde a la congelada."
            )

        variables = modelo.get("variables_en_orden")

        if list(variables or []) != list(VARIABLES_MODELO):
            raise ModelV2ConfigError(
                "La configuración no conserva las seis características en su "
                "orden original."
            )

        if list(modelo.get("escala_entrada") or []) != list(ESCALA_ENTRADA):
            raise ModelV2ConfigError(
                "La escala de entrada no coincide con EmotionLab (1 a 9)."
            )

        heuristica = configuracion.get("heuristica") or {}

        if (
            heuristica.get("peso_nerviosismo") != PESO_NERVIOSISMO_ESPERADO
            or heuristica.get("peso_estado") != PESO_ESTADO_ESPERADO
        ):
            raise ModelV2ConfigError(
                "Los pesos de la heurística no coinciden con la versión "
                "congelada (0.7 / 0.3)."
            )

    def _validar_entorno(
        self,
        configuracion: Mapping[str, Any],
    ) -> list[str]:
        requerida = (configuracion.get("dependencias") or {}).get(
            "scikit-learn"
        )

        if not requerida:
            return []

        try:
            import sklearn
        except ImportError as error:  # pragma: no cover - entorno roto
            raise ModelV2EnvironmentError(
                "scikit-learn no está disponible en el entorno de inferencia."
            ) from error

        if sklearn.__version__ != requerida:
            mensaje = (
                "El entorno de inferencia usa scikit-learn "
                f"{sklearn.__version__} pero el artefacto V2 requiere "
                f"{requerida}."
            )

            if self.strict_environment:
                raise ModelV2EnvironmentError(mensaje)

            warnings.warn(mensaje, RuntimeWarning, stacklevel=2)
            return [mensaje]

        python_requerido = configuracion.get("python_mayor_menor")
        python_actual = f"{sys.version_info.major}.{sys.version_info.minor}"
        if python_requerido and python_actual != python_requerido:
            mensaje = (
                "El entorno de inferencia usa Python "
                f"{python_actual} pero el artefacto V2 requiere "
                f"Python {python_requerido}."
            )
            if self.strict_environment:
                raise ModelV2EnvironmentError(mensaje)
            warnings.warn(mensaje, RuntimeWarning, stacklevel=2)
            return [mensaje]

        return []

    @staticmethod
    def _validar_pipeline(
        pipeline,
        configuracion: Mapping[str, Any],
    ) -> None:
        if list(pipeline.named_steps.keys()) != list(PASOS_PIPELINE):
            raise ModelV2ConfigError(
                "El Pipeline no contiene los pasos esperados "
                "(imputacion, escalado, agrupamiento)."
            )

        if list(pipeline.feature_names_in_) != list(VARIABLES_MODELO):
            raise ModelV2ConfigError(
                "Las características del Pipeline no coinciden con el contrato."
            )

        if pipeline.n_features_in_ != len(VARIABLES_MODELO):
            raise ModelV2ConfigError(
                "El Pipeline no recibe exactamente seis características."
            )

        imputador = pipeline.named_steps["imputacion"]

        if getattr(imputador, "strategy", None) != "median":
            raise ModelV2ConfigError(
                "El paso de imputación no usa la estrategia 'median'."
            )

        estadisticas = getattr(imputador, "statistics_", None)

        if estadisticas is None or len(estadisticas) != len(VARIABLES_MODELO):
            raise ModelV2ConfigError(
                "El artefacto no expone seis medianas históricas."
            )

        algoritmo = (configuracion.get("modelo") or {}).get("algoritmo")

        estimador = pipeline.named_steps["agrupamiento"]

        if algoritmo != "K-Means":
            raise ModelV2ConfigError(
                "El algoritmo de la configuración no es compatible con este "
                "servicio (se esperaba K-Means)."
            )

        from sklearn.cluster import KMeans

        if not isinstance(estimador, KMeans):
            raise ModelV2ConfigError(
                "El estimador del Pipeline no corresponde a K-Means."
            )

        if estimador.n_clusters != N_GRUPOS:
            raise ModelV2ConfigError(
                "El número de grupos del artefacto no coincide con la "
                "configuración congelada."
            )

    # ------------------------------------------------------
    # MEDIANAS
    # ------------------------------------------------------

    @property
    def medianas(self) -> tuple[float, ...]:
        """Medianas históricas alineadas al orden de ``VARIABLES_MODELO``."""

        if self._engine is not None and self._engine.get("medianas"):
            return tuple(self._engine["medianas"])

        return MEDIANAS_ESPERADAS

    # ------------------------------------------------------
    # IMPUTACIÓN
    # ------------------------------------------------------

    def normalizar_valor(self, valor: Any, variable: str) -> Optional[int]:
        """Normaliza una respuesta individual a entero de la escala 1..9.

        ``None``, cadenas vacías y ``NaN`` representan ausencia y devuelven
        ``None``. Cualquier otro valor presente pero inválido lanza
        ``InvalidResponseError`` (no se imputa silenciosamente).
        """

        if valor is None:
            return None

        if isinstance(valor, bool):
            raise InvalidResponseError(
                f"Respuesta inválida en {variable}: booleano."
            )

        if isinstance(valor, float):
            if math.isnan(valor) or math.isinf(valor):
                return None
            if not float(valor).is_integer():
                raise InvalidResponseError(
                    f"Respuesta inválida en {variable}: se esperaba un entero."
                )
            numero = int(valor)
        elif isinstance(valor, int):
            numero = valor
        elif isinstance(valor, str):
            texto = valor.strip()
            if not texto:
                return None
            if _ENTERO_RE.fullmatch(texto) is None:
                raise InvalidResponseError(
                    f"Respuesta inválida en {variable}: se esperaba un entero."
                )
            numero = int(texto)
        else:
            raise InvalidResponseError(
                f"Respuesta inválida en {variable}: tipo de dato incompatible."
            )

        minimo, maximo = ESCALA_ENTRADA

        if not minimo <= numero <= maximo:
            raise InvalidResponseError(
                f"Respuesta fuera de escala en {variable}: se esperaba un "
                f"entero entre {minimo} y {maximo}."
            )

        return numero

    def imputar(
        self,
        valores_originales: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Completa los valores faltantes con la mediana de su variable.

        Devuelve la trazabilidad completa exigida por el proyecto:
        ``respuestas_originales``, ``respuestas_imputadas``,
        ``campos_imputados`` y ``medianas_aplicadas``.
        """

        medianas = self.medianas

        originales: dict[str, Optional[int]] = {}
        imputadas: dict[str, int] = {}
        medicion_original: dict[str, Any] = {}
        campos_imputados: list[str] = []
        medianas_aplicadas: dict[str, float] = {}

        for indice, variable in enumerate(VARIABLES_MODELO):
            valor = self.normalizar_valor(
                valores_originales.get(variable),
                variable,
            )

            # El valor ORIGINAL se conserva tal cual (incluido su ausencia).
            medicion_original[variable] = valores_originales.get(variable)
            originales[variable] = valor

            if valor is None:
                imputadas[variable] = int(medianas[indice])
                campos_imputados.append(variable)
                medianas_aplicadas[variable] = medianas[indice]
            else:
                imputadas[variable] = valor

        return {
            "respuestas_originales": originales,
            "respuestas_medidas": medicion_original,
            "respuestas_imputadas": imputadas,
            "campos_imputados": campos_imputados,
            "medianas_aplicadas": medianas_aplicadas,
        }

    # ------------------------------------------------------
    # INDICADORES DERIVADOS (capa exploratoria independiente)
    # ------------------------------------------------------

    def indicadores_derivados(
        self,
        respuestas_imputadas: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Calcula ``0.7 * nerviosismo + 0.3 * estado`` por etapa.

        Es una capa COMPLEMENTARIA: no sustituye las seis variables del
        K-Means ni introduce categorías clínicas.
        """

        pesos = (
            self._engine.get("pesos")
            if self._engine is not None
            else (PESO_NERVIOSISMO_ESPERADO, PESO_ESTADO_ESPERADO)
        )
        peso_nerviosismo, peso_estado = pesos

        etapas: dict[str, Optional[float]] = {}

        for etapa, (var_nerviosismo, var_estado) in ETAPAS.items():
            nerviosismo = respuestas_imputadas.get(var_nerviosismo)
            estado = respuestas_imputadas.get(var_estado)

            if nerviosismo is None or estado is None:
                etapas[etapa] = None
                continue

            etapas[etapa] = round(
                peso_nerviosismo * float(nerviosismo)
                + peso_estado * float(estado),
                4,
            )

        return {
            "naturaleza": "exploratorio",
            "formula": "0.7 * nerviosismo + 0.3 * estado",
            "peso_nerviosismo": peso_nerviosismo,
            "peso_estado": peso_estado,
            "escala": list(ESCALA_ENTRADA),
            "etapas": etapas,
            "etiquetas_etapa": dict(ETIQUETAS_ETAPA),
            "orientacion_estado_validada": False,
            "inversion_estado": None,
            "umbrales_categorias": None,
            "clasificacion_definitiva": False,
        }

    # ------------------------------------------------------
    # INFERENCIA
    # ------------------------------------------------------

    def analyze(
        self,
        session_id: str,
        valores_originales: Mapping[str, Any],
        *,
        include_indicators: bool = True,
    ) -> dict[str, Any]:
        """Ejecuta el flujo completo de análisis V2 para una sesión.

        Devolver una estructura con el grupo K-Means y toda la trazabilidad.
        Si las seis respuestas están ausentes, NO se ejecuta la inferencia:
        se devuelve ``insufficient_data``.
        """

        imputacion = self.imputar(valores_originales)

        resultado: dict[str, Any] = {
            "session_id": session_id,
            "model_version": MODEL_VERSION_ESPERADA,
            "algorithm": "K-Means",
            "n_clusters": N_GRUPOS,
            "artifact_sha256": SHA256_ESPERADO,
            "original_values": imputacion["respuestas_originales"],
            "measured_values": imputacion["respuestas_medidas"],
            "imputed_values": imputacion["respuestas_imputadas"],
            "imputed_fields": imputacion["campos_imputados"],
            "applied_medians": imputacion["medianas_aplicadas"],
            "inference_input": None,
            "derived_indicators": None,
            "prediction": {"group": None, "n_clusters": N_GRUPOS},
            "inference_at": None,
            "status": ESTADO_PENDIENTE,
            "approved_production": False,
        }

        ausentes = [
            variable
            for variable in VARIABLES_MODELO
            if imputacion["respuestas_originales"].get(variable) is None
        ]

        if len(ausentes) == len(VARIABLES_MODELO):
            resultado["status"] = ESTADO_DATOS_INSUFICIENTES
            resultado["reason"] = "datos_insuficientes"
            resultado["missing_fields"] = ausentes
            return resultado

        if include_indicators:
            resultado["derived_indicators"] = self.indicadores_derivados(
                imputacion["respuestas_imputadas"]
            )

        matriz = pd.DataFrame(
            [
                {
                    variable: float(imputacion["respuestas_imputadas"][variable])
                    for variable in VARIABLES_MODELO
                }
            ],
            columns=list(VARIABLES_MODELO),
        )

        grupo = int(self.engine["pipeline"].predict(matriz)[0])

        if not 0 <= grupo < N_GRUPOS:
            raise ModelV2Error(
                "El modelo devolvió un grupo fuera del rango esperado."
            )

        resultado.update(
            {
                "inference_input": {
                    variable: imputacion["respuestas_imputadas"][variable]
                    for variable in VARIABLES_MODELO
                },
                "prediction": {
                    "group": grupo,
                    "n_clusters": N_GRUPOS,
                },
                "inference_at": _utc_now(),
                "status": ESTADO_ANALIZADO,
                "reason": None,
                "missing_fields": ausentes,
            }
        )

        return resultado

    # ------------------------------------------------------
    # ERRORES
    # ------------------------------------------------------

    @staticmethod
    def error_block(error: Exception) -> dict[str, Any]:
        """Bloque homogéneo de error del análisis, sin datos sensibles."""

        return build_error_block(error)
