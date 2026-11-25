"""EmotionLab V8 inference adapter; the frozen artifacts remain untouched."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from services.emotion_model_v8_package import (
    MODEL_DIR,
    load_official_module,
    official_metadata,
    verify_package,
)
from services.emotion_model_v8_contract import MODEL_VERSION, N_CLUSTERS


def build_v8_error_block(error: Exception, session_id: Optional[str] = None) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "model_version": MODEL_VERSION,
        "algorithm": "KMeans",
        "n_clusters": N_CLUSTERS,
        "status": "processing_error",
        "estado": "error_entrada",
        "grupo": None,
        "prediction": {"group": None, "n_clusters": N_CLUSTERS},
        "tension_index": {
            "status": "pending_validation",
            "value": None,
            "reason": "La orientación/fórmula exacta requiere validación en la fuente V8.",
        },
        "error": {"type": type(error).__name__, "message": str(error)[:300]},
    }


class EmotionModelV8Service:
    def __init__(self, model_dir: Optional[Path | str] = None, *, generator=None):
        self.model_dir = Path(model_dir) if model_dir else MODEL_DIR
        self._generator = generator
        self._metadata = official_metadata(self.model_dir)
        self._manifest = verify_package(self.model_dir)
        self.model_version = self._metadata["version_modelo"]
        self.model_sha256 = self._manifest["modelo_kmeans_v1.joblib"]
        self.scaler_sha256 = self._manifest["escalador_v1.joblib"]

    @property
    def algorithm(self) -> str:
        return self._metadata["algoritmo"]

    @property
    def n_clusters(self) -> int:
        return int(self._metadata["numero_grupos"])

    def _require_supported_runtime(self) -> None:
        expected_python = self._metadata["versiones"]["python"]
        actual_python = platform.python_version()
        if actual_python != expected_python:
            raise RuntimeError(
                f"V8 requiere Python {expected_python}; el proceso usa {actual_python}."
            )
        expected_versions = {
            package: version
            for package, version in self._metadata["versiones"].items()
            if package != "python"
        }
        # The official metadata omits threadpoolctl while its own inference
        # requirements pin it. Read that source of truth without editing V8.
        requirements_path = self.model_dir / "requirements-inferencia.txt"
        for line in requirements_path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s*", line)
            if match:
                package, version = match.groups()
                expected_versions.setdefault(package, version)

        for package, expected in expected_versions.items():
            actual = importlib.metadata.version(package)
            if actual != expected:
                raise RuntimeError(
                    f"V8 requiere {package}=={expected}; el proceso usa {actual}."
                )

    @property
    def generator(self):
        if self._generator is None:
            self._require_supported_runtime()
            module = load_official_module(
                "emotionlab_salida_json.py",
                "emotionlab_salida_json_v8",
                self.model_dir,
            )
            self._generator = module.EmotionLabSalidaJSON(self.model_dir)
        return self._generator

    def analyze(self, session_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id es obligatorio para la inferencia V8.")
        if not isinstance(record, Mapping):
            raise ValueError("El registro original de Unity debe ser un objeto JSON.")

        official = self.generator.procesar_sesion(dict(record))
        # The package's original contract uses participant_id; the backend's
        # immutable experience identity remains session_id.
        official["session_id"] = session_id
        official["model_version"] = self.model_version
        official["algorithm"] = self.algorithm
        official["n_clusters"] = self.n_clusters
        official["model_artifact_sha256"] = self.model_sha256
        official["scaler_artifact_sha256"] = self.scaler_sha256
        official["status"] = self._backend_status(official.get("estado"))
        official["prediction"] = {
            "group": official.get("grupo"),
            "n_clusters": self.n_clusters,
        }
        official["classification_original"] = official.get("grupo")
        official["tension_index"] = {
            "status": "pending_validation",
            "value": None,
            "reason": "La orientación/fórmula exacta requiere validación en la fuente V8.",
            "source": "EmotionLab_V8_Informe_Tecnico_Completo.docx; sección 4",
        }
        # Reject non-finite or non-JSON values before persistence.
        json.dumps(official, ensure_ascii=False, allow_nan=False)
        return official

    @staticmethod
    def _backend_status(package_status: Any) -> str:
        if package_status in {"calculado_sin_estimacion", "calculado_con_estimacion"}:
            return "analyzed"
        if package_status in {"informacion_insuficiente", "sin_datos_suficientes"}:
            return "insufficient_data"
        return "processing_error"

    def error_block(self, error: Exception, session_id: Optional[str] = None) -> dict[str, Any]:
        block = build_v8_error_block(error, session_id)
        block.update(
            {
                "model_version": self.model_version,
                "algorithm": self.algorithm,
                "n_clusters": self.n_clusters,
                "model_artifact_sha256": self.model_sha256,
                "scaler_artifact_sha256": self.scaler_sha256,
            }
        )
        return block


class EmotionModelV8UnavailableService:
    """Keeps API startup available when only an incompatible V2 endpoint exists."""

    model_version = MODEL_VERSION

    def __init__(self, reason: str):
        self.reason = reason

    def analyze(self, session_id: str, _record: Mapping[str, Any]) -> dict[str, Any]:
        raise RuntimeError(self.reason)

    def error_block(self, error: Exception, session_id: Optional[str] = None) -> dict[str, Any]:
        return build_v8_error_block(error, session_id)
