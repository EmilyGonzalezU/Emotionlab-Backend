"""Unity adapter facade backed by the official V8 mapping and adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from services.emotion_model_v8_package import MODEL_DIR, load_official_module


class EmotionV8Parser:
    def __init__(self, record: Mapping[str, Any], model_dir=MODEL_DIR):
        model_dir = Path(model_dir)
        adapter_module = load_official_module(
            "emotionlab_adaptador_unity.py",
            "emotionlab_adaptador_unity_v8",
            model_dir,
        )
        self.adapter = adapter_module.AdaptadorUnityEmotionLab(
            model_dir / "mapeo_unity_ml_v1.json"
        )
        self.record = dict(record) if isinstance(record, Mapping) else record
        self.extraction = self.adapter.transformar_sesion(self.record)

    def get_model_values(self) -> dict[str, Any]:
        return dict(self.extraction["variables"])

    def get_extraction_report(self) -> dict[str, Any]:
        return {
            "estado_adaptacion": self.extraction["estado_adaptacion"],
            "observadas": self.extraction["observadas"],
            "ausentes": list(self.extraction["ausentes"]),
            "preguntas_ignoradas": self.extraction["preguntas_ignoradas"],
        }
