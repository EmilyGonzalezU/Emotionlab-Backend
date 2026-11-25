"""Loader for the vendored, hash-checked EmotionLab V8 package."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping


MODEL_DIR = Path(__file__).resolve().parents[1] / "ia" / "modelo_v8"


def verify_package(model_dir: Path = MODEL_DIR) -> dict[str, str]:
    manifest_path = model_dir / "manifest_sha256_v1.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("Falta el manifiesto SHA-256 del modelo V8.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or not manifest:
        raise ValueError("El manifiesto SHA-256 V8 está vacío o es inválido.")
    for relative, expected in manifest.items():
        candidate = (model_dir / relative).resolve()
        if model_dir.resolve() not in candidate.parents:
            raise ValueError(f"Ruta fuera del paquete V8: {relative}")
        if not candidate.is_file():
            raise FileNotFoundError(f"Falta archivo declarado por el manifiesto: {relative}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError(f"SHA-256 V8 incorrecto: {relative}")
    return dict(manifest)


def load_official_module(filename: str, module_name: str, model_dir: Path = MODEL_DIR):
    verify_package(model_dir)
    path = model_dir / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"No se pudo cargar el componente oficial V8: {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def official_metadata(model_dir: Path = MODEL_DIR) -> dict[str, Any]:
    verify_package(model_dir)
    return json.loads((model_dir / "metadatos_v1.json").read_text(encoding="utf-8"))
